"""Validated identity of the future Matterix OT-2 asset and action extension."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

from ..bridge.registry import ContractRegistry
from ..digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError
from .alignment import JOINT_AXES, JointAlignmentError, JointAlignmentProfile
from .reference_frames import ReferenceFrameAlignment, ReferenceFrameAlignmentError
from .tip_rack import TipRackBinding, TipRackBindingError, binding_for_labware

MATTERIX_ASSET_SCHEMA_VERSION = "3.0"
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
    joint_alignment: JointAlignmentProfile
    reference_frame_alignment: ReferenceFrameAlignment
    tip_rack_bindings: tuple[TipRackBinding, ...]
    contract_id: str
    config_id: str
    calibration_id: str

    @property
    def axis_joints(self) -> dict[str, str]:
        """Return the four real axes that exist as joints in the OT-2 USD."""
        return self.joint_alignment.axis_joints

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
        allowed = {
            "schema_version",
            "task_id",
            "robot_asset_name",
            "asset_path",
            "asset_sha256",
            "action_factory_module",
            "deck_frame",
            "joint_alignment",
            "reference_frame_alignment",
            "tip_rack_bindings",
            "contract_id",
            "config_id",
            "calibration_id",
        }
        unknown = set(value) - allowed
        if unknown:
            msg = f"Matterix OT-2 configuration contains unknown fields {sorted(unknown)}"
            raise MatterixAssetConfigurationError(msg)
        try:
            raw_alignment = value["joint_alignment"]
            if not isinstance(raw_alignment, dict):
                msg = "joint_alignment must be an object"
                raise TypeError(msg)
            raw_frames = value["reference_frame_alignment"]
            if not isinstance(raw_frames, dict):
                msg = "reference_frame_alignment must be an object"
                raise TypeError(msg)
            raw_bindings = value["tip_rack_bindings"]
            if not isinstance(raw_bindings, list):
                msg = "tip_rack_bindings must be an array"
                raise TypeError(msg)
            result = cls(
                schema_version=str(value["schema_version"]),
                task_id=str(value["task_id"]),
                robot_asset_name=str(value["robot_asset_name"]),
                asset_path=str(value["asset_path"]),
                asset_sha256=str(value["asset_sha256"]),
                action_factory_module=str(value["action_factory_module"]),
                deck_frame=str(value["deck_frame"]),
                joint_alignment=JointAlignmentProfile.from_mapping(raw_alignment),
                reference_frame_alignment=ReferenceFrameAlignment.from_mapping(raw_frames),
                tip_rack_bindings=tuple(
                    TipRackBinding.from_mapping(item, index=index)
                    for index, item in enumerate(raw_bindings)
                    if isinstance(item, dict)
                ),
                contract_id=str(value["contract_id"]),
                config_id=str(value["config_id"]),
                calibration_id=str(value["calibration_id"]),
            )
        except (
            JointAlignmentError,
            ReferenceFrameAlignmentError,
            TipRackBindingError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
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
        if result.schema_version != MATTERIX_ASSET_SCHEMA_VERSION or any(not item for item in values):
            msg = (
                f"Matterix OT-2 schema_version must be {MATTERIX_ASSET_SCHEMA_VERSION!r} "
                "and identity fields must not be empty"
            )
            raise MatterixAssetConfigurationError(msg)
        if len(result.tip_rack_bindings) != len(raw_bindings):
            msg = "tip_rack_bindings entries must be objects"
            raise MatterixAssetConfigurationError(msg)
        binding_labware_ids = [item.labware_id for item in result.tip_rack_bindings]
        binding_asset_names = [item.asset_name for item in result.tip_rack_bindings]
        attachment_routes = {(item.mount, item.ee_sensor_name) for item in result.tip_rack_bindings}
        if not result.tip_rack_bindings:
            msg = "Matterix OT-2 configuration requires at least one tip_rack_binding"
            raise MatterixAssetConfigurationError(msg)
        if len(binding_labware_ids) != len(set(binding_labware_ids)):
            msg = "tip_rack_bindings labware_id values must be unique"
            raise MatterixAssetConfigurationError(msg)
        if len(binding_asset_names) != len(set(binding_asset_names)):
            msg = "tip_rack_bindings asset_name values must be unique"
            raise MatterixAssetConfigurationError(msg)
        if len(attachment_routes) != 1:
            msg = "Current IsTipAttached semantics require all tip racks to share one mount and ee_sensor_name"
            raise MatterixAssetConfigurationError(msg)
        if result.deck_frame != result.reference_frame_alignment.base_from_deck.child_frame:
            msg = "deck_frame must match reference_frame_alignment.base_from_deck.child_frame"
            raise MatterixAssetConfigurationError(msg)
        return result

    def validate_joint_alignment(self) -> None:
        """Require verified zero/reference, direction, and ranges for every USD joint."""
        try:
            self.joint_alignment.require_verified(JOINT_AXES)
        except JointAlignmentError as error:
            raise MatterixAssetConfigurationError(str(error)) from error

    def validate_reference_frame_alignment(self) -> None:
        """Require an accepted world/base/deck transform chain."""
        try:
            self.reference_frame_alignment.require_verified()
        except ReferenceFrameAlignmentError as error:
            raise MatterixAssetConfigurationError(str(error)) from error

    def validate_tip_rack_bindings(self, connector: DigitalTwinConfig) -> None:
        """Require every configured tip rack to map one-to-one to nested children."""
        try:
            for binding in self.tip_rack_bindings:
                binding.validate_connector(connector)
        except (TipRackBindingError, DigitalTwinConfigurationError) as error:
            raise MatterixAssetConfigurationError(str(error)) from error

    def tip_target(self, connector: DigitalTwinConfig, labware_id: str, well: str) -> tuple[str, str]:
        """Resolve one connector well into a verified Matterix asset and child id."""
        try:
            binding = binding_for_labware(self.tip_rack_bindings, labware_id)
            valid_ids = binding.validate_connector(connector)
            normalized = well.strip().upper()
            child_id = binding.child_id(normalized)
            if child_id not in valid_ids:
                msg = f"Well {well!r} is not present in configured tip rack {labware_id!r}"
                raise TipRackBindingError(msg)
            return binding.asset_name, child_id
        except (TipRackBindingError, DigitalTwinConfigurationError) as error:
            raise MatterixAssetConfigurationError(str(error)) from error

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
