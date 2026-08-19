"""Strict OT-2 labware-well to Matterix nested-rigid child bindings."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from ..digital_twin.config import DigitalTwinConfig

TIP_RACK_BINDING_SCHEMA_VERSION = "1.0"
_VERIFIED = "VERIFIED"
_UNVERIFIED = "UNVERIFIED"


class TipRackBindingError(ValueError):
    """A tip-rack binding is incomplete, ambiguous, or inconsistent."""


def nested_manifest_sha256(asset_name: str, child_ids: Sequence[str]) -> str:
    """Hash one nested-rigid manifest independently of enumeration order."""
    normalized_asset_name = asset_name.strip()
    normalized_child_ids = sorted(str(child_id).strip() for child_id in child_ids)
    if not normalized_asset_name or any(not child_id for child_id in normalized_child_ids):
        msg = "Nested manifest asset_name and child identifiers must not be empty"
        raise TipRackBindingError(msg)
    if len(normalized_child_ids) != len(set(normalized_child_ids)):
        msg = "Nested manifest contains duplicate child identifiers"
        raise TipRackBindingError(msg)
    payload = {
        "schema_version": TIP_RACK_BINDING_SCHEMA_VERSION,
        "asset_name": normalized_asset_name,
        "child_ids": normalized_child_ids,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _well_ids(connector: DigitalTwinConfig, labware_id: str) -> tuple[str, ...]:
    placement = connector.placement(labware_id)
    return tuple(f"{row}{column}" for row in placement.grid.rows for column in range(1, placement.grid.columns + 1))


@dataclasses.dataclass(frozen=True)
class TipRackBinding:
    """One connector tip rack backed by independently addressable rigid tips."""

    schema_version: str
    binding_id: str
    status: str
    labware_id: str
    asset_name: str
    mount: str
    ee_sensor_name: str
    child_ids: dict[str, str]
    non_tip_child_ids: tuple[str, ...]
    expected_child_count: int
    manifest_sha256: str
    evidence: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, index: int) -> TipRackBinding:
        """Parse a strict binding mapping."""
        field = f"tip_rack_bindings[{index}]"
        allowed = {
            "schema_version",
            "binding_id",
            "status",
            "labware_id",
            "asset_name",
            "mount",
            "ee_sensor_name",
            "child_ids",
            "non_tip_child_ids",
            "expected_child_count",
            "manifest_sha256",
            "evidence",
        }
        unknown = set(value) - allowed
        if unknown:
            msg = f"{field} contains unknown fields {sorted(unknown)}"
            raise TipRackBindingError(msg)
        try:
            count = value["expected_child_count"]
            if isinstance(count, bool):
                msg = "expected_child_count must be an integer"
                raise TypeError(msg)
            raw_child_ids = value["child_ids"]
            raw_non_tip_child_ids = value["non_tip_child_ids"]
            if not isinstance(raw_child_ids, Mapping):
                msg = "child_ids must be an object"
                raise TypeError(msg)
            if not isinstance(raw_non_tip_child_ids, list):
                msg = "non_tip_child_ids must be an array"
                raise TypeError(msg)
            result = cls(
                schema_version=str(value["schema_version"]),
                binding_id=str(value["binding_id"]).strip(),
                status=str(value["status"]).upper(),
                labware_id=str(value["labware_id"]).strip(),
                asset_name=str(value["asset_name"]).strip(),
                mount=str(value["mount"]).strip().upper(),
                ee_sensor_name=str(value["ee_sensor_name"]).strip(),
                child_ids={
                    str(well).strip().upper(): str(child_id).strip() for well, child_id in raw_child_ids.items()
                },
                non_tip_child_ids=tuple(str(child_id).strip() for child_id in raw_non_tip_child_ids),
                expected_child_count=int(count),
                manifest_sha256=str(value["manifest_sha256"]).strip(),
                evidence=str(value["evidence"]).strip(),
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"{field} is missing or invalid: {error}"
            raise TipRackBindingError(msg) from error
        result._validate(field)
        return result

    def _validate(self, field: str) -> None:
        if self.schema_version != TIP_RACK_BINDING_SCHEMA_VERSION:
            msg = f"{field}.schema_version must be {TIP_RACK_BINDING_SCHEMA_VERSION!r}"
            raise TipRackBindingError(msg)
        if self.status not in {_VERIFIED, _UNVERIFIED}:
            msg = f"{field}.status must be VERIFIED or UNVERIFIED"
            raise TipRackBindingError(msg)
        if any(
            not value
            for value in (
                self.binding_id,
                self.labware_id,
                self.asset_name,
                self.mount,
                self.ee_sensor_name,
                self.manifest_sha256,
                self.evidence,
            )
        ):
            msg = f"{field} identity and evidence fields must not be empty"
            raise TipRackBindingError(msg)
        if self.mount not in {"LEFT", "RIGHT"}:
            msg = f"{field}.mount must be LEFT or RIGHT"
            raise TipRackBindingError(msg)
        if len(self.child_ids) != self.expected_child_count:
            msg = f"{field}.child_ids must contain expected_child_count entries"
            raise TipRackBindingError(msg)
        all_child_ids = [*self.child_ids.values(), *self.non_tip_child_ids]
        if any(not child_id for child_id in all_child_ids):
            msg = f"{field} child identifiers must not be empty"
            raise TipRackBindingError(msg)
        if len(all_child_ids) != len(set(all_child_ids)):
            msg = f"{field} child identifiers must be globally unique"
            raise TipRackBindingError(msg)
        if self.expected_child_count < 1:
            msg = f"{field}.expected_child_count must be positive"
            raise TipRackBindingError(msg)
        if self.status == _VERIFIED:
            if self.binding_id.upper().startswith("UNVERIFIED"):
                msg = f"{field} cannot be VERIFIED with an UNVERIFIED binding_id"
                raise TipRackBindingError(msg)
            if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256):
                msg = f"{field}.manifest_sha256 must pin the accepted nested manifest"
                raise TipRackBindingError(msg)
            expected_digest = nested_manifest_sha256(self.asset_name, all_child_ids)
            if self.manifest_sha256 != expected_digest:
                msg = (
                    f"{field}.manifest_sha256 does not match its configured nested child identifiers; "
                    f"expected {expected_digest}"
                )
                raise TipRackBindingError(msg)

    @property
    def verified(self) -> bool:
        """Return whether the nested manifest binding is accepted."""
        return self.status == _VERIFIED

    def require_verified(self) -> None:
        """Reject physical tip attachment from an unaccepted child map."""
        if not self.verified:
            msg = (
                f"Tip-rack binding {self.labware_id!r} is UNVERIFIED; accept the 96-child nested manifest "
                "before translating physical pickup/return semantics"
            )
            raise TipRackBindingError(msg)

    def child_id(self, well: str) -> str:
        """Resolve one normalized well into its explicit nested child id."""
        normalized = well.strip().upper()
        try:
            return self.child_ids[normalized]
        except KeyError as error:
            msg = f"Tip rack {self.labware_id!r} has no nested child mapping for well {well!r}"
            raise TipRackBindingError(msg) from error

    def validate_connector(self, connector: DigitalTwinConfig, *, require_verified: bool = True) -> tuple[str, ...]:
        """Require an exact one-to-one mapping for the connector's rack wells."""
        placement = connector.placement(self.labware_id)
        if "tiprack" not in placement.definition_uri.lower():
            msg = f"Bound labware {self.labware_id!r} is not identified as a tip rack"
            raise TipRackBindingError(msg)
        try:
            instrument = connector.instrument(self.mount)
        except ValueError as error:
            msg = f"Tip-rack binding mount {self.mount} has no connector instrument profile"
            raise TipRackBindingError(msg) from error
        if instrument.channels != 1:
            msg = (
                f"Tip-rack binding {self.labware_id!r} selects one nested child per pickup, but "
                f"the {self.mount} instrument has {instrument.channels} channels; multi-channel "
                "attachment needs a separate multi-child semantic contract"
            )
            raise TipRackBindingError(msg)
        wells = _well_ids(connector, self.labware_id)
        if set(self.child_ids) != set(wells):
            missing = sorted(set(wells) - set(self.child_ids))
            unexpected = sorted(set(self.child_ids) - set(wells))
            msg = (
                f"Tip rack {self.labware_id!r} well map does not match connector geometry; "
                f"missing={missing}, unexpected={unexpected}"
            )
            raise TipRackBindingError(msg)
        child_ids = tuple(self.child_ids[well] for well in wells)
        if len(wells) != self.expected_child_count:
            msg = (
                f"Tip rack {self.labware_id!r} defines {len(wells)} connector wells, "
                f"not expected_child_count={self.expected_child_count}"
            )
            raise TipRackBindingError(msg)
        if len(child_ids) != len(set(child_ids)):
            msg = f"Tip rack {self.labware_id!r} well map does not contain unique child ids"
            raise TipRackBindingError(msg)
        if require_verified:
            self.require_verified()
        return child_ids

    def validate_manifest(self, connector: DigitalTwinConfig, child_ids: Sequence[str]) -> None:
        """Require the runtime nested manifest to equal the configured well map."""
        expected = {*self.validate_connector(connector, require_verified=False), *self.non_tip_child_ids}
        actual = {str(child_id) for child_id in child_ids}
        if len(actual) != len(child_ids):
            msg = f"Nested manifest for {self.asset_name!r} contains duplicate child ids"
            raise TipRackBindingError(msg)
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            msg = (
                f"Nested manifest for {self.asset_name!r} does not match {self.labware_id!r}; "
                f"missing={missing}, unexpected={unexpected}"
            )
            raise TipRackBindingError(msg)
        if self.verified:
            actual_digest = nested_manifest_sha256(self.asset_name, child_ids)
            if actual_digest != self.manifest_sha256:
                msg = (
                    f"Nested manifest hash {actual_digest} does not match pinned "
                    f"{self.manifest_sha256} for {self.asset_name!r}"
                )
                raise TipRackBindingError(msg)


def binding_for_labware(bindings: Sequence[TipRackBinding], labware_id: str) -> TipRackBinding:
    """Resolve exactly one tip-rack binding by connector labware id."""
    matches = [binding for binding in bindings if binding.labware_id == labware_id]
    if len(matches) != 1:
        msg = f"Expected exactly one tip-rack binding for {labware_id!r}, found {len(matches)}"
        raise TipRackBindingError(msg)
    return matches[0]


__all__ = [
    "TIP_RACK_BINDING_SCHEMA_VERSION",
    "TipRackBinding",
    "TipRackBindingError",
    "binding_for_labware",
    "nested_manifest_sha256",
]
