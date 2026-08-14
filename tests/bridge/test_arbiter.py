"""Arbiter mode dispatch, sim-first gating, and shadow divergence."""

from __future__ import annotations

from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.bridge import (
    ContractRegistry,
    OT2BridgeExecutor,
    OT2ExecutionArbiter,
    OT2WorkflowAdapter,
    PreflightExpectation,
    RunMode,
    WorkflowStep,
    default_dry_run,
)
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig

from tests.digital_twin.helpers import config_mapping


class FakeTransport:
    """Replayable transport with mutable tip/liquid state."""

    def __init__(self, config: DigitalTwinConfig, registry: ContractRegistry) -> None:
        self.config = config
        self.registry = registry
        self.revision = 0
        self.calls: list[object] = []
        self.tip_presence = "ABSENT"
        self.liquid_volume = 0.0

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
        if command.feature_identifier == "RobotStateProvider" and command.endpoint == "CurrentState":
            return {
                "revision": self.revision,
                "simulation": True,
                "mounts": [
                    {
                        "mount": "RIGHT",
                        "tip_presence": self.tip_presence,
                        "tip_evidence": "SOFTWARE_TRACKED",
                        "liquid_volume": self.liquid_volume,
                        "liquid_volume_known": True,
                        "homed": True,
                    }
                ],
            }
        if command.endpoint in ("PickUpTip", "Home", "MoveTo", "MoveToWell", "Aspirate", "Dispense"):
            self.revision += 1
            if command.endpoint == "PickUpTip":
                self.tip_presence = "PRESENT"
            if command.endpoint == "DropTip":
                self.tip_presence = "ABSENT"
            if command.endpoint == "Aspirate":
                self.liquid_volume = 50.0
            return {"revision": self.revision}
        return {"ok": True}


def _executor(config: DigitalTwinConfig, transport: FakeTransport, tmp_path: Path) -> OT2BridgeExecutor:
    adapter = OT2WorkflowAdapter.build(config)
    return OT2BridgeExecutor(transport, adapter, tmp_path / "audit.jsonl")


def _steps(config: DigitalTwinConfig) -> list[WorkflowStep]:
    return [
        WorkflowStep("home", "robot.home", {}, "home", "phase"),
        WorkflowStep(
            "pick",
            "robot.pick_up_tip",
            {"pipette": "right_pipette", "labware": "tips_300", "well": "A1"},
            "pick",
            "phase",
        ),
    ]


async def _expectation(config: DigitalTwinConfig, registry: ContractRegistry) -> PreflightExpectation:
    return PreflightExpectation(
        contract_id=registry.snapshot.contract_id,
        config_id=config.config_id,
        calibration_id=config.calibration_id,
        robot_serial_number="SIM-OT2-001",
        pipette_models={"RIGHT": "p300_multi_v2.1"},
    )


@pytest.mark.asyncio
async def test_sim_first_then_real_runs_real_after_sim_gate(tmp_path: Path):
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    registry = ContractRegistry.packaged()
    sim = _executor(config, FakeTransport(config, registry), tmp_path)
    real = _executor(config, FakeTransport(config, registry), tmp_path)
    arbiter = OT2ExecutionArbiter(real, sim, mode=RunMode.SIM_FIRST_THEN_REAL, dry_run_fn=default_dry_run)
    result = await arbiter.run(_parsed(tmp_path), await _expectation(config, registry))
    assert result.ok
    assert result.preflight is not None
    assert result.dry_run is not None and result.dry_run.ok
    assert len(result.real_responses) == 2


@pytest.mark.asyncio
async def test_sim_only_never_touches_real(tmp_path: Path):
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    registry = ContractRegistry.packaged()
    sim = _executor(config, FakeTransport(config, registry), tmp_path)
    real = _executor(config, FakeTransport(config, registry), tmp_path)
    arbiter = OT2ExecutionArbiter(real, sim, mode=RunMode.SIM_ONLY)
    result = await arbiter.run(_parsed(tmp_path), await _expectation(config, registry))
    assert result.ok
    assert len(result.sim_responses) == 2
    assert result.real_responses == []


@pytest.mark.asyncio
async def test_real_only_skips_sim(tmp_path: Path):
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    registry = ContractRegistry.packaged()
    real = _executor(config, FakeTransport(config, registry), tmp_path)
    arbiter = OT2ExecutionArbiter(real, None, mode=RunMode.REAL_ONLY)
    result = await arbiter.run(_parsed(tmp_path), await _expectation(config, registry))
    assert result.ok
    assert len(result.real_responses) == 2
    assert result.sim_responses == []


@pytest.mark.asyncio
async def test_preflight_failure_halts_before_side_effects(tmp_path: Path):
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    registry = ContractRegistry.packaged()
    real = _executor(config, FakeTransport(config, registry), tmp_path)
    arbiter = OT2ExecutionArbiter(real, None, mode=RunMode.REAL_ONLY)
    result = await arbiter.run(
        _parsed(tmp_path),
        PreflightExpectation(
            contract_id="wrong",
            config_id=config.config_id,
            calibration_id=config.calibration_id,
            robot_serial_number="SIM-OT2-001",
            pipette_models={"RIGHT": "p300_multi_v2.1"},
        ),
    )
    assert not result.ok
    assert result.halted_before_real
    assert "preflight" in (result.halt_reason or "")


@pytest.mark.asyncio
async def test_shadow_reports_divergence_when_real_differs(tmp_path: Path):
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    registry = ContractRegistry.packaged()
    sim = _executor(config, FakeTransport(config, registry), tmp_path)
    real = _executor(config, FakeTransport(config, registry), tmp_path)
    real._transport.tip_presence = "PRESENT"  # simulated hardware disagrees after home
    arbiter = OT2ExecutionArbiter(real, sim, mode=RunMode.SHADOW)
    result = await arbiter.run(_parsed(tmp_path), await _expectation(config, registry))
    assert result.ok
    assert len(result.sim_responses) == 2
    assert len(result.real_responses) == 2
    # The pre-seeded PRESENT tip diverges from sim's ABSENT state after Home;
    # after PickUpTip both report PRESENT, so exactly one alert is expected.
    assert len(result.divergence_alerts) == 1
    alert = result.divergence_alerts[0]
    assert alert.step_index == 0
    assert alert.field == "tip_presence"
    assert alert.predicted == "ABSENT"
    assert alert.actual == "PRESENT"


def _parsed(tmp_path: Path):
    from unitelabs.opentrons_ot2.bridge import parse_workflow

    workflow = {
        "workflow_name": "arbiter-test",
        "version": "1.0",
        "phases": [
            {
                "phase_name": "phase",
                "steps": [
                    {"step_id": "home", "action": "robot.home", "params": {}, "description": "home"},
                    {
                        "step_id": "pick",
                        "action": "robot.pick_up_tip",
                        "params": {"pipette": "right_pipette", "labware": "tips_300", "well": "A1"},
                        "description": "pick",
                    },
                ],
            }
        ],
    }
    return parse_workflow(workflow)
