"""CPU acceptance for application wiring. These fixtures never contact a real device or GPU."""

import asyncio
import copy
import http.client
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from ot2_bridge.flex_console import make_server
from ot2_bridge.gateway import GatewayBroker
from ot2_bridge.gateway_agent import DeviceExecutor, serve_gateway, hub_url
from ot2_bridge.instruments import make_plan, profile_template
from ot2_bridge.models import Evidence, Fact, Observation, Outcome, Status
from ot2_bridge.remote import SimulationService


def configuration(tmp_path, instrument="ot2"):
    if instrument == "flex":
        from test_flex import qualified_profile

        p = qualified_profile()
    else:
        p = profile_template("ot2")
    p["real"]["server_uuid"] = "cpu-test-device"
    profile_file, token_file = tmp_path / "device.json", tmp_path / "token"
    profile_file.write_text(json.dumps(p))
    token_file.write_text("test-gateway-credential-" + "x" * 32)
    token_file.chmod(0o600)
    return p, {"profile_file": str(profile_file), "token_file": str(token_file)}, token_file.read_text()


def observation(revision="initial", tip=False):
    return Observation(
        "cpu-fixture",
        {
            "axis.X": Fact(0, Evidence.SIMULATED, "mm", "ot2.machine"),
            "pipette.tip_attached": Fact(tip, Evidence.SIMULATED),
        },
        revision,
    )


class Backend:
    def __init__(self, profile):
        self.profile = profile
        self.operations = []
        self.closed = 0
        self.gate = None
        self.entered = asyncio.Event()
        self.stops = 0

    async def inspect(self):
        return {"server_uuid": self.profile["real"]["server_uuid"], "is_simulating": False, "fixture": True}

    async def begin(self, plan):
        self.plan = plan

    async def snapshot(self, revision):
        return observation(revision)

    async def execute(self, op):
        self.operations.append(op)
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return Outcome(Status.SUCCEEDED, observation(op.operation_id, op.action == "pick_up_tip"))

    async def stop(self):
        self.stops += 1
        if self.gate:
            self.gate.set()
        return {"physical_stop_confirmed": False, "fixture": True}

    async def close(self):
        self.closed += 1


def command(kind, payload=None, **kwargs):
    return {"id": str(uuid.uuid4()), "kind": kind, "payload": payload, "expires_at": time.time() + 120, **kwargs}


def test_executor_rejects_replay_foreign_profile_and_out_of_order(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "state"))
    p, _, _ = configuration(tmp_path)
    backend = Backend(p)
    executor = DeviceExecutor(p, backend, tmp_path / "ledger")
    plan = make_plan(p, str(uuid.uuid4()))

    async def run():
        wrong = copy.deepcopy(p)
        wrong["real"]["server_uuid"] = "different"
        with pytest.raises(ValueError, match="differs"):
            await executor.dispatch(command("begin", make_plan(wrong, str(uuid.uuid4()))))
        await executor.dispatch(command("begin", plan))
        with pytest.raises(ValueError, match="sequence"):
            await executor.dispatch(command("execute", plan["operations"][1]))
        first = command("execute", plan["operations"][0])
        await executor.dispatch(first)
        with pytest.raises(FileExistsError):
            await executor.dispatch(first)
        with pytest.raises(ValueError, match="duplicated"):
            await executor.dispatch(command("execute", plan["operations"][0]))
        await executor.dispatch(command("finish", plan["binding"]))
        with pytest.raises(RuntimeError, match="already attempted"):
            await executor.dispatch(command("begin", plan))

    asyncio.run(run())
    assert len(backend.operations) == 1


def test_gateway_stop_is_concurrent_and_blocks_later_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "state"))
    p, _, _ = configuration(tmp_path)
    backend = Backend(p)
    executor = DeviceExecutor(p, backend, tmp_path / "ledger")
    plan = make_plan(p, str(uuid.uuid4()))

    async def run():
        backend.gate = asyncio.Event()
        await executor.dispatch(command("begin", plan))
        pending = asyncio.create_task(executor.dispatch(command("execute", plan["operations"][0])))
        await backend.entered.wait()
        await executor.dispatch(command("stop"))
        await asyncio.wait_for(pending, 1)
        with pytest.raises(ValueError, match="held"):
            await executor.dispatch(command("execute", plan["operations"][1]))
        await executor.close()

    asyncio.run(run())
    assert backend.stops == 1 and len(backend.operations) == 1


def test_broker_lost_delivery_holds_and_never_requeues(tmp_path):
    p, entry, secret = configuration(tmp_path)
    broker = GatewayBroker([entry])
    key = p["device_id"]
    with pytest.raises(ValueError, match="credential"):
        broker.authorize(key, "Bearer wrong")
    broker.authorize(key, "Bearer " + secret)
    session = broker.register(key, p)["session"]

    async def run():
        pending = asyncio.create_task(broker.request(key, "begin", {}, timeout=0.1))
        await asyncio.sleep(0)
        delivered = broker.poll(key, session, wait=0)
        assert delivered["kind"] == "begin"
        assert broker.poll(key, session, wait=0) is None
        with pytest.raises(asyncio.TimeoutError):
            await pending
        with pytest.raises(RuntimeError, match="unknown"):
            await broker.request(key, "begin", {})
        with pytest.raises(ValueError, match="late"):
            broker.result(key, session, {"id": delivered["id"], "value": {}, "error": ""})

    asyncio.run(run())
    broker.close()


def request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    h = {"Content-Type": "application/json", "X-Bridge-Token": server.state.token}
    h.update(headers or {})
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    response = conn.getresponse()
    status, data = response.status, response.read()
    conn.close()
    return status, json.loads(data)


