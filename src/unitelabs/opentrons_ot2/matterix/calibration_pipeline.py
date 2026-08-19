"""Automated, evidence-producing real OT-2 joint calibration pipeline."""

from __future__ import annotations

import dataclasses
import asyncio
import hashlib
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .alignment import JOINT_AXES

CALIBRATION_PLAN_SCHEMA_VERSION = "1.0"
CALIBRATION_RUN_SCHEMA_VERSION = "1.0"
_MAX_PROBE_DISTANCE_MM = 10.0
_MAX_CALIBRATION_SPEED_MM_S = 20.0
_ADDRESS_PATTERN = re.compile(r"^[^:\s]+:\d{1,5}$")


class HardwareCalibrationError(RuntimeError):
    """The calibration run is unsafe, invalid, or failed on hardware."""


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{field} must be a finite number"
        raise HardwareCalibrationError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"{field} must be finite"
        raise HardwareCalibrationError(msg)
    return result


def _optional_finite_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field)


@dataclasses.dataclass(frozen=True)
class AxisCalibrationPlan:
    """Safe probe and approved operating endpoints for one physical axis."""

    axis: str
    probe_delta_mm: float
    approved_min_mm: float | None
    approved_max_mm: float | None

    @classmethod
    def from_mapping(cls, axis: str, value: Mapping[str, object]) -> AxisCalibrationPlan:
        """Parse one strict axis plan."""
        allowed = {"probe_delta_mm", "approved_min_mm", "approved_max_mm"}
        unknown = set(value) - allowed
        if unknown:
            msg = f"axes.{axis} contains unknown fields {sorted(unknown)}"
            raise HardwareCalibrationError(msg)
        result = cls(
            axis=axis,
            probe_delta_mm=_finite_number(value.get("probe_delta_mm"), f"axes.{axis}.probe_delta_mm"),
            approved_min_mm=_optional_finite_number(value.get("approved_min_mm"), f"axes.{axis}.approved_min_mm"),
            approved_max_mm=_optional_finite_number(value.get("approved_max_mm"), f"axes.{axis}.approved_max_mm"),
        )
        result.validate()
        return result

    def validate(self) -> None:
        """Reject movement toward the positive endstop or an excessive probe."""
        if not -_MAX_PROBE_DISTANCE_MM <= self.probe_delta_mm < 0.0:
            msg = (
                f"Axis {self.axis} probe_delta_mm must be in [-{_MAX_PROBE_DISTANCE_MM}, 0); "
                "all OT-2 joint probes must move away from the positive limit switch"
            )
            raise HardwareCalibrationError(msg)
        if (
            self.approved_min_mm is not None
            and self.approved_max_mm is not None
            and self.approved_min_mm >= self.approved_max_mm
        ):
            msg = f"Axis {self.axis} approved range must satisfy min < max"
            raise HardwareCalibrationError(msg)


