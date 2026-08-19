"""Automated real-robot calibration state-machine tests using a fake transport."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.matterix.calibration_pipeline import (
    HardwareCalibrationError,
    HardwareCalibrationPlan,
    new_evidence_bundle,
    run_hardware_calibration,
    write_evidence_bundle,
)


class FakeRobot:
    """Deterministic in-memory implementation of the production robot protocol."""

    def __init__(
        self,
        *,
        simulating: bool = False,
        delta_bias_mm: float = 0.0,
        fail_home: bool = False,
    ) -> None:
        self.simulating = simulating
        self.delta_bias_mm = delta_bias_mm
        self.fail_home = fail_home
        self.positions = {"X": 418.0, "Y": 353.0, "Z": 218.0, "A": 218.0, "B": 19.0, "C": 19.0}
        self.bounds = {
            "X": (0.0, 418.0),
            "Y": (0.0, 370.0),
            "Z": (None, 218.0),
            "A": (None, 218.0),
        }
        self.calls: list[tuple[object, ...]] = []
        self.stopped = False

    async def is_simulating(self) -> bool:
        """Return the configured transport mode."""
        return self.simulating

    async def serial_number(self) -> str:
        """Return a stable fake serial number."""
        return "OT2-TEST-001"

    async def firmware_version(self) -> str:
        """Return a stable fake firmware version."""
        return "edge-test"

    async def board_revision(self) -> str:
        """Return a stable fake board revision."""
        return "C"

    async def device_information(self) -> dict[str, object]:
        """Return stable fake connector/config provenance."""
        return {
            "robot_serial_number": "OT2-TEST-001",
            "firmware_version": "edge-test",
            "connector_version": "test",
            "contract_id": "contract-test",
            "config_id": "config-test",
            "simulation": self.simulating,
        }

    async def axis_bounds(self) -> dict[str, tuple[float | None, float]]:
        """Return the fake software bounds."""
        return copy.deepcopy(self.bounds)

    async def home(self, axes: str) -> dict[str, float]:
        """Record a home operation and restore reference positions."""
        self.calls.append(("home", axes))
        if self.fail_home:
            raise RuntimeError("partial home failure")
        self.positions.update({"X": 418.0, "Y": 353.0, "Z": 218.0, "A": 218.0})
        return copy.deepcopy(self.positions)

    async def get_position(self) -> dict[str, float]:
        """Return current fake positions."""
        self.calls.append(("position",))
        return copy.deepcopy(self.positions)

    async def move_relative_axis(self, axis: str, delta_mm: float, speed_mm_s: float) -> dict[str, float]:
        """Apply one fake relative move."""
        self.calls.append(("relative", axis, delta_mm, speed_mm_s))
        self.positions[axis] += delta_mm + self.delta_bias_mm
        return copy.deepcopy(self.positions)

    async def move_axis(self, axis: str, position_mm: float, speed_mm_s: float) -> dict[str, float]:
        """Apply one fake absolute move."""
        self.calls.append(("absolute", axis, position_mm, speed_mm_s))
        self.positions[axis] = position_mm
        return copy.deepcopy(self.positions)

    async def emergency_stop(self) -> None:
        """Record an emergency stop."""
        self.calls.append(("stop",))
        self.stopped = True


def _plan_mapping(*, with_vertical_endpoints: bool = False) -> dict[str, object]:
    vertical_min = -20.0 if with_vertical_endpoints else None
    return {
        "schema_version": "1.0",
        "home_axes": "XYZA",
        "movement_speed_mm_s": 20.0,
        "position_tolerance_mm": 0.25,
        "axes": {
            "X": {"probe_delta_mm": -10.0, "approved_min_mm": 0.0, "approved_max_mm": None},
            "Y": {"probe_delta_mm": -10.0, "approved_min_mm": 0.0, "approved_max_mm": None},
            "Z": {"probe_delta_mm": -5.0, "approved_min_mm": vertical_min, "approved_max_mm": None},
            "A": {"probe_delta_mm": -5.0, "approved_min_mm": vertical_min, "approved_max_mm": None},
        },
    }


@pytest.mark.asyncio
async def test_pipeline_homes_probes_returns_and_checkpoints_every_axis() -> None:
    plan = HardwareCalibrationPlan.from_mapping(_plan_mapping())
    evidence = new_evidence_bundle("robot.local:50051", plan, exercise_endpoints=False)
    checkpoints: list[dict[str, object]] = []

    async def checkpoint(value: dict[str, object]) -> None:
        checkpoints.append(copy.deepcopy(value))

    robot = FakeRobot()
    result = await run_hardware_calibration(
        robot,
        plan,
        evidence,
        checkpoint,
        exercise_endpoints=False,
    )

    assert result["status"] == "COMPLETE"
    assert list(result["axis_results"]) == ["X", "Y", "Z", "A"]
    assert result["robot"]["serial_number"] == "OT2-TEST-001"
    assert result["robot"]["digital_twin_identity"]["config_id"] == "config-test"
    assert robot.positions["X"] == pytest.approx(418.0)
    assert robot.positions["A"] == pytest.approx(218.0)
    assert robot.stopped is False
    assert len(checkpoints) == 7
    assert robot.calls[0] == ("home", "XYZA")


@pytest.mark.asyncio
async def test_pipeline_rejects_simulator_before_any_motion() -> None:
    plan = HardwareCalibrationPlan.from_mapping(_plan_mapping())
    evidence = new_evidence_bundle("robot.local:50051", plan, exercise_endpoints=False)
    robot = FakeRobot(simulating=True)

    async def checkpoint(_value: dict[str, object]) -> None:
        pass

    with pytest.raises(HardwareCalibrationError, match="real OT-2"):
        await run_hardware_calibration(robot, plan, evidence, checkpoint, exercise_endpoints=False)
    assert robot.calls == []


@pytest.mark.asyncio
async def test_probe_mismatch_fails_closed_and_emergency_stops() -> None:
    plan = HardwareCalibrationPlan.from_mapping(_plan_mapping())
    evidence = new_evidence_bundle("robot.local:50051", plan, exercise_endpoints=False)
    checkpoints: list[dict[str, object]] = []

    async def checkpoint(value: dict[str, object]) -> None:
        checkpoints.append(copy.deepcopy(value))

    robot = FakeRobot(delta_bias_mm=1.0)
    with pytest.raises(HardwareCalibrationError, match="Axis X probe failed"):
        await run_hardware_calibration(robot, plan, evidence, checkpoint, exercise_endpoints=False)
    assert robot.stopped is True
    assert evidence["status"] == "FAILED"
    assert evidence["errors"][0]["type"] == "HardwareCalibrationError"
    assert checkpoints[-1]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_home_failure_triggers_emergency_stop_and_durable_failure() -> None:
    plan = HardwareCalibrationPlan.from_mapping(_plan_mapping())
    evidence = new_evidence_bundle("robot.local:50051", plan, exercise_endpoints=False)
    checkpoints: list[dict[str, object]] = []

    async def checkpoint(value: dict[str, object]) -> None:
        checkpoints.append(copy.deepcopy(value))

    robot = FakeRobot(fail_home=True)
    with pytest.raises(RuntimeError, match="partial home failure"):
        await run_hardware_calibration(robot, plan, evidence, checkpoint, exercise_endpoints=False)
    assert robot.stopped is True
    assert checkpoints[-1]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_endpoint_replay_requires_reviewed_vertical_minima_before_homing() -> None:
    plan = HardwareCalibrationPlan.from_mapping(_plan_mapping())
    evidence = new_evidence_bundle("robot.local:50051", plan, exercise_endpoints=True)
    robot = FakeRobot()

    async def checkpoint(_value: dict[str, object]) -> None:
        pass

    with pytest.raises(HardwareCalibrationError, match="Axis Z has no finite controller lower bound"):
        await run_hardware_calibration(robot, plan, evidence, checkpoint, exercise_endpoints=True)
    assert robot.calls == []


@pytest.mark.asyncio
async def test_reviewed_endpoint_replay_is_automatic() -> None:
    plan = HardwareCalibrationPlan.from_mapping(_plan_mapping(with_vertical_endpoints=True))
    evidence = new_evidence_bundle("robot.local:50051", plan, exercise_endpoints=True)
    robot = FakeRobot()

    async def checkpoint(_value: dict[str, object]) -> None:
        pass

    result = await run_hardware_calibration(robot, plan, evidence, checkpoint, exercise_endpoints=True)
    assert result["status"] == "COMPLETE"
    assert result["axis_results"]["Z"]["endpoints"]["observed_min_mm"] == pytest.approx(-20.0)
    assert result["axis_results"]["A"]["endpoints"]["pass"] is True


def test_plan_rejects_probe_toward_positive_limit_switch() -> None:
    value = _plan_mapping()
    value["axes"]["X"]["probe_delta_mm"] = 5.0
    with pytest.raises(HardwareCalibrationError, match="away from the positive limit switch"):
        HardwareCalibrationPlan.from_mapping(value)


def test_evidence_writer_creates_hash_sidecar(tmp_path: Path) -> None:
    destination = tmp_path / "run.json"
    digest = write_evidence_bundle(destination, {"status": "COMPLETE", "value": 1.0})
    assert json.loads(destination.read_text(encoding="utf-8"))["status"] == "COMPLETE"
    assert destination.with_suffix(".json.sha256").read_text(encoding="utf-8").startswith(digest)