@pytest.mark.parametrize("instrument", ["ot2", "flex"])
@pytest.mark.parametrize("mode", ["real-only", "shadow", "sim-only"])
def test_browser_to_outbound_gateway_complete_run(tmp_path, monkeypatch, mode, instrument):
    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "locks"))
    p, entry, secret = configuration(tmp_path, instrument)
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"gateways": [entry], "matterix": {"test_only": True}}))
    server = make_server(0, tmp_path / "runs", site_config=site)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    backend = Backend(p)
    executor = DeviceExecutor(p, backend, tmp_path / "ledger")
    service = None

    class SimAdapter:
        def prepare(self, op):
            async def execute():
                return Outcome(Status.SUCCEEDED, observation(op.operation_id, op.action == "pick_up_tip"))

            return execute

    async def native_start(plan, directory, emit, stopped):
        nonlocal service
        service = await SimulationService(
            SimAdapter(), binding=plan["binding"], initial_observation=observation()
        ).listen(0)
        emit({"type": "simulation-ready", "fixture": True})
        return service.sockets[0].getsockname()[1]

    async def native_close():
        if service:
            service.close()
            await service.wait_closed()

    monkeypatch.setattr(server.state.native, "start", native_start)
    monkeypatch.setattr(server.state.native, "close", native_close)

    async def agent():
        await serve_gateway(server.url, p, secret, executor)

    gateway_task = asyncio.run_coroutine_threadsafe(agent(), server.state.runtime.loop)
    try:
        deadline = time.monotonic() + 3
        while not server.state.gateways.catalog()[0]["online"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.state.gateways.catalog()[0]["online"]
        assert request(server, "GET", "/api/site")[1]["managed"]
        assert request(server, "POST", "/api/inspect", {"profile": p})[1]["fixture"]
        # Browser credentials cannot impersonate a device gateway.
        assert request(server, "POST", "/gateway/register", {"profile": p})[0] == 400
        wrong = copy.deepcopy(p)
        wrong["real"]["port"] += 1
        assert request(server, "POST", "/api/review", {"profile": wrong})[0] == 400
        status, review = request(server, "POST", "/api/review", {"profile": p})
        assert status == 200
        assert (
            request(server, "POST", "/api/run", {"mode": mode, "hardware": True, "fingerprint": review["fingerprint"]})[
                0
            ]
            == 202
        )
        deadline = time.monotonic() + 8
        while server.state.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        job = request(server, "GET", "/api/job")[1]
        assert job["status"] == "completed", job
        count = 2 if instrument == "ot2" else 3
        assert len(job["report"]["steps"]) == count
        assert len(backend.operations) == (0 if mode == "sim-only" else count)
        allowed = ("home", "read_position") if instrument == "ot2" else ("pick_up_tip", "drop_tip", "park")
        assert all(op.action in allowed for op in backend.operations)
        # Refresh does not replay and a consumed review cannot be run twice.
        assert (
            request(server, "POST", "/api/run", {"mode": mode, "hardware": True, "fingerprint": review["fingerprint"]})[
                0
            ]
            == 400
        )
        assert (Path(job["output"]) / "report.json").is_file()
    finally:
        server.state.gateways.close()
        gateway_task.cancel()
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8088",
        "http://10.0.0.1:8088",
        "http://127.0.0.1:8/path",
        "http://user:pass@127.0.0.1:8",
        "https://127.0.0.1:8",
    ],
)
def test_gateway_url_restricts_credentials_and_plaintext_scope(url):
    with pytest.raises(ValueError):
        hub_url(url)


def test_cleanup_failure_preserves_step_evidence(tmp_path, monkeypatch):
    from ot2_bridge.flex_console import ConsoleState
    from ot2_bridge import flex_console

    p, entry, _ = configuration(tmp_path)
    state = ConsoleState(tmp_path / "runs", {"gateways": [entry]})
    plan = make_plan(p, "retain-evidence")
    evidence = {"status": "completed", "steps": [{"real": {"observation": "retained"}}], "events": ["retained"]}

    async def begin(self):
        pass

    async def finish(self):
        raise TimeoutError("response lost")

    async def execute(*args, **kwargs):
        return copy.deepcopy(evidence)

    monkeypatch.setattr(flex_console.GatewayAdapter, "begin", begin)
    monkeypatch.setattr(flex_console.GatewayAdapter, "finish", finish)
    monkeypatch.setattr(flex_console, "run_plan", execute)
    try:
        result = state.runtime.run(state.run_managed(plan, "real-only", tmp_path, lambda _: None))
        assert result["status"] == "held"
        assert result["steps"] == evidence["steps"] and result["events"] == evidence["events"]
        assert result["cleanup_errors"]
    finally:
        state.runtime.close()


def test_stop_during_inspect_does_not_use_previous_simulation_mode(tmp_path, monkeypatch):
    from ot2_bridge.flex_console import ConsoleState

    p, entry, _ = configuration(tmp_path)
    state = ConsoleState(tmp_path, {"gateways": [entry]})
    calls = []

    async def request(*args, **kwargs):
        calls.append(args)
        return {"stop_response": True}

    monkeypatch.setattr(state.gateways, "request", request)
    state.busy = True  # inspect owns the console now, previous job is finished.
    state.job = {"mode": "sim-only", "status": "completed"}
    try:
        assert state.runtime.run(state.managed_control(p, "stop"))["stop_response"]
        assert calls[0][1] == "stop"
        state.job["status"] = "running"
        result = state.runtime.run(state.managed_control(p, "stop"))
        assert "no device command" in result["message"]
        assert len(calls) == 1
    finally:
        state.runtime.close()
