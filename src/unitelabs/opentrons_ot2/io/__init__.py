"""
IO module using Opentrons driver layer.

This module provides thin wrappers around the existing Opentrons drivers,
avoiding reimplementation of low-level protocols.

Controllers:
- OT2MotionController: Gantry and pipette motion via SmoothieDriver
- HeaterShakerController: Heater-Shaker module
- ThermocyclerController: Thermocycler module
- TemperatureModuleController: Temperature module
- MagneticModuleController: Magnetic module

Example usage:
    from unitelabs.opentrons_ot2.io import OT2MotionController

    async def main():
        controller = await OT2MotionController.build()
        await controller.home()
        await controller.move({"X": 100, "Y": 100})
        await controller.disconnect()
"""

from ._errors import (
    COMMON_MODULE_ERRORS,
    EngageHeightOutOfRangeError,
    ModuleNotRespondingError,
    ModuleOperationError,
)
from ._types import RPM, DeviceInfo, Temperature
from .heater_shaker import HeaterShakerController
from .magnetic_module import MagneticModuleController
from .modules import AmbiguousModuleConfigurationError, scan_module_ports
from .hardware_proxy import HardwareProxy
from .motion import OT2MotionController
from .digital_twin import OT2DigitalTwinController, PipetteRuntime
from .digital_twin_errors import (
    CalibrationNotConfirmedError,
    ConfigurationMismatchError,
    LiquidStateUnknownError,
    LiquidVolumeOutOfRangeError,
    MovementOutOfBoundsError,
    NotHomedError,
    PipetteNotAttachedError,
    StateReconciliationRequiredError,
    TipDropError,
    TipPickupError,
    TipStateError,
)
from .temperature_module import TemperatureModuleController
from .thermocycler import ThermocyclerController

__all__ = [
    "COMMON_MODULE_ERRORS",
    "RPM",
    "AmbiguousModuleConfigurationError",
    "CalibrationNotConfirmedError",
    "ConfigurationMismatchError",
    "DeviceInfo",
    "EngageHeightOutOfRangeError",
    "HardwareProxy",
    "HeaterShakerController",
    "LiquidStateUnknownError",
    "LiquidVolumeOutOfRangeError",
    "MagneticModuleController",
    "ModuleNotRespondingError",
    "ModuleOperationError",
    "MovementOutOfBoundsError",
    "NotHomedError",
    "OT2DigitalTwinController",
    "OT2MotionController",
    "PipetteNotAttachedError",
    "PipetteRuntime",
    "StateReconciliationRequiredError",
    "Temperature",
    "TemperatureModuleController",
    "ThermocyclerController",
    "TipDropError",
    "TipPickupError",
    "TipStateError",
    "scan_module_ports",
]
