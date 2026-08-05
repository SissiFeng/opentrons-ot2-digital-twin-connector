from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.bridge import (
    ContractRegistry,
    OT2BridgeExecutor,
    OT2PreflightError,
    OT2WorkflowAdapter,
    PreflightExpectation,
    WorkflowStep,
)
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig

from tests.digital_twin.helpers import config_mapping


class FakeTransport:
    """State-revisioning transport with an injectable one-shot side-effect failure."""

    def __init__(self, config: DigitalTwinConfig, registry: ContractRegistry) -> None:
        self.config = config
        self.registry = registry
        self.revision = 7
        self.calls = []
        self.fail_side_effect = False

    async def execute(self, command):
        self.calls.append(command)
        if command.feature_identifier == "DeviceInformationProvider":
            return {
                "robot_serial_number": "SIM-OT2-001",
                "contract_id": self.registry.snapshot.contract_id,
                "config_id": self.config.config_id,
                "simulation": True,
            }
        if command.feature_identifier == "DeckConfigurationProvider":
            return {
                "config_id": self.config.config_id,
                "calibration_id": self.config.calibration_id,
                "calibration_confirmed": True,
            }
        if command.feature_identifier == "PipetteController":
            return [
                {
                    "mount": "RIGHT",
                    "attached": True,
                    "configuration_matches": True,
                    "actual_model": "p300_multi_v2.1",
                }
            ]
        if command.feature_identifier == "RobotStateProvider":
            return {"revision": self.revision, "simulation": True}
        if command.side_effecting:
            if self.fail_side_effect:
                raise RuntimeError("injected actuator failure")
            self.revision += 1
            return {"result": "ok"}
        return {}


def _components(tmp_path: Path):
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    adapter = OT2WorkflowAdapter.build(config)
    transport = FakeTransport(config, adapter.registry)
    executor = OT2BridgeExecutor(transport, adapter, tmp_path / "audit.jsonl")
    expected = PreflightExpectation(
        contract_id=adapter.registry.snapshot.contract_id,
        config_id=config.config_id,
        calibration_id=config.calibration_id,
        robot_serial_number="SIM-OT2-001",
        pipette_models={"RIGHT": "p300_multi_v2.1"},
    )
    return adapter, transport, executor, expected


def _step(action: str = "robot.home") -> WorkflowStep:
    return WorkflowStep(
        step_id="side_effect",
        action=action,
        params={},
        description="test",
        phase_name="phase",
    )


@pytest.mark.asyncio
async def test_preflight_mismatch_blocks_side_effect(tmp_path: Path) -> None:
    _adapter, transport, executor, expected = _components(tmp_path)
    wrong = PreflightExpectation(
        contract_id="wrong",
        config_id=expected.config_id,
        calibration_id=expected.calibration_id,
        robot_serial_number=expected.robot_serial_number,
        pipette_models=expected.pipette_models,
    )
    with pytest.raises(OT2PreflightError, match="contract_id"):
        await executor.preflight(wrong)
    calls_after_preflight = len(transport.calls)
    with pytest.raises(OT2PreflightError, match="Successful preflight"):
        await executor.execute_step(_step())
    assert len(transport.calls) == calls_after_preflight


@pytest.mark.asyncio
async def test_successful_side_effect_records_pre_and_post_state(tmp_path: Path) -> None:
    _adapter, _transport, executor, expected = _components(tmp_path)
    report = await executor.preflight(expected)
    response = await executor.execute_step(_step())
    assert response == {"result": "ok"}
    assert report.state_revision == 7
    records = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["state_before"]["revision"] == 7
    assert records[0]["state_after"]["revision"] == 8
    assert len(records[0]["request_hash"]) == 64
    assert records[0]["error"] == ""


@pytest.mark.asyncio
async def test_failed_side_effect_is_not_retried_and_is_audited(tmp_path: Path) -> None:
    _adapter, transport, executor, expected = _components(tmp_path)
    await executor.preflight(expected)
    transport.fail_side_effect = True
    with pytest.raises(RuntimeError, match="injected actuator failure"):
        await executor.execute_step(_step())
    attempts = [command for command in transport.calls if command.side_effecting and command.endpoint == "Home"]
    assert len(attempts) == 1
    record = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
    assert "injected actuator failure" in record["error"]
    assert record["state_after"]["revision"] == 7
