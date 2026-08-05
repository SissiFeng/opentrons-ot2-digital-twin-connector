"""Validated identity of the future Matterix OT-2 asset and action extension."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from ..bridge.registry import ContractRegistry
from ..digital_twin.config import DigitalTwinConfig

_AXES = frozenset("XYZABC")
_PLACEHOLDER_HASH = "REPLACE_WITH_VERIFIED_ASSET_SHA256"


class MatterixAssetConfigurationError(ValueError):
    """Matterix asset identity is missing, ambiguous, or inconsistent."""


@dataclasses.dataclass(frozen=True)
class OT2MatterixAssetConfig:
    """Identity pins and joint mapping required by an OT-2 Matterix task."""

    schema_version: str
    task_id: str
    robot_asset_name: str
    asset_path: str
    asset_sha256: str
    action_factory_module: str
    deck_frame: str
    axis_joints: dict[str, str]
    contract_id: str
    config_id: str
    calibration_id: str

    @classmethod
    def from_file(cls, path: str | Path) -> OT2MatterixAssetConfig:
        """Load and validate a Matterix asset configuration."""
        config_path = Path(path)
        try:
            value = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            msg = f"Unable to load Matterix OT-2 configuration from {config_path}: {error}"
            raise MatterixAssetConfigurationError(msg) from error
        if not isinstance(value, dict):
            msg = "Matterix OT-2 configuration root must be an object"
            raise MatterixAssetConfigurationError(msg)
        return cls.from_mapping(value)

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> OT2MatterixAssetConfig:
        """Construct and validate a configuration mapping."""
        try:
            raw_joints = value["axis_joints"]
            if not isinstance(raw_joints, dict):
                msg = "axis_joints must be an object"
                raise TypeError(msg)
            result = cls(
                schema_version=str(value["schema_version"]),
                task_id=str(value["task_id"]),
                robot_asset_name=str(value["robot_asset_name"]),
                asset_path=str(value["asset_path"]),
                asset_sha256=str(value["asset_sha256"]),
                action_factory_module=str(value["action_factory_module"]),
                deck_frame=str(value["deck_frame"]),
                axis_joints={str(axis).upper(): str(joint) for axis, joint in raw_joints.items()},
                contract_id=str(value["contract_id"]),
                config_id=str(value["config_id"]),
                calibration_id=str(value["calibration_id"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"Matterix OT-2 configuration is missing or invalid: {error}"
            raise MatterixAssetConfigurationError(msg) from error
        values = (
            result.task_id,
            result.robot_asset_name,
            result.asset_path,
            result.asset_sha256,
            result.action_factory_module,
            result.deck_frame,
            result.contract_id,
            result.config_id,
            result.calibration_id,
        )
        if result.schema_version != "1.0" or any(not item for item in values):
            msg = "Matterix OT-2 schema_version must be '1.0' and identity fields must not be empty"
            raise MatterixAssetConfigurationError(msg)
        if set(result.axis_joints) != _AXES or len(set(result.axis_joints.values())) != len(_AXES):
            msg = "axis_joints must map X, Y, Z, A, B, and C to unique Matterix joint names"
            raise MatterixAssetConfigurationError(msg)
        return result

    def validate_connector(self, connector: DigitalTwinConfig, registry: ContractRegistry) -> None:
        """Fail closed when the twin was prepared for another connector snapshot."""
        errors = []
        if self.contract_id != registry.snapshot.contract_id:
            errors.append(f"contract_id {self.contract_id!r} does not match {registry.snapshot.contract_id!r}")
        if self.config_id != connector.config_id:
            errors.append(f"config_id {self.config_id!r} does not match {connector.config_id!r}")
        if self.calibration_id != connector.calibration_id:
            errors.append(f"calibration_id {self.calibration_id!r} does not match {connector.calibration_id!r}")
        if errors:
            raise MatterixAssetConfigurationError("; ".join(errors))

    def validate_asset_file(self) -> Path:
        """Require an existing asset whose bytes match the pinned SHA-256."""
        if self.asset_sha256 == _PLACEHOLDER_HASH:
            msg = "Matterix asset_sha256 is still a placeholder"
            raise MatterixAssetConfigurationError(msg)
        asset = Path(self.asset_path).expanduser()
        if not asset.is_file():
            msg = f"Matterix OT-2 asset does not exist: {asset}"
            raise MatterixAssetConfigurationError(msg)
        actual = hashlib.sha256(asset.read_bytes()).hexdigest()
        if actual != self.asset_sha256:
            msg = f"Matterix asset hash {actual} does not match pinned {self.asset_sha256}"
            raise MatterixAssetConfigurationError(msg)
        return asset
