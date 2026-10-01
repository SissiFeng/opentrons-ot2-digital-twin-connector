import copy
import io
import json
import zipfile

import pytest

from ot2_bridge.flex import FlexAdapter, NativeFlexAdapter, make_plan, profile_template, validate_plan, validate_profile
from ot2_bridge.flex_bundle import bundle_bytes
from ot2_bridge.flex_runner import run_plan
from ot2_bridge.models import Evidence, Fact, Observation, Outcome, Status
from ot2_bridge.remote import RemoteSimulation, SimulationService
from ot2_bridge.wire import operation_from_dict


def qualified_profile():
    p = profile_template()
    p["real"].update(
        server_uuid="test-server",
        pipette_id="test-pipette",
        pipette_model="test-single",
        calibration_revision="synthetic-only",
        tiprack="test-rack",
        tip_length_mm=58.35,
        pickup={"x": 10, "y": 20, "z": 50},
        pickup_clearance={"x": 10, "y": 20, "z": 150},
        drop={"x": 200, "y": 200, "z": 50},
        drop_clearance={"x": 200, "y": 200, "z": 150},
        park={"x": 100, "y": 100, "z": 150},
    )
    return p


class TestTransport:
    __test__ = False

    def __init__(self):
        self.present = False
        self.calls = []
        self.fail = None
        self.hardware = False

    async def snapshot(self):
        return {
            "server_uuid": "test-server",
            "is_simulating": not self.hardware,
            "pipette": {"id": "test-pipette", "model": "test-single", "channels": 1},
            "machine_error": False,
            "estop": "DISENGAGED",
            "tip_present": self.present,
            "position": {"x": 100, "y": 100, "z": 140},
        }

    async def command(self, feature, name, **params):
        self.calls.append((feature, name, params))
        if name == self.fail:
            raise TimeoutError("uncertain command")
        if name == "PickUpTip":
            self.present = True
        if name == "DropTip":
            self.present = False


class SimAdapter:
    def __init__(self):
        self.calls = []
        self.failure = None

    def prepare(self, op):
        async def execute():
            self.calls.append(op.action)
            status = Status.FAILED if op.action == self.failure else Status.SUCCEEDED
            obs = Observation(
                "synthetic-matterix", {"pipette.tip_attached": Fact(op.action == "pick_up_tip", Evidence.SIMULATED)}
            )
            return Outcome(status, obs)

        return execute


def test_unqualified_profile_is_simulation_only():
    p = profile_template()
    assert validate_profile(p) == p
    with pytest.raises(ValueError):
        validate_profile(p, hardware=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mount", "RIGHT"),
        ("workflow", "mix_four_vials"),
        ("matterix_revision", "main"),
        ("device_id", ""),
        ("schema", "anything"),
    ],
)
def test_unsupported_profile_rejected(field, value):
    p = qualified_profile()
    p[field] = value
    with pytest.raises(ValueError):
        validate_profile(p)


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", True),
        ("host", "robot.local"),
        ("speed_mm_s", float("nan")),
        ("tip_length_mm", 0),
        ("pickup", {"x": True, "y": 0, "z": 1}),
    ],
)
def test_invalid_physical_parameters_rejected(field, value):
    p = qualified_profile()
    p["real"][field] = value
    with pytest.raises(ValueError):
        validate_profile(p, hardware=True)


def test_plan_pins_exact_profile_order_and_parameters():
    original = make_plan(profile_template(), "test-run")
    for change in ("profile", "order", "extra", "identity"):
        plan = copy.deepcopy(original)
        if change == "profile":
            plan["profile"]["device_id"] = "other"
        if change == "order":
            plan["operations"].reverse()
        if change == "extra":
            plan["operations"][0]["parameters"]["x"] = 1
        if change == "identity":
            plan["operations"][0]["operation_id"] = "new"
        with pytest.raises(ValueError):
            validate_plan(plan)