@dataclasses.dataclass(frozen=True)
class HardwareCalibrationPlan:
    """Reviewed inputs for a single automated hardware session."""

    schema_version: str
    home_axes: str
    movement_speed_mm_s: float
    position_tolerance_mm: float
    axes: dict[str, AxisCalibrationPlan]

    @classmethod
    def from_file(cls, path: str | Path) -> HardwareCalibrationPlan:
        """Load and validate a calibration plan from JSON."""
        plan_path = Path(path)
        try:
            value = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            msg = f"Could not read calibration plan {plan_path}: {error}"
            raise HardwareCalibrationError(msg) from error
        if not isinstance(value, Mapping):
            msg = "Calibration plan must be a JSON object"
            raise HardwareCalibrationError(msg)
        return cls.from_mapping(value)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> HardwareCalibrationPlan:
        """Parse a strict calibration plan mapping."""
        allowed = {"schema_version", "home_axes", "movement_speed_mm_s", "position_tolerance_mm", "axes"}
        unknown = set(value) - allowed
        if unknown:
            msg = f"Calibration plan contains unknown fields {sorted(unknown)}"
            raise HardwareCalibrationError(msg)
        raw_axes = value.get("axes")
        if not isinstance(raw_axes, Mapping):
            msg = "Calibration plan axes must be an object"
            raise HardwareCalibrationError(msg)
        normalized_axes = {str(axis).upper(): raw for axis, raw in raw_axes.items()}
        if set(normalized_axes) != set(JOINT_AXES):
            msg = "Calibration plan axes must contain exactly X, Y, Z, and A"
            raise HardwareCalibrationError(msg)
        axes: dict[str, AxisCalibrationPlan] = {}
        for axis in JOINT_AXES:
            raw_axis = normalized_axes[axis]
            if not isinstance(raw_axis, Mapping):
                msg = f"Calibration plan axes.{axis} must be an object"
                raise HardwareCalibrationError(msg)
            axes[axis] = AxisCalibrationPlan.from_mapping(axis, raw_axis)
        result = cls(
            schema_version=str(value.get("schema_version", "")),
            home_axes=str(value.get("home_axes", "")).upper(),
            movement_speed_mm_s=_finite_number(value.get("movement_speed_mm_s"), "movement_speed_mm_s"),
            position_tolerance_mm=_finite_number(value.get("position_tolerance_mm"), "position_tolerance_mm"),
            axes=axes,
        )
        result.validate()
        return result

    def validate(self) -> None:
        """Validate global safety constraints."""
        if self.schema_version != CALIBRATION_PLAN_SCHEMA_VERSION:
            msg = f"Calibration plan schema_version must be {CALIBRATION_PLAN_SCHEMA_VERSION!r}"
            raise HardwareCalibrationError(msg)
        if self.home_axes != "XYZA":
            msg = "Calibration plan home_axes must be exactly 'XYZA'; B/C are semantic-only in the current USD"
            raise HardwareCalibrationError(msg)
        if not 0.0 < self.movement_speed_mm_s <= _MAX_CALIBRATION_SPEED_MM_S:
            msg = f"movement_speed_mm_s must be in (0, {_MAX_CALIBRATION_SPEED_MM_S}]"
            raise HardwareCalibrationError(msg)
        if not 0.0 < self.position_tolerance_mm <= 1.0:
            msg = "position_tolerance_mm must be in (0, 1.0]"
            raise HardwareCalibrationError(msg)

    def as_mapping(self) -> dict[str, object]:
        """Return a stable JSON representation used for provenance hashing."""
        return {
            "schema_version": self.schema_version,
            "home_axes": self.home_axes,
            "movement_speed_mm_s": self.movement_speed_mm_s,
            "position_tolerance_mm": self.position_tolerance_mm,
            "axes": {
                axis: {
                    "probe_delta_mm": item.probe_delta_mm,
                    "approved_min_mm": item.approved_min_mm,
                    "approved_max_mm": item.approved_max_mm,
                }
                for axis, item in self.axes.items()
            },
        }

    @property
    def sha256(self) -> str:
        """Return the deterministic SHA-256 of the reviewed plan."""
        payload = json.dumps(self.as_mapping(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


class CalibrationRobotClient(Protocol):
    """Hardware operations required by the calibration state machine."""

    async def is_simulating(self) -> bool:
        """Return whether the remote endpoint is a simulator."""
        ...

    async def serial_number(self) -> str:
        """Return the physical robot serial number."""
        ...

    async def firmware_version(self) -> str:
        """Return the motion-controller firmware version."""
        ...

    async def board_revision(self) -> str:
        """Return the controller board revision."""
        ...

    async def device_information(self) -> dict[str, object]:
        """Return versioned connector, contract, and configuration identity."""
        ...

    async def axis_bounds(self) -> dict[str, tuple[float | None, float]]:
        """Return reported lower and upper software bounds."""
        ...

    async def home(self, axes: str) -> dict[str, float]:
        """Home the requested axes and return all positions."""
        ...

    async def get_position(self) -> dict[str, float]:
        """Read all current axis positions."""
        ...

    async def move_relative_axis(self, axis: str, delta_mm: float, speed_mm_s: float) -> dict[str, float]:
        """Move one axis by a signed delta and return all positions."""
        ...

    async def move_axis(self, axis: str, position_mm: float, speed_mm_s: float) -> dict[str, float]:
        """Move one axis to an absolute coordinate and return all positions."""
        ...

    async def emergency_stop(self) -> None:
        """Halt motion and force re-homing after a failed run."""
        ...


Checkpoint = Callable[[dict[str, object]], Awaitable[None]]


def new_evidence_bundle(
    robot_address: str,
    plan: HardwareCalibrationPlan,
    *,
    exercise_endpoints: bool,
) -> dict[str, object]:
    """Create the initial durable evidence record before touching hardware."""
    if not _ADDRESS_PATTERN.fullmatch(robot_address):
        msg = "Robot address must be HOST:PORT"
        raise HardwareCalibrationError(msg)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": CALIBRATION_RUN_SCHEMA_VERSION,
        "run_id": str(uuid.uuid4()),
        "status": "CREATED",
        "created_at": now,
        "updated_at": now,
        "robot_address": robot_address,
        "plan_sha256": plan.sha256,
        "plan": plan.as_mapping(),
        "safety_confirmations": {
            "deck_cleared": True,
            "operator_present": True,
            "emergency_stop_accessible": True,
            "exercise_approved_endpoints": exercise_endpoints,
        },
        "robot": {},
        "home_position_mm": {},
        "axis_results": {},
        "errors": [],
    }


def _position_value(position: Mapping[str, float], axis: str) -> float:
    try:
        return _finite_number(position[axis], f"position.{axis}")
    except KeyError as error:
        msg = f"Robot response omitted axis {axis}"
        raise HardwareCalibrationError(msg) from error


def _resolved_endpoints(
    axis: str,
    plan: AxisCalibrationPlan,
    bounds: Mapping[str, tuple[float | None, float]],
) -> tuple[float, float]:
    try:
        controller_min, controller_max = bounds[axis]
    except KeyError as error:
        msg = f"Robot did not report software bounds for axis {axis}"
        raise HardwareCalibrationError(msg) from error
    approved_min = plan.approved_min_mm if plan.approved_min_mm is not None else controller_min
    approved_max = plan.approved_max_mm if plan.approved_max_mm is not None else controller_max
    if approved_min is None:
        msg = (
            f"Axis {axis} has no finite controller lower bound; set axes.{axis}.approved_min_mm "
            "from the reviewed pipette/deck collision envelope before exercising endpoints"
        )
        raise HardwareCalibrationError(msg)
    if not approved_min < approved_max:
        msg = f"Axis {axis} resolved endpoint interval must satisfy min < max"
        raise HardwareCalibrationError(msg)
    if controller_min is not None and approved_min < controller_min:
        msg = f"Axis {axis} approved minimum {approved_min} is below controller minimum {controller_min}"
        raise HardwareCalibrationError(msg)
    if approved_max > controller_max:
        msg = f"Axis {axis} approved maximum {approved_max} exceeds controller maximum {controller_max}"
        raise HardwareCalibrationError(msg)
    return approved_min, approved_max


async def run_hardware_calibration(
    client: CalibrationRobotClient,
    plan: HardwareCalibrationPlan,
    evidence: dict[str, object],
    checkpoint: Checkpoint,
    *,
    exercise_endpoints: bool,
) -> dict[str, object]:
    """Run one fail-safe hardware calibration session and checkpoint every axis."""
    try:
        if await client.is_simulating():
            msg = "Calibration target reports simulation=true; physical evidence requires a real OT-2"
            raise HardwareCalibrationError(msg)

        bounds = await client.axis_bounds()
        if exercise_endpoints:
            for axis in JOINT_AXES:
                _resolved_endpoints(axis, plan.axes[axis], bounds)

        digital_twin_identity = await client.device_information()
        if digital_twin_identity.get("simulation") is not False:
            msg = "Digital-twin identity does not confirm a physical connector"
            raise HardwareCalibrationError(msg)
        serial_number = await client.serial_number()
        identity_serial = str(digital_twin_identity.get("robot_serial_number", ""))
        if identity_serial != serial_number:
            msg = (
                f"Robot serial mismatch between MotionControl ({serial_number!r}) and "
                f"DeviceInformationProvider ({identity_serial!r})"
            )
            raise HardwareCalibrationError(msg)

        evidence["status"] = "RUNNING"
        evidence["robot"] = {
            "serial_number": serial_number,
            "firmware_version": await client.firmware_version(),
            "board_revision": await client.board_revision(),
            "digital_twin_identity": digital_twin_identity,
            "reported_axis_bounds_mm": {axis: {"min": bounds[axis][0], "max": bounds[axis][1]} for axis in JOINT_AXES},
        }
        _touch(evidence)
        await checkpoint(evidence)
    except BaseException as error:
        evidence["status"] = "FAILED"
        errors = evidence.get("errors")
        if isinstance(errors, list):
            errors.append({"type": type(error).__name__, "message": str(error)})
        _touch(evidence)
        await checkpoint(evidence)
        raise

    hardware_motion_started = False
    try:
        hardware_motion_started = True
        home = await client.home(plan.home_axes)
        home_position = {axis: _position_value(home, axis) for axis in JOINT_AXES}
        evidence["home_position_mm"] = home_position
        _touch(evidence)
        await checkpoint(evidence)

        axis_results = evidence["axis_results"]
        if not isinstance(axis_results, dict):
            msg = "Evidence axis_results is not an object"
            raise HardwareCalibrationError(msg)

        for axis in JOINT_AXES:
            item = plan.axes[axis]
            reference = home_position[axis]
            before = await client.get_position()
            before_value = _position_value(before, axis)
            moved = await client.move_relative_axis(
                axis,
                item.probe_delta_mm,
                plan.movement_speed_mm_s,
            )
            after_value = _position_value(moved, axis)
            observed_delta = after_value - before_value
            delta_error = observed_delta - item.probe_delta_mm

            returned = await client.move_axis(axis, reference, plan.movement_speed_mm_s)
            returned_value = _position_value(returned, axis)
            returned_error = returned_value - reference
            probe_passed = (
                abs(delta_error) <= plan.position_tolerance_mm and abs(returned_error) <= plan.position_tolerance_mm
            )
            if not probe_passed:
                msg = (
                    f"Axis {axis} probe failed: observed delta {observed_delta:.4f} mm for command "
                    f"{item.probe_delta_mm:.4f} mm; return error {returned_error:.4f} mm"
                )
                raise HardwareCalibrationError(msg)

            endpoint_result: dict[str, object] | None = None
            if exercise_endpoints:
                approved_min, approved_max = _resolved_endpoints(axis, item, bounds)
                at_min = await client.move_axis(axis, approved_min, plan.movement_speed_mm_s)
                observed_min = _position_value(at_min, axis)
                at_max = await client.move_axis(axis, approved_max, plan.movement_speed_mm_s)
                observed_max = _position_value(at_max, axis)
                returned = await client.move_axis(axis, reference, plan.movement_speed_mm_s)
                returned_value = _position_value(returned, axis)
                endpoint_result = {
                    "approved_min_mm": approved_min,
                    "observed_min_mm": observed_min,
                    "approved_max_mm": approved_max,
                    "observed_max_mm": observed_max,
                    "returned_reference_mm": returned_value,
                    "pass": all(
                        abs(actual - expected) <= plan.position_tolerance_mm
                        for actual, expected in (
                            (observed_min, approved_min),
                            (observed_max, approved_max),
                            (returned_value, reference),
                        )
                    ),
                }
                if not endpoint_result["pass"]:
                    msg = f"Axis {axis} failed approved endpoint replay"
                    raise HardwareCalibrationError(msg)

            axis_results[axis] = {
                "reference_mm": reference,
                "before_mm": before_value,
                "commanded_delta_mm": item.probe_delta_mm,
                "after_mm": after_value,
                "observed_delta_mm": observed_delta,
                "delta_error_mm": delta_error,
                "returned_reference_mm": returned_value,
                "returned_error_mm": returned_error,
                "direction": "NEGATIVE_AWAY_FROM_POSITIVE_LIMIT_SWITCH",
                "probe_pass": True,
                "endpoints": endpoint_result,
            }
            _touch(evidence)
            await checkpoint(evidence)

        evidence["status"] = "COMPLETE"
        _touch(evidence)
        await checkpoint(evidence)
        return evidence
    except BaseException as error:
        evidence["status"] = "FAILED"
        errors = evidence.get("errors")
        if isinstance(errors, list):
            errors.append({"type": type(error).__name__, "message": str(error)})
        _touch(evidence)
        if hardware_motion_started:
            try:
                await client.emergency_stop()
            except Exception as stop_error:  # noqa: BLE001 - preserve the original hardware failure
                if isinstance(errors, list):
                    errors.append(
                        {"type": type(stop_error).__name__, "message": f"Emergency stop failed: {stop_error}"}
                    )
        await checkpoint(evidence)
        raise


def _touch(evidence: dict[str, object]) -> None:
    evidence["updated_at"] = datetime.now(timezone.utc).isoformat()


def write_evidence_bundle(path: str | Path, evidence: Mapping[str, object]) -> str:
    """Atomically write canonical JSON and its SHA-256 sidecar."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    digest = hashlib.sha256(payload).hexdigest()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{digest}  {destination.name}\n",
        encoding="utf-8",
    )
    return digest


class GrpcCalibrationRobotClient:
    """Small production gRPC client for the connector's MotionControlFeature."""

    _PACKAGE = "sila2.ca.accelerationconsortium.robots.motioncontrolfeature.v1"
    _SERVICE = f"{_PACKAGE}.MotionControlFeature"
    _DEVICE_PACKAGE = "sila2.io.github.sissifeng.robots.deviceinformationprovider.v1"
    _DEVICE_SERVICE = f"{_DEVICE_PACKAGE}.DeviceInformationProvider"

    def __init__(self, channel: object, protobuf: object) -> None:
        self._channel = channel
        self._protobuf = protobuf

    async def _call(self, method: str, params: Mapping[str, object] | None = None) -> object:
        request = await self._protobuf.encode(f"{self._PACKAGE}.{method}_Parameters", dict(params or {}))
        stub = self._channel.unary_unary(f"/{self._SERVICE}/{method}")
        response = await stub(request)
        decoded = await self._protobuf.decode(f"{self._PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def _property(self, name: str) -> object:
        method = f"Get_{name}"
        stub = self._channel.unary_unary(f"/{self._SERVICE}/{method}")
        response = await stub(b"")
        decoded = await self._protobuf.decode(f"{self._PACKAGE}.{method}_Responses", response)
        return next(iter(decoded.values()))

    async def is_simulating(self) -> bool:
        """Return the remote simulation flag."""
        return bool(await self._property("IsSimulating"))

    async def serial_number(self) -> str:
        """Read the remote robot serial number."""
        return str(await self._call("SerialNumber"))

    async def firmware_version(self) -> str:
        """Read the remote Smoothie firmware version."""
        return str(await self._call("GetFirmwareVersion"))

    async def board_revision(self) -> str:
        """Read the remote controller board revision."""
        value = await self._property("BoardRevision")
        return str(getattr(value, "value", value))

    async def device_information(self) -> dict[str, object]:
        """Read the versioned digital-twin identity from the remote connector."""
        method = "Get_DeviceInformation"
        stub = self._channel.unary_unary(f"/{self._DEVICE_SERVICE}/{method}")
        response = await stub(b"")
        decoded = await self._protobuf.decode(f"{self._DEVICE_PACKAGE}.{method}_Responses", response)
        value = next(iter(decoded.values()))
        return {
            "robot_serial_number": str(value.robot_serial_number),
            "firmware_version": str(value.firmware_version),
            "connector_version": str(value.connector_version),
            "contract_id": str(value.contract_id),
            "config_id": str(value.config_id),
            "simulation": bool(value.simulation),
        }

    async def axis_bounds(self) -> dict[str, tuple[float | None, float]]:
        """Read and normalize remote software bounds."""
        value = await self._property("AxisBounds")
        result: dict[str, tuple[float | None, float]] = {}
        for item in value:
            axis = str(getattr(item.axis, "value", item.axis)).upper()
            lower = float(item.min_mm)
            result[axis] = (lower if math.isfinite(lower) else None, float(item.max_mm))
        return result

    @staticmethod
    def _position(value: object) -> dict[str, float]:
        position = getattr(value, "position", value)
        return {axis: float(getattr(position, axis.lower())) for axis in (*JOINT_AXES, "B", "C")}

    async def home(self, axes: str) -> dict[str, float]:
        """Home the modeled physical axes."""
        return self._position(await self._call("Home", {"axes": axes}))

    async def get_position(self) -> dict[str, float]:
        """Read all remote axis positions."""
        return self._position(await self._call("GetPosition"))

    async def move_relative_axis(self, axis: str, delta_mm: float, speed_mm_s: float) -> dict[str, float]:
        """Move one remote axis by a safe signed delta."""
        from unitelabs.opentrons_ot2.features.motion_control import Axis

        return self._position(
            await self._call(
                "MoveRelativeAxis",
                {"axis": Axis(axis), "delta": delta_mm, "speed": speed_mm_s},
            )
        )

    async def move_axis(self, axis: str, position_mm: float, speed_mm_s: float) -> dict[str, float]:
        """Move one remote axis to an absolute position."""
        from unitelabs.opentrons_ot2.features.motion_control import Axis

        return self._position(
            await self._call(
                "MoveAxis",
                {"axis": Axis(axis), "position": position_mm, "speed": speed_mm_s},
            )
        )

    async def emergency_stop(self) -> None:
        """Halt all remote motion after a pipeline failure."""
        await self._call("EmergencyStop")


async def run_remote_pipeline(
    robot_address: str,
    plan: HardwareCalibrationPlan,
    evidence_path: Path,
    *,
    exercise_endpoints: bool,
) -> dict[str, object]:
    """Build the local codec, connect to the real connector, and run calibration."""
    import contextlib

    import grpc.aio
    from unitelabs.cdk import SiLAServerConfig

    from unitelabs.opentrons_ot2 import OpentronsOt2Config, create_app

    evidence = new_evidence_bundle(robot_address, plan, exercise_endpoints=exercise_endpoints)

    async def checkpoint(value: dict[str, object]) -> None:
        await asyncio.to_thread(write_evidence_bundle, evidence_path, value)

    await checkpoint(evidence)
    local_config = OpentronsOt2Config(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    generator = create_app(local_config)
    connector = await generator.__anext__()
    await connector.start()
    channel = grpc.aio.insecure_channel(robot_address)
    try:
        try:
            await asyncio.wait_for(channel.channel_ready(), timeout=15.0)
        except TimeoutError as error:
            evidence["status"] = "FAILED"
            errors = evidence.get("errors")
            if isinstance(errors, list):
                errors.append(
                    {
                        "type": "ConnectionTimeout",
                        "message": f"Connector {robot_address} was not ready within 15 seconds",
                    }
                )
            _touch(evidence)
            await checkpoint(evidence)
            message = f"Connector {robot_address} was not ready within 15 seconds"
            raise HardwareCalibrationError(message) from error
        client = GrpcCalibrationRobotClient(channel, connector.sila_server.protobuf)
        return await run_hardware_calibration(
            client,
            plan,
            evidence,
            checkpoint,
            exercise_endpoints=exercise_endpoints,
        )
    finally:
        await channel.close()
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await generator.__anext__()


def report_summary(evidence: Mapping[str, object]) -> str:
    """Return a compact operator-readable run summary."""
    robot = evidence.get("robot")
    serial = robot.get("serial_number", "") if isinstance(robot, Mapping) else ""
    results = evidence.get("axis_results")
    completed = ",".join(results) if isinstance(results, Mapping) else ""
    return (
        f"run_id={evidence.get('run_id', '')}\n"
        f"status={evidence.get('status', '')}\n"
        f"robot_serial={serial}\n"
        f"completed_axes={completed}\n"
    )


__all__ = [
    "CALIBRATION_PLAN_SCHEMA_VERSION",
    "CALIBRATION_RUN_SCHEMA_VERSION",
    "AxisCalibrationPlan",
    "CalibrationRobotClient",
    "GrpcCalibrationRobotClient",
    "HardwareCalibrationError",
    "HardwareCalibrationPlan",
    "new_evidence_bundle",
    "report_summary",
    "run_hardware_calibration",
    "run_remote_pipeline",
    "write_evidence_bundle",
]
