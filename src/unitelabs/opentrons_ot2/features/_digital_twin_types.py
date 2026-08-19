"""Shared SiLA datatypes for the versioned OT-2 digital-twin surface."""

import enum
import typing
from dataclasses import dataclass

from unitelabs.cdk import sila
from unitelabs.cdk.sila import constraints

_MILLIMETRE = constraints.Unit(
    "mm",
    [constraints.Unit.Component(constraints.Unit.SI.METER)],
    factor=0.001,
)
_MILLIMETRES_PER_SECOND = constraints.Unit(
    "mm/s",
    [
        constraints.Unit.Component(constraints.Unit.SI.METER),
        constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-1),
    ],
    factor=0.001,
)
_MICROLITRE = constraints.Unit(
    "µL",
    [constraints.Unit.Component(constraints.Unit.SI.METER, exponent=3)],
    factor=1e-9,
)
_MICROLITRES_PER_SECOND = constraints.Unit(
    "µL/s",
    [
        constraints.Unit.Component(constraints.Unit.SI.METER, exponent=3),
        constraints.Unit.Component(constraints.Unit.SI.SECOND, exponent=-1),
    ],
    factor=1e-9,
)

Millimetres = typing.Annotated[float, _MILLIMETRE]
MovementSpeed = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MILLIMETRES_PER_SECOND]
LiquidVolume = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MICROLITRE]
TrackedLiquidVolume = typing.Annotated[float, constraints.MinimalInclusive(0.0), _MICROLITRE]
LiquidFlowRate = typing.Annotated[float, constraints.MinimalExclusive(0.0), _MICROLITRES_PER_SECOND]


class Mount(enum.Enum):
    """An OT-2 pipette mount."""

    LEFT = "LEFT"
    RIGHT = "RIGHT"


class PositionReference(enum.Enum):
    """Physical point represented by a target position."""

    NOZZLE = "NOZZLE"
    TIP = "TIP"


class WellHeight(enum.Enum):
    """Configured height to use when resolving a symbolic well."""

    APPROACH = "APPROACH"
    WORKING = "WORKING"


class OperationPhase(enum.Enum):
    """Lifecycle phase for a long-running unit operation."""

    STARTING = "STARTING"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TipPresence(enum.Enum):
    """Connector-owned tip state."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


class StateEvidence(enum.Enum):
    """Evidence backing a connector state claim."""

    SOFTWARE_TRACKED = "SOFTWARE_TRACKED"
    HARDWARE_REPORTED = "HARDWARE_REPORTED"
    UNRECONCILED = "UNRECONCILED"


@dataclass
class Position:
    """A point in the calibrated OT-2 deck frame."""

    x: Millimetres
    y: Millimetres
    z: Millimetres


@dataclass
class OperationProgress:
    """Progress and recovery context for an observable unit operation."""

    phase: OperationPhase
    message: str


@dataclass
class DeviceInformation:
    """Robot and connector identity pinned at startup."""

    robot_serial_number: str
    firmware_version: str
    connector_version: str
    contract_id: str
    config_id: str
    simulation: bool


@dataclass
class Capability:
    """One versioned connector capability."""

    identifier: str
    version: str


@dataclass
class ConfigurationInformation:
    """Identity and approval state of the active deck configuration."""

    schema_version: str
    config_revision: str
    config_id: str
    deck_definition: str
    calibration_id: str
    calibration_confirmed: bool


@dataclass
class LabwareInformation:
    """A named labware placement and definition identity."""

    labware_id: str
    definition_uri: str
    definition_hash: str
    slot: str
    origin: Position


@dataclass
class PipetteInformation:
    """Configured limits and hardware identity for one pipette mount."""

    mount: Mount
    attached: bool
    configuration_matches: bool
    expected_model: str
    actual_model: str
    name: str
    pipette_id: str
    channels: int
    minimum_volume: LiquidVolume
    maximum_volume: LiquidVolume
    tip_presence: TipPresence
    tip_evidence: StateEvidence


@dataclass
class TipStateInformation:
    """Tip presence together with the evidence supporting that claim."""

    mount: Mount
    tip_presence: TipPresence
    tip_evidence: StateEvidence


@dataclass
class MountState:
    """State of one pipette mount."""

    mount: Mount
    position: Position
    homed: bool
    tip_presence: TipPresence
    tip_evidence: StateEvidence
    liquid_volume: TrackedLiquidVolume
    liquid_volume_known: bool


@dataclass
class RobotState:
    """Revisioned state snapshot suitable for bridge reconciliation."""

    revision: int
    door_closed: bool
    simulation: bool
    last_operation: str
    last_error: str
    mounts: list[MountState]
    modules: list[str]


def report_progress(
    status: sila.Status,
    intermediate: sila.Intermediate[OperationProgress],
    progress: float,
    phase: OperationPhase,
    message: str,
) -> None:
    """Publish one observable-command progress update with matching intermediate response."""
    status.update(progress=progress)
    intermediate.send(OperationProgress(phase=phase, message=message))


def report_remaining_time(
    status: sila.Status,
    intermediate: sila.Intermediate[OperationProgress],
    progress: float,
    remaining_seconds: float,
    phase: OperationPhase,
    message: str,
) -> None:
    """Publish progress plus an estimated remaining execution time."""
    import datetime

    status.update(
        progress=progress,
        remaining_time=datetime.timedelta(seconds=max(0.0, remaining_seconds)),
    )
    intermediate.send(OperationProgress(phase=phase, message=message))