def test_bundle_contains_relocatable_source_and_single_run_scripts():
    plan = make_plan(profile_template(), "test-run")
    with zipfile.ZipFile(io.BytesIO(bundle_bytes(plan))) as z:
        assert json.loads(z.read("plan.json")) == plan
        assert b"--serve" not in z.read("simulate.sh")
        assert b"--serve" in z.read("serve-sim.sh")
        assert "bridge-src/ot2_bridge/flex_matterix.py" in z.namelist()
        assert "bridge-src/ot2_bridge/web/index.html" in z.namelist()
        assert "bridge-src/ot2_bridge/patches/flex-pipette-mount.patch" in z.namelist()
        assert all("__pycache__" not in name for name in z.namelist())


def test_native_mapping_preserves_all_actions_once():
    names = [
        "WaitCfg",
        "MoveToJointConfigCfg",
        "MoveToJointConfigCfg",
        "SetFLEXTipAttachedCfg",
        "MoveToJointConfigCfg",
        "MoveToJointConfigCfg",
        "MoveToJointConfigCfg",
        "SetFLEXTipAttachedCfg",
        "WaitCfg",
        "MoveToJointConfigCfg",
        "MoveToJointConfigCfg",
    ]
    actions = [type(name, (), {})() for name in names]
    for i, attached in ((3, True), (7, False)):
        actions[i].attached = attached
        actions[i].tip_child_id = "Tips__A1"
        actions[i].tip_asset_name = "flex_tips"
        actions[i].pipette = "left"
    adapter = NativeFlexAdapter(profile_template(), actions, None)
    assert [step for seq in adapter.chunks.values() for step in seq] == actions
    with pytest.raises(ValueError):
        NativeFlexAdapter(profile_template(), actions[:-1], None)


@pytest.mark.asyncio
async def test_full_real_adapter_maps_verified_tip_cycle():
    profile = qualified_profile()
    transport = TestTransport()
    adapter = FlexAdapter(profile, transport, hardware=False)
    for item in make_plan(profile, "test")["operations"]:
        outcome = await adapter.prepare(operation_from_dict(item))()
        assert outcome.status is Status.SUCCEEDED
        assert outcome.observation.facts["pipette.tip_attached"].evidence is Evidence.SIMULATED
    calls = transport.calls
    assert [name for _, name, _ in calls].count("PickUpTip") == 1
    assert [name for _, name, _ in calls].count("DropTip") == 1
    assert calls[0][2]["X"] == 100  # Vertical lift before XY traverse.
    assert calls[0][2]["Z"] == 150


@pytest.mark.asyncio
async def test_uncertain_physical_command_is_never_retried():
    profile = qualified_profile()
    transport = TestTransport()
    transport.fail = "PickUpTip"
    adapter = FlexAdapter(profile, transport, hardware=False)
    result = await run_plan(make_plan(profile, "test"), mode="real-only", real=adapter)
    assert result["status"] == "held"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["real"]["outcome"]["status"] == "unknown"
    assert [name for _, name, _ in transport.calls].count("PickUpTip") == 1


@pytest.mark.asyncio
async def test_hardware_cannot_silently_use_connector_simulator():
    transport = TestTransport()
    adapter = FlexAdapter(qualified_profile(), transport, hardware=True)
    with pytest.raises(ValueError, match="mode"):
        await adapter.snapshot()
    assert not transport.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "sim-only"])
async def test_runner_pairs_through_real_remote_protocol(mode):
    profile = qualified_profile()
    plan = make_plan(profile, "test")
    sim = SimAdapter()
    initial = Observation("synthetic-matterix", {"pipette.tip_attached": Fact(False, Evidence.SIMULATED)})
    service = SimulationService(sim, binding=plan["binding"], initial_observation=initial)
    server = await service.listen(0)
    try:
        remote = RemoteSimulation(server.sockets[0].getsockname()[1])
        adapter = FlexAdapter(profile, TestTransport(), hardware=False) if mode == "shadow" else None
        result = await run_plan(plan, mode=mode, real=adapter, simulation=remote)
        assert result["status"] == "completed", result
        assert sim.calls == ["pick_up_tip", "drop_tip", "park"]
        if mode == "shadow":
            assert all(s["comparisons"][0]["status"] == "match" for s in result["steps"])
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stop_request_prevents_first_dispatch():
    t = TestTransport()
    result = await run_plan(
        make_plan(qualified_profile(), "test"),
        mode="real-only",
        real=FlexAdapter(qualified_profile(), t, hardware=False),
        stop_requested=lambda: True,
    )
    assert result["status"] == "held"
    assert not t.calls


