import copy
import io
import json
import zipfile

import pytest

from ot2_bridge import ot2_console as ot2
from ot2_bridge.flex_bundle import bundle_bytes
from ot2_bridge.flex_runner import run_plan
from ot2_bridge.instruments import profile_template, validate_plan
from ot2_bridge.models import Evidence, Fact, Observation, Outcome, Status
from ot2_bridge.remote import RemoteSimulation, SimulationService
from ot2_bridge.wire import operation_from_dict


def test_instrument_contracts_are_separate():
    profile = profile_template()
    assert profile["schema"] == "ot2.profile/1"
    plan = ot2.make_plan(profile, "test-run")
    assert [op["action"] for op in plan["operations"]] == ["home", "read_position"]
    validate_plan(plan)
    with pytest.raises(ValueError):
        ot2.validate_profile(profile_template("flex"))
    with pytest.raises(ValueError):
        ot2.validate_profile(profile, hardware=True)
    with zipfile.ZipFile(io.BytesIO(bundle_bytes(plan))) as archive:
        assert json.loads(archive.read("plan.json")) == plan
        assert b"OT-2 HOME" in archive.read("README.txt")
        assert "bridge-src/ot2_bridge/ot2_console.py" in archive.namelist()
        assert b'MATTERIX_PYTHON' in archive.read("serve-sim.sh")
        assert b'./matterix.sh' not in archive.read("serve-sim.sh")


def test_ot2_profile_binds_both_physical_models_and_its_own_assets():
    from ot2_bridge.flex import ASSETS_REVISION as FLEX_ASSETS

    profile = profile_template()
    assert profile["assets_revision"] != FLEX_ASSETS
    assert profile["task"] == "Matterix-OT2-DualMulti-Home-v1"
    assert profile["pipettes"]["identity_source"] == "operator_reported"
    assert profile["pipettes"]["left"] == {"model": "p10_multi_v1.6", "channels": 8}
    assert profile["pipettes"]["right"] == {"model": "p300_multi_v2.0", "channels": 8}
    profile["pipettes"]["left"], profile["pipettes"]["right"] = profile["pipettes"]["right"], profile["pipettes"]["left"]
    with pytest.raises(ValueError, match="pipettes"):
        ot2.validate_profile(profile)


@pytest.mark.parametrize("change", ["order", "profile", "parameters", "schema"])
def test_mutated_ot2_plan_rejected(change):
    plan = ot2.make_plan(profile_template(), "test-run")
    if change == "order":
        plan["operations"].reverse()
    elif change == "profile":
        plan["profile"]["real"]["server_uuid"] = "other"
    elif change == "parameters":
        plan["operations"][0]["parameters"]["axes"] = "X"
    else:
        plan["binding"]["schema"] = "flex.tip-cycle/1"
    with pytest.raises(ValueError):
        validate_plan(plan)


class TestTransport:
    __test__ = False

    def __init__(self, simulated=False):
        self.info = {
            "server_uuid": "bound-ot2",
            "is_simulating": simulated,
            "position": dict.fromkeys("xyzabc", 10.0),
            "homed": dict.fromkeys("xyzabc", False),
        }
        self.calls = []
        self.fail = False

    async def describe(self):
        return copy.deepcopy(self.info)

    async def command(self, feature, name, **parameters):
        self.calls.append((feature, name, parameters))
        if self.fail:
            raise TimeoutError("uncertain hardware result")
        self.info["homed"] = dict.fromkeys("xyzabc", True)


def qualified():
    profile = profile_template()
    profile["real"]["server_uuid"] = "bound-ot2"
    return profile


@pytest.mark.asyncio
async def test_real_home_and_readback_uses_only_ot2_motion_and_never_invents_tip():
    profile = qualified()
    transport = TestTransport()
    adapter = ot2.OT2HomeAdapter(profile, transport)
    plan = ot2.make_plan(profile, "physical-fixture")
    result = await run_plan(plan, mode="real-only", real=adapter)
    assert result["status"] == "completed"
    assert transport.calls == [("MotionControlFeature", "Home", {"Axes": "XYZABC"})]
    facts = result["steps"][1]["real"]["outcome"]["observation"]["facts"]
    assert facts["axis.X"]["unit"] == "mm"
    assert facts["axis.X"]["frame"] == "ot2.machine"
    assert facts["homed.X"]["value"] is True
    assert facts["homed.X"]["evidence"] == "software_tracked"
    assert "pipette.tip_attached" not in facts


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["mode", "identity", "stop", "invalid-observation"])
async def test_ot2_refuses_dispatch_on_bad_binding_or_stop(mismatch):
    profile = qualified()
    transport = TestTransport()
    if mismatch == "mode":
        transport.info["is_simulating"] = True
    if mismatch == "identity":
        transport.info["server_uuid"] = "flex-or-other-ot2"
    if mismatch == "invalid-observation":
        transport.info["position"]["x"] = float("nan")
    adapter = ot2.OT2HomeAdapter(profile, transport, stop_requested=lambda: mismatch == "stop")
    result = await run_plan(ot2.make_plan(profile, "bad-binding"), mode="real-only", real=adapter)
    assert result["status"] == "held"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_ot2_unknown_command_does_not_read_next_step_or_retry():
    transport = TestTransport()
    transport.fail = True
    result = await run_plan(
        ot2.make_plan(qualified(), "timeout"), mode="real-only", real=ot2.OT2HomeAdapter(qualified(), transport)
    )
    assert result["status"] == "held"
    assert len(result["steps"]) == len(transport.calls) == 1
    assert result["steps"][0]["real"]["outcome"]["status"] == "unknown"


