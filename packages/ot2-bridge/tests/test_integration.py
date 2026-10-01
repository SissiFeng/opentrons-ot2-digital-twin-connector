"""Behavioral tests for real client mapping, remote failure boundaries and native lifecycle."""

import asyncio
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ot2_bridge import Evidence, Fact, Observation, Outcome, Status
from ot2_bridge.cli import check_initial, demo_plan, execute_plan, run_demo
from ot2_bridge.client_adapter import OT2ClientAdapter, position_observation, validate_client_operation
from ot2_bridge.demo import DemoBackend, operations
from ot2_bridge.matterix_runtime import MatterixRuntime, NativeOT2Adapter
from ot2_bridge.remote import RemoteSimulation, SimulationService
from ot2_bridge.wire import dumps, operation_from_dict, outcome_from_dict, plain


@pytest.mark.parametrize("op", operations())
async def test_upstream_call_mapping_preserves_complete_parameters(op):
    method = AsyncMock(return_value={"X": 999})
    client = SimpleNamespace(**{op.action: method})
    observation = position_observation({"X": 1.5}, source="firmware")
    observe = AsyncMock(return_value=observation)
    adapter = OT2ClientAdapter("ot2", client, observe, mounts={"LEFT": 1})
    result = await adapter.prepare(op)()
    expected = dict(op.parameters)
    if "mount" in expected:
        expected["mount"] = "left"
        del expected["channels"]
    method.assert_awaited_once_with(**expected)
    assert result.observation.facts["axis.X"].value == 1.5  # Never the commanded target.
    assert "pipette.volume" not in result.observation.facts
    assert result.status is Status.SUCCEEDED


async def test_readback_failure_preserves_success_without_invented_state():
    adapter = OT2ClientAdapter(
        "ot2", SimpleNamespace(home=AsyncMock()), AsyncMock(side_effect=OSError("readback lost")), mounts={"LEFT": 1}
    )
    result = await adapter.prepare(operations()[0])()
    assert result.status is Status.SUCCEEDED and result.observation is None
    assert "readback lost" in result.error


@pytest.mark.parametrize(
    "changes",
    [
        {"channels": 8},
        {"mount": "RIGHT"},
        {"volume_ul": True},
        {"volume_ul": -2},
        {"flow_rate_ul_s": 0},
        {"unexpected": 1},
    ],
)
def test_semantic_preflight_rejects_incompatible_requests(changes):
    op = operations()[3]
    with pytest.raises(ValueError):
        validate_client_operation(replace(op, parameters=dict(op.parameters) | changes), "ot2", {"LEFT": 1})


async def test_service_reservation_replay_and_failed_session():
    plan = demo_plan()
    sim = DemoBackend(source="sim", fail_action="home")
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.observe())
    hello = await service.dispatch({"method": "hello", "binding": plan["binding"]})
    session = hello["session"]
    payload = {"method": "prepare", "operation": plain(operations()[0]), "session": session}
    token = (await service.dispatch(payload))["token"]
    with pytest.raises(RuntimeError, match="busy"):
        await service.dispatch(payload)
    with pytest.raises(ValueError, match="reservation"):
        await service.dispatch({"method": "execute", "token": "bad", "session": session})
    result = await service.dispatch({"method": "execute", "token": token, "session": session})
    assert result.status is Status.FAILED
    with pytest.raises(RuntimeError, match="recovery"):
        await service.dispatch(payload)


async def test_remote_binding_and_preflight_errors_do_not_execute():
    plan = demo_plan()
    sim = DemoBackend(source="sim")
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.observe())
    server = await service.listen(0)
    async with server:
        remote = RemoteSimulation(server.sockets[0].getsockname()[1])
        with pytest.raises(RuntimeError, match="mismatch"):
            await remote.connect({"wrong": "binding"})
        initial = await remote.connect(plan["binding"])
        check_initial(initial, plan["initial_sim"])
        with pytest.raises(RuntimeError, match="expected fields"):
            await remote.stage(replace(operations()[0], parameters={"axes": "XYZ", "extra": 1}))
        assert not service.seen and sim.volume == 0
        staged = await remote.stage(operations()[0])
        with pytest.raises(ValueError, match="mismatch"):
            staged.prepare(operations()[1])
        result = await staged.prepare(operations()[0])()
        assert result.status is Status.SUCCEEDED
        with pytest.raises(ValueError, match="consumed"):
            staged.prepare(operations()[0])
        with pytest.raises(RuntimeError, match="already dispatched"):
            await remote.stage(operations()[0])


