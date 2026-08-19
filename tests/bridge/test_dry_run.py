"""Physical-feasibility dry-run gate for SIM_FIRST_THEN_REAL mode."""

from __future__ import annotations

from pathlib import Path


from unitelabs.opentrons_ot2.bridge import (
    OT2WorkflowAdapter,
    WorkflowStep,
)
from unitelabs.opentrons_ot2.bridge.dry_run import dry_run_workflow
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig

from tests.digital_twin.helpers import config_mapping


def _config(tmp_path: Path) -> DigitalTwinConfig:
    return DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))


def _step(action: str, params: dict[str, object], step_id: str = "step") -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        action=action,
        params=params,
        description="test",
        phase_name="phase",
    )


def _adapter(tmp_path: Path) -> OT2WorkflowAdapter:
    return OT2WorkflowAdapter.build(_config(tmp_path))


def _home(tmp_path: Path) -> WorkflowStep:
    return _step("robot.home", {}, "home")


def _move_to_well(tmp_path: Path, **overrides: object) -> WorkflowStep:
    params: dict[str, object] = {
        "pipette": "right_pipette",
        "labware": "source_plate",
        "well": "A1",
        "height": "WORKING",
    }
    params.update(overrides)
    return _step("robot.move_to_well", params, "move")


def test_ok_workflow_passes_gate(tmp_path: Path) -> None:
    result = dry_run_workflow(_config(tmp_path), _adapter(tmp_path).registry, [])
    assert result.ok
    assert result.reason is None
    assert result.failed_step_index is None


def test_ok_workflow_passes_gate_with_steps(tmp_path: Path) -> None:
    config = _config(tmp_path)
    adapter = _adapter(tmp_path)
    steps = [
        _home(tmp_path),
        _move_to_well(tmp_path),
        _step("robot.pick_up_tip", {"pipette": "right_pipette", "labware": "tips_300", "well": "A1"}, "pick"),
        _step("robot.aspirate", {"pipette": "right_pipette", "volume_ul": 50}, "asp"),
        _step("robot.dispense", {"pipette": "right_pipette", "volume_ul": 50}, "disp"),
    ]
    result = dry_run_workflow(config, adapter, steps)
    assert result.ok
    assert result.error_class is None
    assert result.failed_step_index is None


def test_unknown_well_fails_gate(tmp_path: Path) -> None:
    steps = [_move_to_well(tmp_path, well="ZZ99")]
    result = dry_run_workflow(_config(tmp_path), _adapter(tmp_path), steps)
    assert not result.ok
    assert result.error_class == "PhysicalInfeasibilityError"
    assert "Unknown well" in (result.reason or "")
    assert result.failed_step_index == 0


def test_out_of_bounds_position_fails_gate(tmp_path: Path) -> None:
    steps = [
        _step(
            "robot.move_to",
            {"pipette": "right_pipette", "x": 9999, "y": 0, "z": 0},
            "far",
        )
    ]
    result = dry_run_workflow(_config(tmp_path), _adapter(tmp_path), steps)
    assert not result.ok
    assert result.error_class == "PhysicalInfeasibilityError"
    assert "outside deck bounds" in (result.reason or "")
    assert result.failed_step_index == 0


def test_volume_exceeds_instrument_envelope_fails_gate(tmp_path: Path) -> None:
    steps = [
        _step(
            "robot.aspirate",
            {"pipette": "right_pipette", "volume_ul": 5000},
            "asp",
        )
    ]
    result = dry_run_workflow(_config(tmp_path), _adapter(tmp_path), steps)
    assert not result.ok
    assert "envelope" in (result.reason or "")
    assert result.failed_step_index == 0


def test_unknown_mount_fails_gate(tmp_path: Path) -> None:
    steps = [_move_to_well(tmp_path, mount="LEFT")]
    result = dry_run_workflow(_config(tmp_path), _adapter(tmp_path), steps)
    assert not result.ok
    assert "No instrument profile" in (result.reason or "")
    assert result.failed_step_index == 0