class SyntheticNative:
    def observe(self, revision="initial"):
        return Observation(
            "synthetic-matterix",
            {
                f"joint.{i}": Fact(v, Evidence.SIMULATED, "m", "matterix.joint")
                for i, v in enumerate((0.2, 0.18, 0.0, 0.0))
            },
            revision,
        )

    async def __call__(self, operation, actions):
        self.actions = actions
        return Outcome(Status.SUCCEEDED, self.observe(operation.operation_id))


def native_adapter(profile):
    action = type(
        "MoveToJointConfigCfg", (), {"agent_assets": "ot2", "target_joint_positions": (0.2, 0.18, 0.0, 0.0)}
    )()
    return ot2.NativeOT2HomeAdapter(profile, [action], SyntheticNative())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sim-only", "shadow"])
async def test_ot2_paired_completion_keeps_native_frames_separate(mode):
    profile = qualified()
    plan = ot2.make_plan(profile, "ot2-" + mode)
    sim = native_adapter(profile)
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.runtime.observe())
    server = await service.listen(0)
    try:
        result = await run_plan(
            plan,
            mode=mode,
            real=ot2.OT2HomeAdapter(profile, TestTransport()) if mode == "shadow" else None,
            simulation=RemoteSimulation(server.sockets[0].getsockname()[1]),
        )
        assert result["status"] == "completed", result
        assert len(result["steps"]) == 2
        assert result["comparison_scope"].startswith("completion-only")
        if mode == "shadow":
            assert result["steps"][0]["comparisons"] == []
            assert result["steps"][0]["real"]["outcome"]["observation"]["facts"]["axis.X"]["frame"] == "ot2.machine"
            assert result["steps"][0]["sim"]["outcome"]["observation"]["facts"]["joint.0"]["frame"] == "matterix.joint"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_native_readback_must_confirm_home_pose():
    profile = profile_template()
    adapter = native_adapter(profile)
    adapter.runtime.observe = lambda revision: Observation("synthetic-matterix", {})
    outcome = await adapter.prepare(operation_from_dict(ot2.make_plan(profile, "missing")["operations"][1]))()
    assert outcome.status is Status.FAILED


@pytest.mark.asyncio
async def test_ot2_standard_sila_wire(tmp_path, monkeypatch):
    pytest.importorskip("sila2")
    pytest.importorskip("unitelabs.cdk")
    from unitelabs.cdk import Connector, SiLAServerConfig
    from unitelabs.opentrons_ot2 import OpentronsOt2Config
    from unitelabs.opentrons_ot2.features.motion_control import MotionControlFeature
    from unitelabs.opentrons_ot2.io import OT2MotionController
    from ot2_bridge.ot2_sila import OT2SiLATransport
    from ot2_bridge.flex_cli import run_connected, control

    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "attempts"))
    controller = await OT2MotionController.build(simulate=True)
    connector = Connector(
        OpentronsOt2Config(
            use_simulator=True,
            sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
            cloud_server_endpoint=None,
            discovery=None,
        )
    )
    connector.register(MotionControlFeature(controller))
    await connector.start()
    transport = OT2SiLATransport(port=int(connector.sila_server._address.rsplit(":", 1)[1]))
    try:
        await transport.connect()
        info = await transport.describe()
        assert info["is_simulating"] is True
        assert set(info["position"]) == set("xyzabc")
        profile = profile_template()
        profile["real"].update(port=transport.port, server_uuid=info["server_uuid"])
        # Production mode must reject the explicitly simulated development service.
        rejected = await run_connected(ot2.make_plan(profile, "wire-physical-refused"), mode="real-only", hardware=True)
        assert rejected["status"] == "held"
        for mode in ("real-only", "shadow"):
            plan = ot2.make_plan(profile, "wire-" + mode)
            sim = native_adapter(profile)
            service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.runtime.observe())
            server = await service.listen(0)
            try:
                result = await run_connected(
                    plan, mode=mode, hardware=False, sim_port=server.sockets[0].getsockname()[1]
                )
                assert result["status"] == "completed", result
                assert len(result["steps"]) == 2
                assert result["steps"][1]["real"]["outcome"]["observation"]["facts"]["homed.X"]["value"] is True
            finally:
                server.close()
                await server.wait_closed()
        response = await control(profile, "stop")
        assert response["physical_stop_confirmed"] is False
    finally:
        await transport.close()
        await connector.stop()
        await controller.disconnect()