@pytest.mark.asyncio
async def test_stop_during_staging_discards_without_dispatch():
    state = {"stop": False, "discarded": False}
    sim = SimAdapter()

    class Staged:
        def prepare(self, op):
            return sim.prepare(op)

        async def discard(self):
            state["discarded"] = True

    class Remote:
        last_observation = Observation("sim", {"pipette.tip_attached": Fact(False, Evidence.SIMULATED)})

        async def connect(self, binding):
            return {}

        async def stage(self, operation):
            state["stop"] = True
            return Staged()

    transport = TestTransport()
    result = await run_plan(
        make_plan(qualified_profile(), "stop-test"),
        mode="shadow",
        real=FlexAdapter(qualified_profile(), transport, hardware=False),
        simulation=Remote(),
        stop_requested=lambda: state["stop"],
    )
    assert result["status"] == "held"
    assert state["discarded"]
    assert not transport.calls
    assert not sim.calls


@pytest.mark.asyncio
async def test_cancellation_retains_partial_pair_evidence():
    import asyncio

    sim_started, real_finished = asyncio.Event(), asyncio.Event()

    class SlowSim:
        last_observation = Observation("sim", {"pipette.tip_attached": Fact(False, Evidence.SIMULATED)})

        async def connect(self, binding):
            return {}

        async def stage(self, op):
            return self

        def prepare(self, op):
            async def execute():
                sim_started.set()
                await asyncio.Event().wait()

            return execute

    def emit(event):
        if event["type"] == "backend-finished" and event["side"] == "real":
            real_finished.set()

    task = asyncio.create_task(
        run_plan(
            make_plan(qualified_profile(), "cancel-test"),
            mode="shadow",
            real=FlexAdapter(qualified_profile(), TestTransport(), hardware=False),
            simulation=SlowSim(),
            emit=emit,
        )
    )
    await sim_started.wait()
    await real_finished.wait()
    task.cancel()
    result = await task
    assert result["status"] == "held"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["real"]["outcome"]["status"] == "succeeded"
    assert result["steps"][0]["sim"]["outcome"]["status"] == "unknown"


def test_local_run_session_excludes_overlap_and_replay(tmp_path, monkeypatch):
    from ot2_bridge.flex_session import device_session

    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path))
    profile = qualified_profile()
    with device_session(profile, run_id="once"):
        with pytest.raises(RuntimeError, match="Another local"):
            with device_session(profile, run_id="second"):
                pass
    with pytest.raises(RuntimeError, match="already attempted"):
        with device_session(profile, run_id="once"):
            pass
    with device_session(profile, run_id="new-reviewed-run"):
        pass


@pytest.mark.asyncio
async def test_home_holds_local_lock_during_state_check(monkeypatch):
    from contextlib import contextmanager
    from ot2_bridge import flex_cli

    held = {"value": False}

    @contextmanager
    def guard(profile):
        held["value"] = True
        try:
            yield
        finally:
            held["value"] = False

    class Transport(TestTransport):
        async def describe(self):
            assert held["value"]
            return await self.snapshot()

        async def command(self, feature, name, **params):
            assert held["value"]
            return await super().command(feature, name, **params)

        async def close(self):
            pass

    transport = Transport()

    async def connected(profile, **kwargs):
        return transport

    monkeypatch.setattr(flex_cli, "connected", connected)
    monkeypatch.setattr(flex_cli, "device_session", guard)
    await flex_cli.control(qualified_profile(), "home", hardware=False)
    assert not held["value"]
    assert transport.calls[0][1] == "Home"
