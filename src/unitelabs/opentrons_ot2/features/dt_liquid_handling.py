"""Calibrated, state-aware liquid handling primitives."""

import asyncio

from unitelabs.cdk import sila

from ..io import (
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    LiquidStateUnknownError,
    LiquidVolumeOutOfRangeError,
    NotHomedError,
    OT2DigitalTwinController,
    PipetteNotAttachedError,
    TipStateError,
)
from ._digital_twin_types import (
    LiquidFlowRate,
    LiquidVolume,
    Mount,
    OperationPhase,
    OperationProgress,
    report_progress,
)
from .device_information import CATEGORY, ORIGINATOR

_LIQUID_ERRORS = [
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    NotHomedError,
    PipetteNotAttachedError,
    TipStateError,
    LiquidStateUnknownError,
    LiquidVolumeOutOfRangeError,
]


class DigitalTwinLiquidHandlingController(sila.Feature):
    """Uses connector-owned pipette calibration for volume-controlled plunger movement."""

    def __init__(self, controller: OT2DigitalTwinController) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="LiquidHandlingController",
            name="Liquid Handling Controller",
            version="1.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def aspirate(
        self,
        mount: Mount,
        volume: LiquidVolume,
        flow_rate: LiquidFlowRate,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LiquidVolume:
        """Aspirate a validated volume using the configured pipette calibration."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting aspiration.")
        try:
            snapshot = await self._controller.aspirate(mount.value, volume, flow_rate)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                "Aspiration cancelled; liquid volume is unknown.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Aspiration completed.")
        return snapshot.mount(mount.value).liquid_volume

    @sila.ObservableCommand(errors=_LIQUID_ERRORS)
    async def dispense(
        self,
        mount: Mount,
        volume: LiquidVolume,
        flow_rate: LiquidFlowRate,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> LiquidVolume:
        """Dispense a validated volume using the configured pipette calibration."""
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting dispense.")
        try:
            snapshot = await self._controller.dispense(mount.value, volume, flow_rate)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                "Dispense cancelled; liquid volume is unknown.",
            )
            raise
        report_progress(status, intermediate, 1.0, OperationPhase.COMPLETED, "Dispense completed.")
        return snapshot.mount(mount.value).liquid_volume
