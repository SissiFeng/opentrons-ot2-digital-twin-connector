"""Build and verify a hash-pinned contract from serialized SiLA FDL."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sila.framework.fdl import Serializer
from unitelabs.cdk import sila

_SILA_NAMESPACE = {"sila": "http://www.sila-standard.org"}
CONTRACT_SCHEMA_VERSION = "1.0"


class ContractMismatchError(RuntimeError):
    """The client and connector contracts differ; update the adapter before issuing commands."""


@dataclasses.dataclass(frozen=True)
class EndpointContract:
    """One command or property and its SiLA execution mode."""

    identifier: str
    mode: str
    parameters: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class FeatureContract:
    """Normalized contract extracted from one serialized feature definition."""

    originator: str
    category: str
    identifier: str
    version: str
    fqi: str
    commands: tuple[EndpointContract, ...]
    properties: tuple[EndpointContract, ...]
    defined_errors: tuple[str, ...]
    fdl_sha256: str


@dataclasses.dataclass(frozen=True)
class ContractSnapshot:
    """Canonical connector contract pinned by a stable SHA-256 contract identifier."""

    schema_version: str
    config_schema_version: str
    config_schema_sha256: str
    features: tuple[FeatureContract, ...]

    @property
    def contract_id(self) -> str:
        """Return the hash identity of this contract without a self-referential field."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def canonical_json(self) -> str:
        """Serialize deterministically for hashing and cross-language adapters."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def to_mapping(self) -> dict[str, Any]:
        """Return the transport representation including contract_id."""
        return {"contract_id": self.contract_id, **dataclasses.asdict(self)}

    def write(self, path: str | Path) -> None:
        """Write an auditable contract artifact."""
        Path(path).write_text(json.dumps(self.to_mapping(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> ContractSnapshot:
        """Parse and verify a stored contract artifact."""
        features = tuple(
            FeatureContract(
                originator=str(item["originator"]),
                category=str(item["category"]),
                identifier=str(item["identifier"]),
                version=str(item["version"]),
                fqi=str(item["fqi"]),
                commands=tuple(
                    EndpointContract(
                        identifier=str(endpoint["identifier"]),
                        mode=str(endpoint["mode"]),
                        parameters=tuple(endpoint["parameters"]),
                    )
                    for endpoint in item["commands"]
                ),
                properties=tuple(
                    EndpointContract(
                        identifier=str(endpoint["identifier"]),
                        mode=str(endpoint["mode"]),
                        parameters=tuple(endpoint["parameters"]),
                    )
                    for endpoint in item["properties"]
                ),
                defined_errors=tuple(item["defined_errors"]),
                fdl_sha256=str(item["fdl_sha256"]),
            )
            for item in value["features"]
        )
        snapshot = cls(
            schema_version=str(value["schema_version"]),
            config_schema_version=str(value["config_schema_version"]),
            config_schema_sha256=str(value["config_schema_sha256"]),
            features=features,
        )
        expected = str(value["contract_id"])
        if snapshot.contract_id != expected:
            msg = f"Stored contract_id {expected} does not match canonical contract {snapshot.contract_id}"
            raise ContractMismatchError(msg)
        return snapshot

    @classmethod
    def from_file(cls, path: str | Path) -> ContractSnapshot:
        """Load a contract artifact."""
        return cls.from_mapping(json.loads(Path(path).read_text(encoding="utf-8")))

    def require(self, expected_contract_id: str) -> None:
        """Fail closed when a bridge or twin was generated for another contract."""
        if self.contract_id != expected_contract_id:
            msg = (
                f"Connector contract {self.contract_id} does not match expected {expected_contract_id}. "
                "Regenerate the client adapter from the connector FDL before motion."
            )
            raise ContractMismatchError(msg)


def _text_identifiers(root: ElementTree.Element, path: str) -> tuple[str, ...]:
    return tuple(sorted(identifier.text or "" for identifier in root.findall(path, _SILA_NAMESPACE) if identifier.text))


def _endpoint_contracts(root: ElementTree.Element, element_name: str) -> tuple[EndpointContract, ...]:
    endpoints = []
    for element in root.findall(f".//sila:{element_name}", _SILA_NAMESPACE):
        identifier = element.find("sila:Identifier", _SILA_NAMESPACE)
        observable = element.find("sila:Observable", _SILA_NAMESPACE)
        if identifier is None or not identifier.text or observable is None or not observable.text:
            msg = f"Serialized FDL {element_name} is missing Identifier or Observable"
            raise ContractMismatchError(msg)
        endpoints.append(
            EndpointContract(
                identifier=identifier.text,
                mode="OBSERVABLE" if observable.text == "Yes" else "UNOBSERVABLE",
                parameters=tuple(
                    parameter.text or ""
                    for parameter in element.findall("sila:Parameter/sila:Identifier", _SILA_NAMESPACE)
                    if parameter.text
                ),
            )
        )
    return tuple(sorted(endpoints, key=lambda endpoint: endpoint.identifier))


def _feature_contract(feature: sila.Feature) -> FeatureContract:
    fdl = Serializer.serialize(feature.serialize)
    root = ElementTree.fromstring(fdl)
    identifier_node = root.find("sila:Identifier", _SILA_NAMESPACE)
    if identifier_node is None or not identifier_node.text:
        msg = "Serialized FDL has no feature Identifier"
        raise ContractMismatchError(msg)
    originator = root.attrib["Originator"]
    category = root.attrib["Category"]
    version = root.attrib["FeatureVersion"]
    identifier = identifier_node.text
    return FeatureContract(
        originator=originator,
        category=category,
        identifier=identifier,
        version=version,
        fqi=f"{originator}/{category}/{identifier}/v{version.split('.')[0]}",
        commands=_endpoint_contracts(root, "Command"),
        properties=_endpoint_contracts(root, "Property"),
        defined_errors=_text_identifiers(root, ".//sila:DefinedExecutionError/sila:Identifier"),
        fdl_sha256=hashlib.sha256(fdl.encode()).hexdigest(),
    )


def build_contract_snapshot(
    features: Iterable[sila.Feature],
    *,
    config_schema_version: str,
    config_schema_bytes: bytes,
) -> ContractSnapshot:
    """Build a canonical contract from connector-registered feature instances."""
    normalized = tuple(sorted((_feature_contract(feature) for feature in features), key=lambda item: item.fqi))
    if not normalized:
        msg = "At least one serialized feature is required to build a connector contract"
        raise ContractMismatchError(msg)
    return ContractSnapshot(
        schema_version=CONTRACT_SCHEMA_VERSION,
        config_schema_version=config_schema_version,
        config_schema_sha256=hashlib.sha256(config_schema_bytes).hexdigest(),
        features=normalized,
    )