@pytest.mark.parametrize("drift,fail,count,exit_code", [(0, "", 7, 0), (2, "", 4, 3), (0, "aspirate", 4, 2)])
async def test_demo_full_network_run_and_negative_controls(tmp_path, drift, fail, count, exit_code):
    args = SimpleNamespace(out=str(tmp_path), drift=drift, fail_action=fail)
    assert await run_demo(args) == exit_code
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert len(records) == count
    assert (tmp_path / "report.html").exists()
    for record in records:
        assert record["real"]["started"] < record["sim"]["finished"]
        assert record["sim"]["started"] < record["real"]["finished"]
    if drift:
        assert records[-1]["comparisons"][0]["status"] == "different"
    if fail:
        assert records[-1]["real"]["outcome"]["status"] == "succeeded"
        assert records[-1]["comparisons"][0]["status"] == "unavailable"


async def test_existing_trace_is_not_overwritten(tmp_path):
    (tmp_path / "trace.jsonl").write_text("existing evidence")
    with pytest.raises(FileExistsError):
        await execute_plan(demo_plan(), None, None, tmp_path)
    assert (tmp_path / "trace.jsonl").read_text() == "existing evidence"


def test_wire_round_trip_and_unknown_evidence():
    observation = Observation("source", {"x": Fact(None, Evidence.UNKNOWN, "mm", "machine")})
    outcome = Outcome(Status.UNKNOWN, observation, "disconnected")
    assert outcome_from_dict(json.loads(dumps(outcome))) == outcome
    op = operations()[2]
    assert operation_from_dict(json.loads(dumps(op))) == op
    with pytest.raises(ValueError, match="Initial"):
        check_initial(plain(observation), {"x": 0})


class Flag:
    def __init__(self, value):
        self.value = value

    def all(self):
        return self.value

    def any(self):
        return self.value


class Action:
    def to(self, device):
        return self


class FakeSM:
    def __init__(self):
        self.installed = []
        self.steps = 0

    def set_action_sequence(self, configs):
        self.installed.append(configs)

    def reset(self):
        self.steps = 0
        self.action_sequence_success = Flag(False)
        self.action_sequence_failure = Flag(False)

    def step(self, obs):
        self.steps += 1
        self.action_sequence_success = Flag(self.steps == 3)
        return Action(), ["semantic-output"]


class FakeEnv:
    num_envs, device = 1, "cpu"

    def __init__(self, terminated=False):
        self.calls = []
        self.terminated = terminated

    def step(self, action, *, semantic_actions):
        self.calls.append(semantic_actions)
        return {}, None, Flag(self.terminated), Flag(False), {}


async def test_native_runtime_steps_semantics_and_preserves_environment():
    env, sm = FakeEnv(), FakeSM()
    runtime = MatterixRuntime(env, sm, {}, {"asset_name": "ot2"})
    for op in operations()[:2]:
        result = await runtime(op, ("cfg",))
        assert result.status is Status.SUCCEEDED
    assert env.calls == [["semantic-output"]] * 6
    assert len(sm.installed) == 2


