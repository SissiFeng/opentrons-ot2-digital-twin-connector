from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.bridge import OT2WorkflowAdapter, parse_workflow
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig
from unitelabs.opentrons_ot2.matterix import (
    AspirateCfg,
    DispenseCfg,
    DropTipCfg,
    HomeCfg,
    MatterixAssetConfigurationError,
    MoveToWellCfg,
    OT2MatterixAssetConfig,
    OT2MatterixPlanner,
    PickUpTipCfg,
    ReconcileTipCfg,
    install_action_sequence,
)
from unitelabs.opentrons_ot2.matterix.preflight import build_checks, main

from tests.digital_twin.helpers import config_mapping
from tests.matterix.helpers import (
    verified_alignment_mapping,
    verified_frame_alignment_mapping,
    verified_tip_binding_mapping,
)


class FakeFactory:
    def build_ot2_action_cfg(self, action):
        return {"type": type(action).__name__, "action": action}


class FakeStateMachine:
    def __init__(self) -> None:
        self.configs = None

    def set_action_sequence(self, configs) -> None:
        self.configs = configs


def _plan(tmp_path: Path):
    connector = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    workflow = parse_workflow(Path(__file__).parents[2] / "examples" / "workflows" / "ot2_tip_transfer.json")
    adapter = OT2WorkflowAdapter.build(connector)
    return connector, OT2MatterixPlanner(adapter).plan(workflow)


def test_planner_uses_bridge_semantics_and_preserves_request_hashes(tmp_path: Path) -> None:
    connector, plan = _plan(tmp_path)
    assert plan.config_id == connector.config_id
    assert len(plan.steps) == 8
    assert [type(action) for action in plan.actions] == [
        HomeCfg,
        ReconcileTipCfg,
        PickUpTipCfg,
        MoveToWellCfg,
        AspirateCfg,
        MoveToWellCfg,
        DispenseCfg,
        DropTipCfg,
    ]
    assert len({step.request_hash for step in plan.steps}) == 8
    assert all(len(step.request_hash) == 64 for step in plan.steps)


def test_state_machine_boundary_installs_complete_sequence(tmp_path: Path) -> None:
    _connector, plan = _plan(tmp_path)
    state_machine = FakeStateMachine()
    runtime = install_action_sequence(state_machine, plan.actions, FakeFactory())
    assert state_machine.configs == list(runtime)
    assert [item["type"] for item in runtime] == [type(action).__name__ for action in plan.actions]


def test_asset_identity_and_hash_are_fail_closed(tmp_path: Path) -> None:
    connector, plan = _plan(tmp_path)
    asset_path = tmp_path / "ot2.usd"
    asset_path.write_bytes(b"usd-test-asset")
    mapping = {
        "schema_version": "3.0",
        "task_id": "Matterix-OT2-LiquidHandler-v1",
        "robot_asset_name": "ot2",
        "asset_path": str(asset_path),
        "asset_sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
        "action_factory_module": "matterix_ot2_actions",
        "deck_frame": "ot2_deck",
        "joint_alignment": verified_alignment_mapping(),
        "reference_frame_alignment": verified_frame_alignment_mapping(),
        "tip_rack_bindings": [verified_tip_binding_mapping()],
        "contract_id": plan.contract_id,
        "config_id": connector.config_id,
        "calibration_id": connector.calibration_id,
    }
    config = OT2MatterixAssetConfig.from_mapping(mapping)
    config.validate_connector(connector, OT2WorkflowAdapter.build(connector).registry)
    assert config.validate_asset_file() == asset_path
    mapping["asset_sha256"] = "0" * 64
    with pytest.raises(MatterixAssetConfigurationError, match="does not match pinned"):
        OT2MatterixAssetConfig.from_mapping(mapping).validate_asset_file()


def test_checked_example_reports_real_runtime_not_ready() -> None:
    checks = build_checks("config/ot2_dt_config.json", "config/matterix_ot2.json")
    by_name = {check.name: check for check in checks}
    assert by_name["OT-2 connector configuration"].ok is True
    assert by_name["Confirmed physical calibration"].ok is False
    assert by_name["Verified OT-2 joint alignment"].ok is False
    assert by_name["Verified world/base/deck alignment"].ok is False
    assert by_name["Verified individual tip addressing"].ok is False
    assert by_name["Matterix OT-2 USD asset"].ok is False
    assert by_name["matterix_ot2_actions"].ok is False
    assert main(["--strict", "--json"]) == 1


def test_matterix_config_pins_current_example_contract() -> None:
    config = OT2MatterixAssetConfig.from_file("config/matterix_ot2.json")
    connector = DigitalTwinConfig.from_file("config/ot2_dt_config.json")
    adapter = OT2WorkflowAdapter.build(connector)
    config.validate_connector(connector, adapter.registry)
    raw = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    assert raw["contract_id"] == adapter.registry.snapshot.contract_id
