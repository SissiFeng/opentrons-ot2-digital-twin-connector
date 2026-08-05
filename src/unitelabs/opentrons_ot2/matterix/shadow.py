"""Predict connector state from a Matterix plan and report typed divergences."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping

from ..digital_twin.config import DigitalTwinConfig
from .actions import (
    AspirateCfg,
    DispenseCfg,
    DropTipCfg,
    HomeCfg,
    MoveToCfg,
    MoveToWellCfg,
    PickUpTipCfg,
    ReconcileTipCfg,
)
from .workflow import MatterixWorkflowPlan


@dataclasses.dataclass
class PredictedMountState:
    """Expected connector-visible state for one mount."""

    mount: str
    position: tuple[float, float, float] | None
    homed: bool | None
    tip_presence: str | None
    tip_evidence: str | None
    liquid_volume: float | None
    liquid_volume_known: bool | None


@dataclasses.dataclass(frozen=True)
class ShadowPrediction:
    """Expected final state and source request identities."""

    mounts: tuple[PredictedMountState, ...]
    request_hashes: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class ShadowDivergence:
    """One field that differs between predicted twin and connector evidence."""

    mount: str
    field: str
    predicted: object
    actual: object
    tolerance: float | None


def predict_plan(
    plan: MatterixWorkflowPlan,
    connector_state: Mapping[str, object],
    config: DigitalTwinConfig,
) -> ShadowPrediction:
    """Apply a semantic plan to a connector state snapshot without hardware access."""
    mounts = _mount_map(connector_state)
    for action in plan.actions:
        if isinstance(action, HomeCfg):
            for mount in mounts.values():
                mount.homed = True
            continue
        mount = mounts.get(action.mount)
        if mount is None:
            msg = f"Connector state has no mount {action.mount!r}"
            raise ValueError(msg)
        profile = config.instrument(action.mount)
        if isinstance(action, MoveToCfg):
            offset = profile.tip_length if action.reference == "TIP" else 0.0
            mount.position = (action.x, action.y, action.z + offset)
        elif isinstance(action, MoveToWellCfg):
            point = config.resolve_well(
                action.labware_id,
                action.well,
                use_approach=action.height == "APPROACH",
            )
            offset = profile.tip_length if action.reference == "TIP" else 0.0
            mount.position = (point.x, point.y, point.z + offset)
        elif isinstance(action, PickUpTipCfg):
            point = config.resolve_well(action.labware_id, action.well, use_approach=False)
            mount.position = (point.x, point.y, profile.safe_travel_height)
            mount.tip_presence = "PRESENT"
            mount.tip_evidence = "SOFTWARE_TRACKED"
            mount.liquid_volume = 0.0
            mount.liquid_volume_known = True
        elif isinstance(action, DropTipCfg):
            mount.position = (config.trash.x, config.trash.y, profile.safe_travel_height)
            mount.tip_presence = "ABSENT"
            mount.tip_evidence = "SOFTWARE_TRACKED"
            mount.liquid_volume = 0.0
            mount.liquid_volume_known = True
        elif isinstance(action, ReconcileTipCfg):
            mount.tip_presence = "PRESENT" if action.present else "ABSENT"
            mount.tip_evidence = "SOFTWARE_TRACKED"
            mount.liquid_volume = 0.0
            mount.liquid_volume_known = not action.present
        elif isinstance(action, AspirateCfg):
            mount.liquid_volume = (mount.liquid_volume or 0.0) + action.volume
            mount.liquid_volume_known = True
        elif isinstance(action, DispenseCfg):
            mount.liquid_volume = (mount.liquid_volume or 0.0) - action.volume
            mount.liquid_volume_known = True
    return ShadowPrediction(
        mounts=tuple(sorted(mounts.values(), key=lambda item: item.mount)),
        request_hashes=tuple(step.request_hash for step in plan.steps),
    )


def compare_with_connector_state(
    prediction: ShadowPrediction,
    connector_state: Mapping[str, object],
    *,
    position_tolerance_mm: float = 0.5,
    volume_tolerance_ul: float = 0.5,
) -> tuple[ShadowDivergence, ...]:
    """Compare prediction with a post-run connector snapshot."""
    if position_tolerance_mm < 0 or volume_tolerance_ul < 0:
        msg = "Shadow tolerances must be non-negative"
        raise ValueError(msg)
    actual_mounts = {_text(item.get("mount")): item for item in _mapping_list(connector_state.get("mounts"), "mounts")}
    divergences: list[ShadowDivergence] = []
    for predicted in prediction.mounts:
        actual = actual_mounts.get(predicted.mount)
        if actual is None:
            divergences.append(ShadowDivergence(predicted.mount, "mount", "present", "missing", None))
            continue
        for field in ("homed", "tip_presence", "tip_evidence", "liquid_volume_known"):
            expected = getattr(predicted, field)
            observed = _normalized_value(actual.get(field))
            if expected is not None and observed != expected:
                divergences.append(ShadowDivergence(predicted.mount, field, expected, observed, None))
        if predicted.liquid_volume is not None:
            observed_volume = _finite(actual.get("liquid_volume"), "liquid_volume")
            if abs(predicted.liquid_volume - observed_volume) > volume_tolerance_ul:
                divergences.append(
                    ShadowDivergence(
                        predicted.mount,
                        "liquid_volume",
                        predicted.liquid_volume,
                        observed_volume,
                        volume_tolerance_ul,
                    )
                )
        if predicted.position is not None:
            observed_position = actual.get("position")
            if not isinstance(observed_position, Mapping):
                divergences.append(
                    ShadowDivergence(predicted.mount, "position", predicted.position, observed_position, None)
                )
            else:
                actual_xyz = tuple(_finite(observed_position.get(axis), f"position.{axis}") for axis in ("x", "y", "z"))
                distance = math.dist(predicted.position, actual_xyz)
                if distance > position_tolerance_mm:
                    divergences.append(
                        ShadowDivergence(
                            predicted.mount,
                            "position",
                            predicted.position,
                            actual_xyz,
                            position_tolerance_mm,
                        )
                    )
    return tuple(divergences)


def _mount_map(connector_state: Mapping[str, object]) -> dict[str, PredictedMountState]:
    result = {}
    for item in _mapping_list(connector_state.get("mounts"), "mounts"):
        mount = _text(item.get("mount"))
        position_value = item.get("position")
        position = None
        if isinstance(position_value, Mapping):
            position = tuple(_finite(position_value.get(axis), f"position.{axis}") for axis in ("x", "y", "z"))
        result[mount] = PredictedMountState(
            mount=mount,
            position=position,
            homed=_optional_bool(item.get("homed")),
            tip_presence=_text(item.get("tip_presence")),
            tip_evidence=_text(item.get("tip_evidence")),
            liquid_volume=_finite(item.get("liquid_volume"), "liquid_volume"),
            liquid_volume_known=_optional_bool(item.get("liquid_volume_known")),
        )
    return result


def _mapping_list(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        msg = f"Connector state field {field!r} must be a list of structures"
        raise ValueError(msg)
    return value


def _text(value: object) -> str:
    return str(getattr(value, "value", value)).rsplit(".", maxsplit=1)[-1].upper()


def _normalized_value(value: object) -> object:
    if isinstance(value, str) or hasattr(value, "value"):
        return _text(value)
    return value


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        msg = f"Connector state field {field!r} must be finite numeric"
        raise ValueError(msg)
    return float(value)