async def test_native_auto_reset_and_cancellation_require_recovery():
    runtime = MatterixRuntime(FakeEnv(True), FakeSM(), {}, {"asset_name": "ot2"})
    with pytest.raises(RuntimeError, match="auto-reset"):
        await runtime(operations()[0], ("cfg",))
    with pytest.raises(RuntimeError, match="recovery"):
        await runtime(operations()[0], ("cfg",))
    runtime = MatterixRuntime(FakeEnv(), FakeSM(), {}, {"asset_name": "ot2"})
    task = asyncio.create_task(runtime(operations()[0], ("cfg",)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "CancelledError" in runtime.fault and not runtime.busy


async def test_native_adapter_exact_recipe_and_current_pose_mapping(monkeypatch):
    def cfg(**kwargs):
        return kwargs

    modules = {
        "compositional_actions.ot2_liquid_handling": SimpleNamespace(AspirateOT2Cfg=cfg, DispenseOT2Cfg=cfg),
        "primitive_actions.move_to_joint_config": SimpleNamespace(MoveToJointConfigCfg=cfg),
        "semantic_actions.ot2_action": SimpleNamespace(SetOT2TipAttachedCfg=cfg),
        "robot_action_spaces": SimpleNamespace(OT2_JOINT_ACTION_SPACE="space"),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, "matterix_sm." + name, module)
    op = operations()[2]
    profile = {
        "device_id": "ot2",
        "asset_name": "robot",
        "recipes": [
            {
                "action": op.action,
                "parameters": plain(op.parameters),
                "steps": [{"type": "joints", "positions_m": [0.1, 0.2, 0, 0], "timeout_s": 10, "threshold_m": 0.001}],
            }
        ],
    }
    submit = AsyncMock(return_value=Outcome(Status.SUCCEEDED))
    adapter = NativeOT2Adapter(profile, submit)
    await adapter.prepare(op)()
    assert submit.call_args.args[1][0]["target_joint_positions"] == (0.1, 0.2, 0, 0)
    with pytest.raises(ValueError, match="reviewed"):
        adapter.prepare(replace(op, parameters=dict(op.parameters) | {"well": "H12"}))
    liquid = operations()[3]
    await adapter.prepare(liquid)()
    assert submit.call_args.args[1] == ({"asset_name": "robot", "volume": 20, "flow_rate": 10},)


async def test_session_token_required_and_wrong_run_rejected():
    plan = demo_plan()
    sim = DemoBackend(source="sim")
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.observe())
    with pytest.raises(ValueError, match="handshake"):
        await service.dispatch({"method": "prepare", "operation": plain(operations()[0])})
    hello = await service.dispatch({"method": "hello", "binding": plan["binding"]})
    assert hello["ready"]
    assert not (await service.dispatch({"method": "hello", "binding": plan["binding"]}))["ready"]
    wrong_run = replace(operations()[0], run_id="different-run")
    with pytest.raises(ValueError, match="run/device"):
        await service.dispatch({"method": "prepare", "operation": plain(wrong_run), "session": hello["session"]})
    assert service.pending is None and not service.seen


async def test_runtime_timeout_is_unknown_and_never_retried():
    sim = DemoBackend(source="sim")
    plan = demo_plan()
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.observe(), timeout=0.001)
    session = (await service.dispatch({"method": "hello", "binding": plan["binding"]}))["session"]
    token = (await service.dispatch({"method": "prepare", "operation": plain(operations()[0]), "session": session}))[
        "token"
    ]
    result = await service.dispatch({"method": "execute", "token": token, "session": session})
    assert result.status is Status.UNKNOWN and service.fault
    assert len(service.seen) == 1


def test_entire_plan_preflight_rejects_late_duplicate_before_dispatch():
    from ot2_bridge.cli import validate_plan

    plan = demo_plan()
    plan["operations"][-1]["operation_id"] = plan["operations"][0]["operation_id"]
    with pytest.raises(ValueError, match="duplicate"):
        validate_plan(plan)


async def test_disconnect_does_not_replay_or_clear_running_reservation():
    plan = demo_plan()
    sim = DemoBackend(source="sim")
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.observe())
    server = await service.listen(0)
    async with server:
        remote = RemoteSimulation(server.sockets[0].getsockname()[1])
        await remote.connect(plan["binding"])
        staged = await remote.stage(operations()[0])
        await staged.discard()
        assert service.pending is None and not service.seen
        staged = await remote.stage(operations()[0])
        remote.timeout = 0.005
        with pytest.raises(asyncio.TimeoutError):
            await staged.prepare(operations()[0])()
        assert service.running
        await asyncio.sleep(0.04)
        assert not service.running and len(service.seen) == 1
        remote.timeout = 1
        with pytest.raises(RuntimeError, match="already dispatched"):
            await remote.stage(operations()[0])
