"""
Import-safe OT-2 → Matterix action translation.

This module converts connector-owned ``OT2ActionCfg`` semantic configs into
Matterix state-machine primitive configs *without* importing Isaac Lab or
``matterix_sm``.  The returned structures are plain dataclasses that serialize
to JSON; the actual ``matterix_sm`` config classes (``MoveToJointConfigCfg``,
``SemanticActionCfg``, ``WaitCfg``) are constructed lazily on a Linux Isaac
environment by ``build_ot2_action_cfg`` when ``MATTERIX_PATH`` is set.

Symbolic well references (``MoveToWellCfg``, ``PickUpTipCfg``) are resolved
through the same pinned ``DigitalTwinConfig`` the connector executes against,
so the twin mirrors the exact calibrated deck coordinates.  Dropped tips use
the configured trash position.

OT-2 axes (hardware convention):

    X  gantry right/left
    Y  gantry forward/back
    Z  LEFT mount vertical
    A  RIGHT mount vertical
    B  LEFT plunger
    C  RIGHT plunger

Moves target the active mount: aligned machine X/Y plus the mount's
vertical axis (nozzle_deck_axis_position + machine z, plus tip length when a
tip is referenced).  The legacy OT-2 USD has no B/C plunger joints, so
aspirate/dispense remain Matterix semantic operations instead of inventing
non-existent action dimensions.

Mapping summary (mirrors the PoC twin-real/twin-sim translation layer):

    Home            -> MoveToJointConfig(aligned home)   (X/Y/Z/A)
    MoveTo          -> MoveToJointConfig(target_joints)  (X/Y + mount Z/A)
    MoveToWell      -> MoveToJointConfig(target_joints)  (resolved well)
    PickUpTip       -> safe Z/A + XY + descend + attach + retract
    ReturnTip       -> safe Z/A + XY + descend + detach selected child + retract
    DropTip         -> safe Z/A + XY + descend + detach to configured trash + retract
    ReconcileTip    -> IsTipAttached=False; PRESENT requires a selected child
    Aspirate        -> LiquidVolume += volume             (B/C are semantic-only)
    Dispense        -> LiquidVolume -= volume             (B/C are semantic-only)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence
from typing import TypeAlias

from ..digital_twin.config import DeckPoint, DigitalTwinConfig
from .actions import (
    AspirateCfg,
    DispenseCfg,
    DropTipCfg,
    HomeCfg,
    MoveToCfg,
    MoveToWellCfg,
    OT2ActionCfg,
    PickUpTipCfg,
    ReconcileTipCfg,
    ReturnTipCfg,
)
from .alignment import JOINT_AXES, JointAlignmentProfile
from .tip_rack import TipRackBinding, TipRackBindingError, binding_for_labware

_MOUNT_AXIS = {"LEFT": "Z", "RIGHT": "A"}


class OT2MatterixTranslationError(ValueError):
    """An OT-2 action cannot be represented by Matterix primitives."""


@dataclass(frozen=True)
class MatterixSemantic:
    """One semantic state change for the Matterix semantic manager."""

    type: str
    asset_name: str
    value: bool | float | str
    additional_info: dict[str, float | str] = field(default_factory=dict)


@dataclass(frozen=True)
class MatterixMoveToJointConfig:
    """
    Matterix MoveToJointConfigCfg-compatible joint-space move.

    ``joint_indices`` and ``target_joint_positions`` address the four physical
    USD joints in X/Y/Z/A order; values are aligned metres.
    """

    agent_assets: str
    joint_indices: tuple[int, ...]
    target_joint_positions: tuple[float, ...]
    position_threshold: float = 0.005
    settling_time: float = 0.05


@dataclass(frozen=True)
class MatterixSemanticAction:
    """Matterix SemanticActionCfg-compatible configuration."""

    semantics: tuple[MatterixSemantic, ...]


@dataclass(frozen=True)
class MatterixWait:
    """Matterix WaitCfg-compatible configuration."""

    duration: float


MatterixActionCfg: TypeAlias = MatterixMoveToJointConfig | MatterixSemanticAction | MatterixWait


@dataclass(frozen=True)
class TranslatedAction:
    """One OT-2 semantic action as one or more Matterix primitive configs."""

    source: str
    configs: tuple[MatterixActionCfg, ...]


def _gantry_joints(
    agent_asset: str,
    machine: DeckPoint,
    vertical_axis: str,
    vertical: float,
    alignment: JointAlignmentProfile,
) -> MatterixMoveToJointConfig:
    """Build the X/Y + mount-vertical joint move for one gantry target."""
    axes = ("X", "Y", vertical_axis)
    real_positions = (machine.x, machine.y, vertical)
    return MatterixMoveToJointConfig(
        agent_assets=agent_asset,
        joint_indices=alignment.joint_indices(axes),
        target_joint_positions=tuple(
            alignment.real_to_sim(axis, position) for axis, position in zip(axes, real_positions, strict=True)
        ),
    )


def _axis_joints(
    agent_asset: str,
    axes: tuple[str, ...],
    positions: tuple[float, ...],
    alignment: JointAlignmentProfile,
) -> MatterixMoveToJointConfig:
    """Build one partial joint target in connector-axis order."""
    return MatterixMoveToJointConfig(
        agent_assets=agent_asset,
        joint_indices=alignment.joint_indices(axes),
        target_joint_positions=tuple(
            alignment.real_to_sim(axis, position) for axis, position in zip(axes, positions, strict=True)
        ),
    )


def _home_joints(agent_asset: str, alignment: JointAlignmentProfile) -> MatterixMoveToJointConfig:
    """Build a four-joint target corresponding to the real OT-2 homed pose."""
    return MatterixMoveToJointConfig(
        agent_assets=agent_asset,
        joint_indices=alignment.joint_indices(JOINT_AXES),
        target_joint_positions=alignment.home_targets(),
    )


def _tip_attachment(
    value: bool,
    agent_asset: str,
    *,
    tip_asset_name: str | None = None,
    tip_child_id: str | None = None,
) -> MatterixSemanticAction:
    """Build the PR47 ``IsTipAttached`` semantic for one nested tip."""
    if value and (not tip_asset_name or not tip_child_id):
        msg = "Attaching an OT-2 tip requires a verified tip asset and nested child id"
        raise OT2MatterixTranslationError(msg)
    additional_info = {}
    if tip_asset_name is not None:
        additional_info["tip_asset_name"] = tip_asset_name
    if tip_child_id is not None:
        additional_info["tip_child_id"] = tip_child_id
    return MatterixSemanticAction(
        semantics=(
            MatterixSemantic(
                type="IsTipAttached",
                asset_name=agent_asset,
                value=value,
                additional_info=additional_info,
            ),
        )
    )


def _tip_target(
    connector: DigitalTwinConfig,
    bindings: Sequence[TipRackBinding],
    mount: str,
    labware_id: str,
    well: str,
) -> tuple[str, str]:
    """Resolve a connector well to an accepted nested-rigid child."""
    try:
        binding = binding_for_labware(bindings, labware_id)
        if binding.mount != mount:
            msg = f"Tip rack {labware_id!r} is bound to {binding.mount}, not requested mount {mount}"
            raise TipRackBindingError(msg)
        valid_ids = binding.validate_connector(connector)
        child_id = binding.child_id(well)
    except TipRackBindingError as error:
        raise OT2MatterixTranslationError(str(error)) from error
    if child_id not in valid_ids:
        msg = f"Well {well!r} does not map to a child of tip rack {labware_id!r}"
        raise OT2MatterixTranslationError(msg)
    return binding.asset_name, child_id


def _mount_vertical(connector: DigitalTwinConfig, mount: str, machine: DeckPoint, *, tip_reference: bool) -> float:
    """Mirror the connector's ``_machine_target`` vertical axis conversion."""
    profile = connector.instrument(mount)
    vertical = profile.nozzle_deck_axis_position + machine.z
    if tip_reference:
        vertical += profile.tip_length
    return vertical


