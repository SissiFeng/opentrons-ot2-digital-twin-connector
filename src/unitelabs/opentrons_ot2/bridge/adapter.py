"""Adapt external ``robot.*`` steps to the pinned OT-2 SiLA contract."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError, InstrumentProfile
from ..features._digital_twin_types import Mount, Position, PositionReference, WellHeight
from .models import EndpointKind, OT2Command, WorkflowStep
from .registry import ContractRegistry

_SETUP_ACTIONS = frozenset(
    {
        "robot.load_labware",
        "robot.load_custom_labware",
        "robot.load_pipettes",
        "robot.set_lights",
    }
)


class OT2StepError(ValueError):
    """An external robot step cannot be represented by the connector contract."""

    def __init__(self, step_id: str, reason: str) -> None:
        self.step_id = step_id
        self.reason = reason
        super().__init__(f"OT-2 step {step_id!r}: {reason}")


@dataclass(frozen=True)
class OT2WorkflowAdapter:
    """Compile external workflow steps while keeping setup configuration declarative."""

    config: DigitalTwinConfig
    registry: ContractRegistry

    @classmethod
    def build(cls, config: DigitalTwinConfig) -> OT2WorkflowAdapter:
        """Build an adapter pinned to the connector's packaged contract."""
        return cls(config=config, registry=ContractRegistry.packaged())

    def adapt(self, step: WorkflowStep) -> OT2Command | None:
        """Translate one robot step; leave other devices with the orchestrator."""
        if step.device != "robot":
            return None
        if step.action in _SETUP_ACTIONS:
            return None
        handler = {
            "robot.home": self._home,
            "robot.move_to": self._move_to,
            "robot.move_to_well": self._move_to_well,
            "robot.get_position": self._get_position,
            "robot.pick_up_tip": self._pick_up_tip,
            "robot.drop_tip": self._drop_tip,
            "robot.get_tip_presence": self._get_tip_presence,
            "robot.reconcile_tip": self._reconcile_tip,
            "robot.aspirate": self._aspirate,
            "robot.dispense": self._dispense,
            "robot.get_state": self._get_state,
            "robot.get_machine_status": self._get_state,
            "robot.get_device_information": self._get_device_information,
            "robot.get_configuration": self._get_configuration,
            "robot.get_attached_pipettes": self._get_attached_pipettes,
        }.get(step.action)
        if handler is None:
            raise OT2StepError(step.step_id, f"unsupported robot action {step.action!r}")
        return handler(step)

    def _command(
        self,
        step: WorkflowStep,
        feature: str,
        endpoint: str,
        parameters: dict[str, object],
        *,
        side_effecting: bool,
        kind: EndpointKind = EndpointKind.COMMAND,
    ) -> OT2Command:
        return self.registry.command(
            step_id=step.step_id,
            action=step.action,
            feature_identifier=feature,
            endpoint=endpoint,
            parameters=parameters,
            kind=kind,
            side_effecting=side_effecting,
        )

    def _property(self, step: WorkflowStep, feature: str, endpoint: str) -> OT2Command:
        return self._command(
            step,
            feature,
            endpoint,
            {},
            side_effecting=False,
            kind=EndpointKind.PROPERTY,
        )

    def _home(self, step: WorkflowStep) -> OT2Command:
        return self._command(step, "MotionController", "Home", {}, side_effecting=True)

    def _move_to(self, step: WorkflowStep) -> OT2Command:
        return self._command(
            step,
            "MotionController",
            "MoveTo",
            {
                "mount": self._mount(step),
                "position": Position(
                    x=self._number(step, "x"),
                    y=self._number(step, "y"),
                    z=self._number(step, "z"),
                ),
                "speed": self._nonnegative(step, "speed_mm_s", fallback="speed", default=0.0),
                "reference": PositionReference[self._choice(step, "reference", {"NOZZLE", "TIP"}, "NOZZLE")],
            },
            side_effecting=True,
        )

    def _move_to_well(self, step: WorkflowStep) -> OT2Command:
        labware_id = self._labware(step)
        well = self._string(step, "well")
        try:
            self.config.resolve_well(labware_id, well, use_approach=False)
        except DigitalTwinConfigurationError as error:
            raise OT2StepError(step.step_id, str(error)) from error
        return self._command(
            step,
            "MotionController",
            "MoveToWell",
            {
                "mount": self._mount(step),
                "labware_id": labware_id,
                "well": well,
                "height": WellHeight[self._choice(step, "height", {"APPROACH", "WORKING"}, "WORKING")],
                "speed": self._nonnegative(step, "speed_mm_s", fallback="speed", default=0.0),
                "reference": PositionReference[self._choice(step, "reference", {"NOZZLE", "TIP"}, "TIP")],
            },
            side_effecting=True,
        )

    def _get_position(self, step: WorkflowStep) -> OT2Command:
        return self._command(
            step,
            "MotionController",
            "MountPosition",
            {"mount": self._mount(step)},
            side_effecting=False,
        )

    def _pick_up_tip(self, step: WorkflowStep) -> OT2Command:
        labware_id = self._labware(step)
        well = self._string(step, "well")
        try:
            self.config.resolve_well(labware_id, well, use_approach=False)
        except DigitalTwinConfigurationError as error:
            raise OT2StepError(step.step_id, str(error)) from error
        return self._command(
            step,
            "TipController",
            "PickUpTip",
            {"mount": self._mount(step), "labware_id": labware_id, "well": well},
            side_effecting=True,
        )

    def _drop_tip(self, step: WorkflowStep) -> OT2Command:
        return self._command(
            step,
            "TipController",
            "DropTip",
            {"mount": self._mount(step)},
            side_effecting=True,
        )

    def _get_tip_presence(self, step: WorkflowStep) -> OT2Command:
        return self._command(
            step,
            "TipController",
            "TipState",
            {"mount": self._mount(step)},
            side_effecting=False,
        )

    def _reconcile_tip(self, step: WorkflowStep) -> OT2Command:
        present = step.params.get("present")
        if not isinstance(present, bool):
            raise OT2StepError(step.step_id, "'present' must be boolean")
        return self._command(
            step,
            "TipController",
            "ReconcileTip",
            {"mount": self._mount(step), "present": present},
            side_effecting=True,
        )

    def _aspirate(self, step: WorkflowStep) -> OT2Command:
        profile = self._instrument(step)
        return self._liquid(step, "Aspirate", Mount[profile.mount], profile.default_aspirate_flow_rate)

    def _dispense(self, step: WorkflowStep) -> OT2Command:
        profile = self._instrument(step)
        return self._liquid(step, "Dispense", Mount[profile.mount], profile.default_dispense_flow_rate)

    def _liquid(self, step: WorkflowStep, endpoint: str, mount: Mount, default_flow_rate: float) -> OT2Command:
        volume = self._number_alias(step, "volume_ul", "volume")
        if volume <= 0:
            raise OT2StepError(step.step_id, "volume_ul must be greater than zero")
        flow_rate = self._number_alias(step, "flow_rate_ul_s", "flow_rate", default=default_flow_rate)
        if flow_rate <= 0:
            raise OT2StepError(step.step_id, "flow_rate_ul_s must be greater than zero")
        return self._command(
            step,
            "LiquidHandlingController",
            endpoint,
            {"mount": mount, "volume": volume, "flow_rate": flow_rate},
            side_effecting=True,
        )

    def _get_state(self, step: WorkflowStep) -> OT2Command:
        return self._property(step, "RobotStateProvider", "CurrentState")

    def _get_device_information(self, step: WorkflowStep) -> OT2Command:
        return self._property(step, "DeviceInformationProvider", "DeviceInformation")

    def _get_configuration(self, step: WorkflowStep) -> OT2Command:
        return self._property(step, "DeckConfigurationProvider", "ConfigurationInformation")

    def _get_attached_pipettes(self, step: WorkflowStep) -> OT2Command:
        return self._property(step, "PipetteController", "Pipettes")

    def _mount(self, step: WorkflowStep) -> Mount:
        return Mount[self._instrument(step).mount]

    def _instrument(self, step: WorkflowStep) -> InstrumentProfile:
        raw_mount = step.params.get("mount")
        if raw_mount is not None:
            if not isinstance(raw_mount, str):
                raise OT2StepError(step.step_id, "'mount' must be LEFT or RIGHT")
            try:
                return self.config.instrument(raw_mount)
            except DigitalTwinConfigurationError as error:
                raise OT2StepError(step.step_id, str(error)) from error
        for key in ("pipette", "instrument", "pipette_name"):
            alias = step.params.get(key)
            if alias is not None:
                if not isinstance(alias, str) or not alias:
                    raise OT2StepError(step.step_id, f"{key!r} must be a non-empty string")
                try:
                    return self.config.instrument_for_alias(alias)
                except DigitalTwinConfigurationError as error:
                    raise OT2StepError(step.step_id, str(error)) from error
        if len(self.config.instruments) == 1:
            return self.config.instruments[0]
        raise OT2StepError(step.step_id, "requires 'mount' or a configured pipette alias")

    def _labware(self, step: WorkflowStep) -> str:
        key = "labware_id" if "labware_id" in step.params else "labware"
        value = self._string(step, key)
        try:
            self.config.placement(value)
        except DigitalTwinConfigurationError as error:
            raise OT2StepError(step.step_id, str(error)) from error
        return value

    @staticmethod
    def _string(step: WorkflowStep, key: str) -> str:
        value = step.params.get(key)
        if not isinstance(value, str) or not value:
            raise OT2StepError(step.step_id, f"requires non-empty string {key!r}")
        return value

    def _number(self, step: WorkflowStep, key: str) -> float:
        if key not in step.params:
            raise OT2StepError(step.step_id, f"requires numeric {key!r}")
        return self._finite(step, key, step.params[key])

    def _number_alias(
        self,
        step: WorkflowStep,
        preferred: str,
        fallback: str,
        *,
        default: float | None = None,
    ) -> float:
        if preferred in step.params:
            return self._finite(step, preferred, step.params[preferred])
        if fallback in step.params:
            return self._finite(step, fallback, step.params[fallback])
        if default is not None:
            return default
        raise OT2StepError(step.step_id, f"requires numeric {preferred!r} or {fallback!r}")

    def _nonnegative(self, step: WorkflowStep, preferred: str, *, fallback: str, default: float) -> float:
        value = self._number_alias(step, preferred, fallback, default=default)
        if value < 0:
            raise OT2StepError(step.step_id, f"{preferred!r} must be non-negative")
        return value

    @staticmethod
    def _finite(step: WorkflowStep, key: str, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OT2StepError(step.step_id, f"{key!r} must be numeric")
        number = float(value)
        if not math.isfinite(number):
            raise OT2StepError(step.step_id, f"{key!r} must be finite")
        return number

    @staticmethod
    def _choice(step: WorkflowStep, key: str, allowed: set[str], default: str) -> str:
        raw = step.params.get(key, default)
        if not isinstance(raw, str) or raw.upper() not in allowed:
            raise OT2StepError(step.step_id, f"{key!r} must be one of {sorted(allowed)}")
        return raw.upper()
