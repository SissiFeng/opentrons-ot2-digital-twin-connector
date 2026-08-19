"""Connector-owned OT-2 digital-twin unit operations."""

from __future__ import annotations

import asyncio
import collections.abc
import dataclasses
import math

from ..digital_twin.config import DeckPoint, DigitalTwinConfig, InstrumentProfile
from ..digital_twin.state import DigitalTwinStateStore, EvidenceSource, StateSnapshot, TipState
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
from .motion import OT2MotionController

_MOUNT_AXIS = {"LEFT": "Z", "RIGHT": "A"}
_PLUNGER_AXIS = {"LEFT": "B", "RIGHT": "C"}


@dataclasses.dataclass(frozen=True)
class PipetteRuntime:
    """Configured and hardware-reported identity for one pipette mount."""

    mount: str
    attached: bool
    expected_model: str
    actual_model: str
    name: str
    pipette_id: str
    channels: int
    minimum_volume: float
    maximum_volume: float

    @property
    def matches_configuration(self) -> bool:
        """Return whether attached hardware matches the pinned profile."""
        return self.attached and self.actual_model.casefold() == self.expected_model.casefold()


class OT2DigitalTwinController:
    """High-level OT-2 operations backed by one motion controller and one state store."""

    def __init__(
        self,
        motion: OT2MotionController,
        config: DigitalTwinConfig,
        state: DigitalTwinStateStore,
    ) -> None:
        self._motion = motion
        self._config = config
        self._state = state
        self._serial_number = ""
        self._firmware_version = ""
        self._pipettes: dict[str, PipetteRuntime] = {}
        self._modules: tuple[str, ...] = ()

    @classmethod
    async def build(cls, motion: OT2MotionController, config: DigitalTwinConfig) -> OT2DigitalTwinController:
        """Create the controller and capture startup identity without hiding hardware failures."""
        state = DigitalTwinStateStore(config.state_path, tuple(profile.mount for profile in config.instruments))
        controller = cls(motion=motion, config=config, state=state)
        await controller.refresh_identity()
        return controller

    @property
    def config(self) -> DigitalTwinConfig:
        """Return the immutable active configuration."""
        return self._config

    @property
    def state(self) -> DigitalTwinStateStore:
        """Return connector-owned durable state."""
        return self._state

    @property
    def serial_number(self) -> str:
        """Return the startup-captured robot serial number."""
        return self._serial_number

    @property
    def firmware_version(self) -> str:
        """Return the startup-captured Smoothie firmware version."""
        return self._firmware_version

    @property
    def pipettes(self) -> tuple[PipetteRuntime, ...]:
        """Return hardware/config identity for every configured mount."""
        return tuple(self._pipettes[profile.mount] for profile in self._config.instruments)

    @property
    def modules(self) -> tuple[str, ...]:
        """Return attached module identities captured at startup."""
        return self._modules

    @property
    def is_simulating(self) -> bool:
        """Return whether the hardware boundary is simulated."""
        return self._motion.is_simulating

    @property
    def raw_position(self) -> dict[str, float]:
        """Return a copy of cached raw-axis positions."""
        return dict(self._motion.position)

    @property
    def homed_flags(self) -> dict[str, bool]:
        """Return a copy of raw-axis homing flags."""
        return dict(self._motion.homed_flags)

    @property
    def door_closed(self) -> bool:
        """Return the current OT-2 window-switch state."""
        return self._motion.read_door_switch()

    def set_modules(self, module_identities: tuple[str, ...]) -> None:
        """Set fail-closed module identities discovered by the application entrypoint."""
        if len(module_identities) != len(set(module_identities)):
            msg = f"Duplicate module identities are ambiguous: {module_identities}"
            raise ConfigurationMismatchError(msg)
        self._modules = tuple(sorted(module_identities))

    async def refresh_identity(self) -> None:
        """Refresh robot and pipette identity from the hardware boundary."""
        self._serial_number = await self._motion.get_serial_number()
        self._firmware_version = await self._motion.get_firmware_version()
        found: dict[str, PipetteRuntime] = {}
        for profile in self._config.instruments:
            actual_model = await self._motion.read_pipette_model(profile.mount)
            pipette_id = await self._motion.read_pipette_id(profile.mount)
            if self._motion.is_simulating and not actual_model:
                actual_model = profile.expected_model
                pipette_id = f"SIMULATED-{profile.mount}"
            found[profile.mount] = PipetteRuntime(
                mount=profile.mount,
                attached=bool(actual_model),
                expected_model=profile.expected_model,
                actual_model=actual_model,
                name=profile.name,
                pipette_id=pipette_id,
                channels=profile.channels,
                minimum_volume=profile.minimum_volume,
                maximum_volume=profile.maximum_volume,
            )
        self._pipettes = found

    async def home(self) -> None:
        """Home all gantry, mount, and plunger axes."""
        try:
            await self._motion.home("XYZABC")
        except asyncio.CancelledError:
            await self.halt_after_cancel("HOME_CANCELLED")
            raise
        await self._state.record("HOME")

    async def home_with_progress(self, emit: collections.abc.Callable[[float, str], None]) -> None:
        """
        Home all axes while publishing truthful progress callbacks.

        The callback receives ``(progress, message)`` and runs after each home
        phase.  Cancellation still halts hardware and raises ``CancelledError``.
        """
        emit(0.0, "Starting full OT-2 home.")
        phases = ("XYZ", "A", "B", "C")
        total = len(phases)
        try:
            for index, phase in enumerate(phases, start=1):
                await self._motion.home(phase)
                emit(min(0.99, index / total), f"Homed {phase} axes.")
        except asyncio.CancelledError:
            await self.halt_after_cancel("HOME_CANCELLED")
            raise
        emit(1.0, "Full OT-2 home completed.")
        await self._state.record("HOME")

    async def move_to(self, mount: str, point: DeckPoint, speed: float, *, tip_reference: bool) -> DeckPoint:
        """Move one pipette mount to a calibrated logical deck point."""
        profile = self._require_ready_mount(mount)
        if tip_reference and self._state.snapshot.mount(profile.mount).tip_state is not TipState.PRESENT:
            msg = f"{profile.mount} must have a reconciled PRESENT tip before using TIP reference coordinates"
            raise TipStateError(msg)
        machine, vertical = self._machine_target(profile, point, tip_reference=tip_reference)
        self._validate_machine_target(profile.mount, machine, vertical)
        try:
            await self._motion.move(
                target={"X": machine.x, "Y": machine.y, _MOUNT_AXIS[profile.mount]: vertical},
                speed=speed,
            )
        except asyncio.CancelledError:
            await self.halt_after_cancel("MOVE_CANCELLED")
            raise
        await self._state.record("MOVE_TO")
        return self.mount_position(profile.mount)

    async def move_to_well(
        self,
        mount: str,
        labware_id: str,
        well: str,
        speed: float,
        *,
        use_approach: bool,
        tip_reference: bool,
    ) -> DeckPoint:
        """Resolve and move to a symbolic labware well."""
        try:
            point = self._config.resolve_well(labware_id, well, use_approach=use_approach)
        except ValueError as error:
            raise ConfigurationMismatchError(str(error)) from error
        return await self.move_to(mount, point, speed, tip_reference=tip_reference)

    def mount_position(self, mount: str) -> DeckPoint:
        """Return the cached nozzle position in the logical deck frame."""
        profile = self._config.instrument(mount)
        raw = self._motion.position
        return DeckPoint(
            x=float(raw.get("X", 0.0)) - self._config.deck_offset.x,
            y=float(raw.get("Y", 0.0)) - self._config.deck_offset.y,
            z=(
                float(raw.get(_MOUNT_AXIS[profile.mount], 0.0))
                - profile.nozzle_deck_axis_position
                - self._config.deck_offset.z
            ),
        )

    async def pick_up_tip(self, mount: str, labware_id: str, well: str) -> StateSnapshot:
        """Atomically move, pick up a tip, retract, and record software-tracked evidence."""
        profile = self._require_ready_mount(mount)
        current = self._state.snapshot.mount(profile.mount)
        if current.tip_state is TipState.UNKNOWN:
            msg = f"{profile.mount} tip state is UNKNOWN; reconcile physical tip presence before pickup"
            raise StateReconciliationRequiredError(msg)
        if current.tip_state is TipState.PRESENT:
            msg = f"{profile.mount} already has a tracked tip; drop or reconcile it before pickup"
            raise TipStateError(msg)
        try:
            logical = self._config.resolve_well(labware_id, well, use_approach=False)
            machine, pickup_axis = self._machine_target(profile, logical, tip_reference=False)
            safe_axis = profile.nozzle_deck_axis_position + profile.safe_travel_height + self._config.deck_offset.z
            self._validate_machine_target(profile.mount, machine, pickup_axis)
            await self._motion.pick_up_tip_at(
                mount=profile.mount,
                x=machine.x,
                y=machine.y,
                pickup_axis_position=pickup_axis,
                safe_axis_position=safe_axis,
                tip_length=profile.tip_length,
                actuation_mode=profile.tip_actuation_mode,
                presses=profile.pickup_presses,
                increment=profile.pickup_increment,
            )
        except asyncio.CancelledError:
            await self._mark_tip_unknown(profile.mount, "PICK_UP_TIP_CANCELLED")
            raise
        except (CalibrationNotConfirmedError, ConfigurationMismatchError, NotHomedError):
            raise
        except Exception as error:
            await self._mark_tip_unknown(profile.mount, "PICK_UP_TIP_FAILED", str(error))
            msg = (
                f"Tip pickup on {profile.mount} did not complete: {error}. "
                "Inspect the pipette and rack, then reconcile tip presence."
            )
            raise TipPickupError(msg) from error
        return await self._state.update_mount(
            profile.mount,
            tip_state=TipState.PRESENT,
            tip_evidence=EvidenceSource.SOFTWARE_TRACKED,
            liquid_volume=0.0,
            liquid_volume_known=True,
            operation="PICK_UP_TIP",
        )

    async def drop_tip(self, mount: str) -> StateSnapshot:
        """Atomically move to configured trash, release the tip, retract, and reconcile state."""
        profile = self._require_ready_mount(mount)
        current = self._state.snapshot.mount(profile.mount)
        if current.tip_state is TipState.UNKNOWN:
            msg = f"{profile.mount} tip state is UNKNOWN; reconcile physical tip presence before release"
            raise StateReconciliationRequiredError(msg)
        if current.tip_state is TipState.ABSENT:
            msg = f"{profile.mount} has no tracked tip to release"
            raise TipStateError(msg)
        try:
            machine, release_axis = self._machine_target(profile, self._config.trash, tip_reference=True)
            safe_axis = profile.nozzle_deck_axis_position + profile.safe_travel_height + self._config.deck_offset.z
            self._validate_machine_target(profile.mount, machine, release_axis)
            await self._motion.drop_tip_at(
                mount=profile.mount,
                x=machine.x,
                y=machine.y,
                release_axis_position=release_axis,
                safe_axis_position=safe_axis,
                actuation_mode=profile.tip_actuation_mode,
                drop_tip_plunger_position=profile.drop_tip_plunger_position,
            )
        except asyncio.CancelledError:
            await self._mark_tip_unknown(profile.mount, "DROP_TIP_CANCELLED")
            raise
        except (CalibrationNotConfirmedError, ConfigurationMismatchError, NotHomedError):
            raise
        except Exception as error:
            await self._mark_tip_unknown(profile.mount, "DROP_TIP_FAILED", str(error))
            msg = (
                f"Tip release on {profile.mount} did not complete: {error}. "
                "Inspect the pipette and trash, then reconcile tip presence."
            )
            raise TipDropError(msg) from error
        return await self._state.update_mount(
            profile.mount,
            tip_state=TipState.ABSENT,
            tip_evidence=EvidenceSource.SOFTWARE_TRACKED,
            liquid_volume=0.0,
            liquid_volume_known=True,
            operation="DROP_TIP",
        )

    async def aspirate(self, mount: str, volume: float, flow_rate: float) -> StateSnapshot:
        """Aspirate using connector-owned calibration and update tracked liquid volume."""
        profile = self._require_liquid_operation(mount, volume, flow_rate)
        current = self._state.snapshot.mount(profile.mount)
        target_volume = current.liquid_volume + volume
        if target_volume > profile.maximum_volume:
            msg = (
                f"Aspirating {volume:g} µL would raise tracked liquid to {target_volume:g} µL, "
                f"above the {profile.maximum_volume:g} µL pipette maximum"
            )
            raise LiquidVolumeOutOfRangeError(msg)
        try:
            await self._motion.aspirate(
                _PLUNGER_AXIS[profile.mount],
                volume,
                profile.microlitres_per_millimetre,
                flow_rate,
            )
        except asyncio.CancelledError:
            await self._mark_liquid_unknown(profile.mount, "ASPIRATE_CANCELLED")
            raise
        except Exception as error:
            await self._mark_liquid_unknown(profile.mount, "ASPIRATE_FAILED", str(error))
            raise
        return await self._state.update_mount(
            profile.mount,
            liquid_volume=target_volume,
            liquid_volume_known=True,
            operation="ASPIRATE",
        )

    async def dispense(self, mount: str, volume: float, flow_rate: float) -> StateSnapshot:
        """Dispense using connector-owned calibration and update tracked liquid volume."""
        profile = self._require_liquid_operation(mount, volume, flow_rate)
        current = self._state.snapshot.mount(profile.mount)
        if volume > current.liquid_volume:
            msg = f"Cannot dispense {volume:g} µL; only {current.liquid_volume:g} µL is tracked in the tip"
            raise LiquidVolumeOutOfRangeError(msg)
        try:
            await self._motion.dispense(
                _PLUNGER_AXIS[profile.mount],
                volume,
                profile.microlitres_per_millimetre,
                flow_rate,
            )
        except asyncio.CancelledError:
            await self._mark_liquid_unknown(profile.mount, "DISPENSE_CANCELLED")
            raise
        except Exception as error:
            await self._mark_liquid_unknown(profile.mount, "DISPENSE_FAILED", str(error))
            raise
        return await self._state.update_mount(
            profile.mount,
            liquid_volume=current.liquid_volume - volume,
            liquid_volume_known=True,
            operation="DISPENSE",
        )

    async def reconcile_tip(self, mount: str, present: bool) -> StateSnapshot:
        """Apply an explicit physical inspection result."""
        self._config.instrument(mount)
        return await self._state.reconcile_tip(mount, present)

    async def halt_after_cancel(self, operation: str) -> None:
        """Halt hardware after cancellation and require a new home."""
        await self._motion.stop()
        await self._state.record(operation, "Hardware halted after cancellation; home before continuing.")

    def _require_ready_mount(self, mount: str) -> InstrumentProfile:
        profile = self._config.instrument(mount)
        if not self._config.calibration_confirmed:
            msg = (
                f"Calibration {self._config.calibration_id!r} is not confirmed. "
                "Verify the deck and pipette calibration, set calibration_confirmed=true, and restart."
            )
            raise CalibrationNotConfirmedError(msg)
        runtime = self._pipettes[profile.mount]
        if not runtime.attached:
            msg = f"No pipette is reported on {profile.mount}; expected {profile.expected_model}"
            raise PipetteNotAttachedError(msg)
        if not runtime.matches_configuration:
            msg = (
                f"{profile.mount} reports pipette model {runtime.actual_model!r}, "
                f"but configuration expects {profile.expected_model!r}"
            )
            raise ConfigurationMismatchError(msg)
        required_axes = ("X", "Y", _MOUNT_AXIS[profile.mount])
        missing = tuple(axis for axis in required_axes if not self._motion.homed_flags.get(axis, False))
        if missing:
            msg = f"Axes {missing} are not homed; run Home before moving {profile.mount}"
            raise NotHomedError(msg)
        return profile

    def _require_liquid_operation(self, mount: str, volume: float, flow_rate: float) -> InstrumentProfile:
        profile = self._require_ready_mount(mount)
        if not math.isfinite(volume) or not profile.minimum_volume <= volume <= profile.maximum_volume:
            msg = (
                f"Volume must be finite and within {profile.minimum_volume:g}-"
                f"{profile.maximum_volume:g} µL for {profile.name}"
            )
            raise LiquidVolumeOutOfRangeError(msg)
        if not math.isfinite(flow_rate) or flow_rate <= 0:
            msg = "Flow rate must be finite and greater than 0 µL/s"
            raise LiquidVolumeOutOfRangeError(msg)
        current = self._state.snapshot.mount(profile.mount)
        if current.tip_state is not TipState.PRESENT:
            msg = f"{profile.mount} requires a reconciled PRESENT tip before liquid handling"
            raise TipStateError(msg)
        if not current.liquid_volume_known:
            msg = f"{profile.mount} liquid volume is unknown after interruption; replace or reconcile the tip"
            raise LiquidStateUnknownError(msg)
        return profile

    def _machine_target(
        self,
        profile: InstrumentProfile,
        point: DeckPoint,
        *,
        tip_reference: bool,
    ) -> tuple[DeckPoint, float]:
        if not self._config.deck_bounds.contains(point):
            msg = f"Logical deck point {point} is outside configured deck bounds"
            raise MovementOutOfBoundsError(msg)
        machine = self._config.to_machine_point(point)
        vertical = profile.nozzle_deck_axis_position + machine.z
        if tip_reference:
            vertical += profile.tip_length
        return machine, vertical

    def _validate_machine_target(self, mount: str, machine: DeckPoint, vertical: float) -> None:
        bounds = self._motion.axis_bounds
        values = {"X": machine.x, "Y": machine.y, _MOUNT_AXIS[mount]: vertical}
        invalid = {
            axis: value
            for axis, value in values.items()
            if not math.isfinite(value) or value > bounds[axis] or (axis in {"X", "Y"} and value < 0)
        }
        if invalid:
            msg = f"Machine target {invalid} exceeds hardware bounds {bounds}"
            raise MovementOutOfBoundsError(msg)

    async def _mark_tip_unknown(self, mount: str, operation: str, error: str = "") -> None:
        await self._state.update_mount(
            mount,
            tip_state=TipState.UNKNOWN,
            tip_evidence=EvidenceSource.UNRECONCILED,
            liquid_volume_known=False,
            operation=operation,
            error=error or "Physical tip outcome is uncertain and requires reconciliation.",
        )

    async def _mark_liquid_unknown(self, mount: str, operation: str, error: str = "") -> None:
        await self._state.update_mount(
            mount,
            liquid_volume_known=False,
            operation=operation,
            error=error or "Liquid outcome is uncertain and requires reconciliation.",
        )