def _safe_vertical(connector: DigitalTwinConfig, mount: str) -> float:
    """Mirror the connector's safe vertical target including deck calibration."""
    profile = connector.instrument(mount)
    return profile.nozzle_deck_axis_position + profile.safe_travel_height + connector.deck_offset.z


def _tip_motion_sequence(
    *,
    connector: DigitalTwinConfig,
    alignment: JointAlignmentProfile,
    agent_asset: str,
    mount: str,
    machine: DeckPoint,
    contact_vertical: float,
    semantic: MatterixSemanticAction,
) -> tuple[MatterixActionCfg, ...]:
    """Mirror the connector's collision-safe atomic tip motion order."""
    vertical_axis = _MOUNT_AXIS[mount]
    safe = _safe_vertical(connector, mount)
    return (
        _axis_joints(agent_asset, (vertical_axis,), (safe,), alignment),
        _axis_joints(agent_asset, ("X", "Y"), (machine.x, machine.y), alignment),
        _axis_joints(agent_asset, (vertical_axis,), (contact_vertical,), alignment),
        semantic,
        _axis_joints(agent_asset, (vertical_axis,), (safe,), alignment),
    )


def translate_action(
    action: OT2ActionCfg,
    *,
    connector: DigitalTwinConfig,
    alignment: JointAlignmentProfile,
    agent_asset: str = "ot2",
    tip_rack_bindings: Sequence[TipRackBinding] = (),
) -> TranslatedAction:
    """
    Translate one connector semantic action into Matterix primitive configs.

    ``connector`` is the pinned digital-twin configuration used to resolve
    symbolic well references and instrument calibration into the calibrated
    deck frame. ``alignment`` is the verified zero/reference, direction, and
    range mapping for the pinned OT-2 USD. ``agent_asset`` names the OT-2
    articulation in the Matterix scene.
    """
    mount = str(getattr(action, "mount", "RIGHT")).upper()
    if isinstance(action, HomeCfg):
        return TranslatedAction("home", (_home_joints(agent_asset, alignment),))
    if isinstance(action, MoveToCfg):
        machine = connector.to_machine_point(DeckPoint(action.x, action.y, action.z))
        vertical = _mount_vertical(connector, mount, machine, tip_reference=action.reference == "TIP")
        return TranslatedAction(
            "move_to",
            (_gantry_joints(agent_asset, machine, _MOUNT_AXIS[mount], vertical, alignment),),
        )
    if isinstance(action, MoveToWellCfg):
        point = connector.resolve_well(
            action.labware_id,
            action.well,
            use_approach=action.height == "APPROACH",
        )
        machine = connector.to_machine_point(point)
        vertical = _mount_vertical(connector, mount, machine, tip_reference=action.reference == "TIP")
        return TranslatedAction(
            "move_to_well",
            (_gantry_joints(agent_asset, machine, _MOUNT_AXIS[mount], vertical, alignment),),
        )
    if isinstance(action, PickUpTipCfg):
        point = connector.resolve_well(action.labware_id, action.well, use_approach=False)
        machine = connector.to_machine_point(point)
        vertical = _mount_vertical(connector, mount, machine, tip_reference=False)
        tip_asset_name, tip_child_id = _tip_target(
            connector,
            tip_rack_bindings,
            mount,
            action.labware_id,
            action.well,
        )
        return TranslatedAction(
            "pick_up_tip",
            _tip_motion_sequence(
                connector=connector,
                alignment=alignment,
                agent_asset=agent_asset,
                mount=mount,
                machine=machine,
                contact_vertical=vertical,
                semantic=_tip_attachment(
                    True,
                    agent_asset,
                    tip_asset_name=tip_asset_name,
                    tip_child_id=tip_child_id,
                ),
            ),
        )
    if isinstance(action, ReturnTipCfg):
        point = connector.resolve_well(action.labware_id, action.well, use_approach=False)
        machine = connector.to_machine_point(point)
        vertical = _mount_vertical(connector, mount, machine, tip_reference=True)
        tip_asset_name, tip_child_id = _tip_target(
            connector,
            tip_rack_bindings,
            mount,
            action.labware_id,
            action.well,
        )
        return TranslatedAction(
            "return_tip",
            _tip_motion_sequence(
                connector=connector,
                alignment=alignment,
                agent_asset=agent_asset,
                mount=mount,
                machine=machine,
                contact_vertical=vertical,
                semantic=_tip_attachment(
                    False,
                    agent_asset,
                    tip_asset_name=tip_asset_name,
                    tip_child_id=tip_child_id,
                ),
            ),
        )
    if isinstance(action, DropTipCfg):
        machine = connector.to_machine_point(connector.trash)
        vertical = _mount_vertical(connector, mount, machine, tip_reference=True)
        return TranslatedAction(
            "drop_tip",
            _tip_motion_sequence(
                connector=connector,
                alignment=alignment,
                agent_asset=agent_asset,
                mount=mount,
                machine=machine,
                contact_vertical=vertical,
                semantic=_tip_attachment(False, agent_asset),
            ),
        )
    if isinstance(action, ReconcileTipCfg):
        if action.present:
            msg = (
                "ReconcileTip(present=True) cannot select a physical nested tip; "
                "use PickUpTip with labware_id and well, or reconcile ABSENT"
            )
            raise OT2MatterixTranslationError(msg)
        return TranslatedAction("reconcile_tip", (_tip_attachment(False, agent_asset),))
    if isinstance(action, AspirateCfg):
        return TranslatedAction(
            "aspirate",
            (
                MatterixSemanticAction(
                    semantics=(
                        MatterixSemantic(
                            type="LiquidVolume",
                            asset_name=f"pipette_{mount.lower()}",
                            value=float(action.volume),
                            additional_info={"operation": "add"},
                        ),
                    )
                ),
            ),
        )
    if isinstance(action, DispenseCfg):
        return TranslatedAction(
            "dispense",
            (
                MatterixSemanticAction(
                    semantics=(
                        MatterixSemantic(
                            type="LiquidVolume",
                            asset_name=f"pipette_{mount.lower()}",
                            value=float(action.volume),
                            additional_info={"operation": "subtract"},
                        ),
                    )
                ),
            ),
        )
    msg = f"Unsupported OT-2 action {type(action).__name__}"
    raise OT2MatterixTranslationError(msg)


def translate_sequence(
    actions: tuple[OT2ActionCfg, ...],
    *,
    connector: DigitalTwinConfig,
    alignment: JointAlignmentProfile,
    agent_asset: str = "ot2",
    tip_rack_bindings: Sequence[TipRackBinding] = (),
) -> tuple[TranslatedAction, ...]:
    """Translate a workflow's OT-2 semantic actions in source order."""
    return tuple(
        translate_action(
            action,
            connector=connector,
            alignment=alignment,
            agent_asset=agent_asset,
            tip_rack_bindings=tip_rack_bindings,
        )
        for action in actions
    )


__all__ = [
    "MatterixActionCfg",
    "MatterixMoveToJointConfig",
    "MatterixSemantic",
    "MatterixSemanticAction",
    "MatterixWait",
    "OT2MatterixTranslationError",
    "TranslatedAction",
    "translate_action",
    "translate_sequence",
]
