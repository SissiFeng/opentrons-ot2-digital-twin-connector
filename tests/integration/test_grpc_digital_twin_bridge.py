"""Real local gRPC coverage for the contract-derived OT-2 bridge transport."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitelabs.cdk import SiLAServerConfig
from unitelabs.opentrons_ot2 import OpentronsOt2Config, create_app
from unitelabs.opentrons_ot2.bridge import (
    OT2BridgeExecutor,
    OT2SiLATransport,
    OT2WorkflowAdapter,
    PreflightExpectation,
    WorkflowStep,
)
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig

from tests.digital_twin.helpers import config_mapping


def _step(step_id: str, action: str, params: dict[str, object]) -> WorkflowStep:
    return WorkflowStep(
        step_id=step_id,
        action=action,
        params=params,
        description="local gRPC integration",
        phase_name="integration",
    )


@pytest.mark.asyncio
@pytest.mark.simulator_only
async def test_bridge_preflight_and_atomic_transfer_over_real_grpc(tmp_path: Path) -> None:
    raw_config = config_mapping(tmp_path / "state.json")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(raw_config), encoding="utf-8")
    digital_twin_config = DigitalTwinConfig.from_mapping(raw_config)
    app_config = OpentronsOt2Config(
        use_simulator=True,
        digital_twin_config_path=str(config_path),
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    generator = create_app(app_config)
    connector = await generator.__anext__()
    await connector.start()
    address = connector.sila_server._address
    host, raw_port = address.rsplit(":", maxsplit=1)
    transport = await OT2SiLATransport.connect(
        host,
        int(raw_port),
        codec=connector.sila_server.protobuf,
        timeout_s=10,
    )
    try:
        adapter = OT2WorkflowAdapter.build(digital_twin_config)
        device_command = adapter.adapt(_step("identity", "robot.get_device_information", {}))
        assert device_command is not None
        device = await transport.execute(device_command)
        assert isinstance(device, dict)
        executor = OT2BridgeExecutor(transport, adapter, tmp_path / "audit.jsonl")
        report = await executor.preflight(
            PreflightExpectation(
                contract_id=adapter.registry.snapshot.contract_id,
                config_id=digital_twin_config.config_id,
                calibration_id=digital_twin_config.calibration_id,
                robot_serial_number=str(device["robot_serial_number"]),
                pipette_models={"RIGHT": "p300_multi_v2.1"},
            )
        )
        assert report.simulation is True

        await executor.execute_step(_step("home", "robot.home", {}))
        await executor.execute_step(
            _step(
                "reconcile",
                "robot.reconcile_tip",
                {"pipette": "right_pipette", "present": False},
            )
        )
        await executor.execute_step(
            _step(
                "pickup",
                "robot.pick_up_tip",
                {"pipette": "right_pipette", "labware": "tips_300", "well": "A1"},
            )
        )
        await executor.execute_step(
            _step(
                "aspirate",
                "robot.aspirate",
                {"pipette": "right_pipette", "volume_ul": 50.0},
            )
        )
        await executor.execute_step(
            _step(
                "dispense",
                "robot.dispense",
                {"pipette": "right_pipette", "volume_ul": 50.0},
            )
        )
        await executor.execute_step(_step("drop", "robot.drop_tip", {"pipette": "right_pipette"}))
        state = await executor.execute_step(_step("state", "robot.get_state", {}))
        assert isinstance(state, dict)
        mount = state["mounts"][0]
        assert mount["tip_presence"] == "ABSENT"
        assert mount["tip_evidence"] == "SOFTWARE_TRACKED"
        assert mount["liquid_volume"] == pytest.approx(0.0)
        assert len((tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    finally:
        await transport.close()
        await connector.stop()
        await generator.aclose()
