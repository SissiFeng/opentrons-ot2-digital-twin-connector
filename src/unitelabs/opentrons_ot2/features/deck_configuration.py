"""Read-only, versioned deck configuration provider."""

from unitelabs.cdk import sila

from ..io import OT2DigitalTwinController
from ._digital_twin_types import ConfigurationInformation, LabwareInformation, Position
from .device_information import CATEGORY, ORIGINATOR


class DeckConfigurationProvider(sila.Feature):
    """Provides the exact geometry and calibration identity used by unit operations."""

    def __init__(self, controller: OT2DigitalTwinController) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="DeckConfigurationProvider",
            name="Deck Configuration Provider",
            version="1.0",
        )
        self._controller = controller

    @sila.UnobservableProperty()
    def configuration_information(self) -> ConfigurationInformation:
        """Return the active configuration and calibration identity."""
        config = self._controller.config
        return ConfigurationInformation(
            schema_version=config.schema_version,
            config_revision=config.config_revision,
            config_id=config.config_id,
            deck_definition=config.deck_definition,
            calibration_id=config.calibration_id,
            calibration_confirmed=config.calibration_confirmed,
        )

    @sila.UnobservableProperty()
    def labware_placements(self) -> list[LabwareInformation]:
        """Return named labware placements without opaque JSON payloads."""
        return [
            LabwareInformation(
                labware_id=item.labware_id,
                definition_uri=item.definition_uri,
                definition_hash=item.definition_hash,
                slot=item.slot,
                origin=Position(x=item.origin.x, y=item.origin.y, z=item.origin.z),
            )
            for item in self._controller.config.labware
        ]
