from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.bridge import (
    ContractRegistry,
    EndpointKind,
    ExecutionMode,
    OT2StepError,
    OT2WorkflowAdapter,
    WorkflowFormatError,
    WorkflowStep,
    parse_workflow,
)
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig
from unitelabs.opentrons_ot2.features._digital_twin_types import Mount, PositionReference

from tests.digital_twin.helpers import config_mapping


def _adapter(tmp_path: Path) -> OT2WorkflowAdapter:
    return OT2WorkflowAdapter.build(DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json")))


def _step(action: str, params: dict[str, object], step_id: str = "step") -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        action=action,
        params=params,
        description="test",
        phase_name="phase",
    )


def test_example_workflow_preserves_external_shape_and_adapts(tmp_path: Path) -> None:
    path = Path(__file__).parents[2] / "examples" / "workflows" / "ot2_tip_transfer.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    workflow = parse_workflow(raw)
    adapter = _adapter(tmp_path)
    assert workflow.name == raw["workflow_name"]
    assert [step.action for step in workflow.steps] == [
        item["action"] for phase in raw["phases"] for item in phase["steps"]
    ]
    assert adapter.adapt(workflow.steps[0]) is None
    commands = [adapter.adapt(step) for step in workflow.steps[1:]]
    assert [command.endpoint for command in commands if command is not None] == [
        "Home",
        "ReconcileTip",
        "PickUpTip",
        "MoveToWell",
        "Aspirate",
        "MoveToWell",
        "Dispense",
        "DropTip",
    ]
    aspirate = commands[4]
    assert aspirate is not None
    assert aspirate.parameters == {"mount": Mount.RIGHT, "volume": 50.0, "flow_rate": 94.0}
    assert aspirate.execution_mode is ExecutionMode.OBSERVABLE
    assert aspirate.side_effecting is True


def test_contract_registry_derives_exact_package_mode_and_parameters(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    command = adapter.adapt(_step("robot.get_tip_presence", {"pipette": "right_pipette"}))
    assert command is not None
    binding = adapter.registry.binding_for(command)
    assert binding.package == "sila2.io.github.sissifeng.robots.tipcontroller.v1"
    assert binding.service == "TipController"
    assert binding.wire_method == "TipState"
    assert binding.parameters == ("Mount",)
    assert binding.execution_mode is ExecutionMode.UNOBSERVABLE

    state = adapter.adapt(_step("robot.get_state", {}))
    assert state is not None
    state_binding = adapter.registry.binding_for(state)
    assert state.endpoint_kind is EndpointKind.PROPERTY
    assert state_binding.wire_method == "Get_CurrentState"


def test_adapter_resolves_aliases_and_validates_finite_values(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    command = adapter.adapt(
        _step(
            "robot.move_to",
            {
                "pipette": "p300_multi_gen2",
                "x": 10,
                "y": 20,
                "z": 30,
                "reference": "tip",
            },
        )
    )
    assert command is not None
    assert command.parameters["mount"] is Mount.RIGHT
    assert command.parameters["reference"] is PositionReference.TIP
    with pytest.raises(OT2StepError, match="must be finite"):
        adapter.adapt(
            _step(
                "robot.move_to",
                {"mount": "RIGHT", "x": float("nan"), "y": 20, "z": 30},
            )
        )


def test_unknown_robot_action_fails_but_other_devices_remain_external(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    assert adapter.adapt(_step("squidstat.run_experiment", {})) is None
    with pytest.raises(OT2StepError, match="unsupported robot action"):
        adapter.adapt(_step("robot.magic", {}))


def test_duplicate_workflow_step_ids_fail_closed() -> None:
    raw = {
        "workflow_name": "duplicate",
        "version": "1",
        "phases": [
            {
                "phase_name": "one",
                "steps": [
                    {"step_id": "same", "action": "robot.home", "params": {}},
                    {"step_id": "same", "action": "robot.home", "params": {}},
                ],
            }
        ],
    }
    with pytest.raises(WorkflowFormatError, match="globally unique"):
        parse_workflow(raw)


def test_request_hash_is_stable_and_parameter_sensitive(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    first = adapter.adapt(_step("robot.aspirate", {"pipette": "right_pipette", "volume_ul": 50}))
    second = adapter.adapt(_step("robot.aspirate", {"pipette": "right_pipette", "volume_ul": 50}))
    different = adapter.adapt(_step("robot.aspirate", {"pipette": "right_pipette", "volume_ul": 60}))
    assert first is not None and second is not None and different is not None
    assert first.request_hash == second.request_hash
    assert first.request_hash != different.request_hash


def test_registry_rejects_parameter_drift() -> None:
    registry = ContractRegistry.packaged()
    with pytest.raises(RuntimeError, match="do not match contract parameters"):
        registry.command(
            step_id="bad",
            action="robot.aspirate",
            feature_identifier="LiquidHandlingController",
            endpoint="Aspirate",
            parameters={"mount": "RIGHT", "volume": 10.0},
            side_effecting=True,
        )
