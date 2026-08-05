"""OT-2 semantic action configurations consumed by a Matterix action factory."""

from __future__ import annotations

import dataclasses
from typing import TypeAlias


@dataclasses.dataclass(frozen=True)
class HomeCfg:
    """Home all OT-2 axes."""


@dataclasses.dataclass(frozen=True)
class MoveToCfg:
    """Move one mount in the calibrated deck frame."""

    mount: str
    x: float
    y: float
    z: float
    speed: float
    reference: str


@dataclasses.dataclass(frozen=True)
class MoveToWellCfg:
    """Resolve and move to one pinned labware well."""

    mount: str
    labware_id: str
    well: str
    height: str
    speed: float
    reference: str


@dataclasses.dataclass(frozen=True)
class PickUpTipCfg:
    """Simulate the connector-owned atomic move, pickup, and retract sequence."""

    mount: str
    labware_id: str
    well: str


@dataclasses.dataclass(frozen=True)
class DropTipCfg:
    """Simulate the connector-owned configured-trash release sequence."""

    mount: str


@dataclasses.dataclass(frozen=True)
class ReconcileTipCfg:
    """Apply an operator-inspection boundary to twin state."""

    mount: str
    present: bool


@dataclasses.dataclass(frozen=True)
class AspirateCfg:
    """Simulate calibrated aspiration and tracked liquid volume."""

    mount: str
    volume: float
    flow_rate: float


@dataclasses.dataclass(frozen=True)
class DispenseCfg:
    """Simulate calibrated dispense and tracked liquid volume."""

    mount: str
    volume: float
    flow_rate: float


OT2ActionCfg: TypeAlias = (
    HomeCfg | MoveToCfg | MoveToWellCfg | PickUpTipCfg | DropTipCfg | ReconcileTipCfg | AspirateCfg | DispenseCfg
)
