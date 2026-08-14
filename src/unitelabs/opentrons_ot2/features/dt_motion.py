"""Mount-oriented, observable motion operations for OT-2 digital twins."""

import asyncio

from unitelabs.cdk import sila

from ..digital_twin.config import DeckPoint
from ..io import (
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    MovementOutOfBoundsError,
    NotHomedError,
    OT2DigitalTwinController,
    PipetteNotAttachedError,
    TipStateError,
)
from ._digital_twin_types import (
    Mount,
    MovementSpeed,
    OperationPhase,
    OperationProgress,
    Position,
    PositionReference,
    WellHeight,
    report_progress,
)
from .device_information import CATEGORY, ORIGINATOR

_MOTION_ERRORS = [
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    NotHomedError,
    MovementOutOfBoundsError,
    PipetteNotAttachedError,
    TipStateError,
]


class DigitalTwinMotionController(sila.Feature):
    """Controls OT-2 pipette mounts in calibrated deck coordinates."""

    def __init__(self, controller: OT2DigitalTwinController) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="MotionController",
            name="Motion Controller",
            version="2.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=_MOTION_ERRORS)
    async def home(
        self,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> None:
        """
        Home all axes and publish progress and cancellation state.

        Progress updates are published per homing phase; cancellation halts
        hardware and requires a new home before continuing.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting full OT-2 home.")
        try:
            await self._controller.home_with_progress(
                lambda progress, message: report_progress(
                    status,
                    intermediate,
                    progress,
                    OperationPhase.EXECUTING,
                    message,
                )
            )
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Home cancelled; hardware halted.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Full OT-2 home completed.")

    @sila.ObservableCommand(errors=_MOTION_ERRORS)
    async def move_to(
        self,
        mount: Mount,
        position: Position,
        speed: MovementSpeed,
        reference: PositionReference,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Position:
        """
        Move one mount to an absolute calibrated deck position.

        The operation is cancellable; cancellation halts the gantry and requires a
        new home before further movement.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, f"Starting {mount.value} move.")
        try:
            result = await self._controller.move_to(
                mount.value,
                DeckPoint(x=position.x, y=position.y, z=position.z),
                speed,
                tip_reference=reference is PositionReference.TIP,
            )
            report_progress(status, intermediate, 1.0, OperationPhase.EXECUTING, f"{mount.value} move complete.")
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Move cancelled; hardware halted.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} move completed.")
        return Position(x=result.x, y=result.y, z=result.z)

    @sila.ObservableCommand(errors=_MOTION_ERRORS)
    async def move_to_well(
        self,
        mount: Mount,
        labware_id: str,
        well: str,
        height: WellHeight,
        speed: MovementSpeed,
        reference: PositionReference,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> Position:
        """
        Resolve a symbolic labware well from the pinned configuration and move to it.

        The operation is cancellable; cancellation halts the gantry and requires a
        new home before further movement.
        """
        report_progress(
            status,
            intermediate,
            0.0,
            OperationPhase.STARTING,
            f"Resolving {labware_id}/{well} for {mount.value}.",
        )
        try:
            result = await self._controller.move_to_well(
                mount.value,
                labware_id,
                well,
                speed,
                use_approach=height is WellHeight.APPROACH,
                tip_reference=reference is PositionReference.TIP,
            )
            report_progress(status, intermediate, 1.0, OperationPhase.EXECUTING, f"{mount.value} well move complete.")
        except asyncio.CancelledError:
            report_progress(status, intermediate, 1.0, OperationPhase.CANCELLED, "Move cancelled; hardware halted.")
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, f"{mount.value} well move completed.")
        return Position(x=result.x, y=result.y, z=result.z)

    @sila.UnobservableCommand(errors=[ConfigurationMismatchError])
    def mount_position(self, mount: Mount) -> Position:
        """Return the cached calibrated nozzle position for one mount."""
        point = self._controller.mount_position(mount.value)
        return Position(x=point.x, y=point.y, z=point.z)
