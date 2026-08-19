"""Atomic, evidence-qualified OT-2 tip lifecycle."""

import asyncio

from unitelabs.cdk import sila

from ..io import (
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    MovementOutOfBoundsError,
    NotHomedError,
    OT2DigitalTwinController,
    PipetteNotAttachedError,
    StateReconciliationRequiredError,
    TipDropError,
    TipPickupError,
    TipStateError,
)
from ._digital_twin_types import (
    Mount,
    OperationPhase,
    OperationProgress,
    StateEvidence,
    TipPresence,
    TipStateInformation,
    report_progress,
)
from .device_information import CATEGORY, ORIGINATOR

_TIP_ERRORS = [
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    NotHomedError,
    MovementOutOfBoundsError,
    PipetteNotAttachedError,
    TipStateError,
    StateReconciliationRequiredError,
]


class DigitalTwinTipController(sila.Feature):
    """Owns atomic move, pickup or release, and state reconciliation."""

    def __init__(self, controller: OT2DigitalTwinController) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="TipController",
            name="Tip Controller",
            version="1.0",
        )
        self._controller = controller

    @sila.ObservableCommand(errors=[*_TIP_ERRORS, TipPickupError])
    async def pick_up_tip(
        self,
        mount: Mount,
        labware_id: str,
        well: str,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TipPresence:
        """
        Move to a configured tip well, pick up, retract, and record software evidence.

        The operation is cancellable; cancellation forces physical state to UNKNOWN
        and requires reconciliation before retrying.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting atomic tip pickup.")
        try:
            snapshot = await self._controller.pick_up_tip(mount.value, labware_id, well)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                "Tip pickup cancelled; physical state is UNKNOWN.",
            )
            raise
        report_progress(
            status,
            intermediate,
            1.0,
            OperationPhase.COMPLETED,
            "Tip pickup completed with SOFTWARE_TRACKED evidence.",
        )
        return TipPresence[snapshot.mount(mount.value).tip_state.name]

    @sila.ObservableCommand(errors=[*_TIP_ERRORS, TipDropError])
    async def drop_tip(
        self,
        mount: Mount,
        *,
        status: sila.Status,
        intermediate: sila.Intermediate[OperationProgress],
    ) -> TipPresence:
        """
        Move to configured trash, release, retract, and record software evidence.

        The operation is cancellable; cancellation forces physical state to UNKNOWN
        and requires reconciliation before retrying.
        """
        report_progress(status, intermediate, 0.0, OperationPhase.STARTING, "Starting atomic tip release.")
        try:
            snapshot = await self._controller.drop_tip(mount.value)
        except asyncio.CancelledError:
            report_progress(
                status,
                intermediate,
                1.0,
                OperationPhase.CANCELLED,
                "Tip release cancelled; physical state is UNKNOWN.",
            )
            raise
        report_progress(
            status,
            intermediate,
            1.0,
            OperationPhase.COMPLETED,
            "Tip release completed with SOFTWARE_TRACKED evidence.",
        )
        return TipPresence[snapshot.mount(mount.value).tip_state.name]

    @sila.UnobservableCommand(errors=[ConfigurationMismatchError])
    async def reconcile_tip(self, mount: Mount, present: bool) -> StateEvidence:
        """Record a deliberate physical inspection of tip presence."""
        snapshot = await self._controller.reconcile_tip(mount.value, present)
        return StateEvidence[snapshot.mount(mount.value).tip_evidence.name]

    @sila.UnobservableCommand(errors=[ConfigurationMismatchError])
    def tip_state(self, mount: Mount) -> TipStateInformation:
        """Return connector-owned tip presence and its evidence source."""
        state = self._controller.state.snapshot.mount(mount.value)
        return TipStateInformation(
            mount=mount,
            tip_presence=TipPresence[state.tip_state.name],
            tip_evidence=StateEvidence[state.tip_evidence.name],
        )
