"""Read-only identity and capability provider for digital-twin clients."""

from unitelabs.cdk import sila

from ..io import OT2DigitalTwinController
from ._digital_twin_types import Capability, DeviceInformation

ORIGINATOR = "io.github.sissifeng"
CATEGORY = "robots"


class DeviceInformationProvider(sila.Feature):
    """Provides immutable startup identity and connector capability metadata."""

    def __init__(self, controller: OT2DigitalTwinController, connector_version: str, contract_id: str) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="DeviceInformationProvider",
            name="Device Information Provider",
            version="1.0",
        )
        self._controller = controller
        self._connector_version = connector_version
        self._contract_id = contract_id

    def set_contract_id(self, contract_id: str) -> None:
        """Set the contract identity after all features have been registered and serialized."""
        self._contract_id = contract_id

    @sila.UnobservableProperty()
    def device_information(self) -> DeviceInformation:
        """Return robot, configuration, and connector contract identity."""
        return DeviceInformation(
            robot_serial_number=self._controller.serial_number,
            firmware_version=self._controller.firmware_version,
            connector_version=self._connector_version,
            contract_id=self._contract_id,
            config_id=self._controller.config.config_id,
            simulation=self._controller.is_simulating,
        )

    @sila.UnobservableProperty()
    def capabilities(self) -> list[Capability]:
        """Return the versioned high-level feature surface."""
        return [
            Capability(identifier="DeviceInformationProvider", version="1.0"),
            Capability(identifier="DeckConfigurationProvider", version="1.0"),
            Capability(identifier="RobotStateProvider", version="1.0"),
            Capability(identifier="MotionController", version="2.0"),
            Capability(identifier="PipetteController", version="1.0"),
            Capability(identifier="TipController", version="1.0"),
            Capability(identifier="LiquidHandlingController", version="1.0"),
        ]
