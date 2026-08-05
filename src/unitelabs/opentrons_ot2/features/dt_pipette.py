"""Rich read-only pipette inventory for digital-twin clients."""

from unitelabs.cdk import sila

from ..io import OT2DigitalTwinController
from ._digital_twin_types import (
    Mount,
    PipetteInformation,
    StateEvidence,
    TipPresence,
)
from .device_information import CATEGORY, ORIGINATOR


class DigitalTwinPipetteController(sila.Feature):
    """Provides configured limits, hardware identity, and evidence-qualified tip state."""

    def __init__(self, controller: OT2DigitalTwinController) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="PipetteController",
            name="Pipette Controller",
            version="1.0",
        )
        self._controller = controller

    @sila.UnobservableProperty()
    def pipettes(self) -> list[PipetteInformation]:
        """Return one structured record for every configured mount."""
        state = self._controller.state.snapshot
        return [
            PipetteInformation(
                mount=Mount[item.mount],
                attached=item.attached,
                configuration_matches=item.matches_configuration,
                expected_model=item.expected_model,
                actual_model=item.actual_model,
                name=item.name,
                pipette_id=item.pipette_id,
                channels=item.channels,
                minimum_volume=item.minimum_volume,
                maximum_volume=item.maximum_volume,
                tip_presence=TipPresence[state.mount(item.mount).tip_state.name],
                tip_evidence=StateEvidence[state.mount(item.mount).tip_evidence.name],
            )
            for item in self._controller.pipettes
        ]
