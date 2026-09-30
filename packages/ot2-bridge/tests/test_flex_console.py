import http.client
import asyncio
import json
import threading

import pytest

from ot2_bridge.flex import profile_template
from ot2_bridge.flex_console import make_server


@pytest.fixture
def console(tmp_path):
    server = make_server(0, tmp_path / "runs")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, method, path, data=None, *, token=True, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Bridge-Token"] = server.state.token
    h.update(headers or {})
    connection.request(method, path, body=json.dumps(data) if data is not None else None, headers=h)
    result = connection.getresponse()
    status, raw = result.status, result.read()
    connection.close()
    return status, raw


def test_repeated_http_checks_keep_one_live_event_loop(console, monkeypatch):
    from ot2_bridge import flex_console

    loops = []

    async def inspect(*args, **kwargs):
        loops.append(asyncio.get_running_loop())
        return {"read_only_fixture": True}

    monkeypatch.setattr(flex_console, "control", inspect)
    for _ in range(3):
        assert request(console, "POST", "/api/inspect", {"profile": profile_template()})[0] == 200
    assert len(set(loops)) == 1
    assert not loops[0].is_closed()
    console.server_close()
    assert loops[0].is_closed()


def test_stop_can_share_the_loop_with_a_pending_check(console, monkeypatch):
    from ot2_bridge import flex_console

    started = threading.Event()
    loops = []
    gate = None
    result = []

    async def control(profile, action, **kwargs):
        nonlocal gate
        loops.append(asyncio.get_running_loop())
        if action == "inspect":
            gate = asyncio.Event()
            started.set()
            await gate.wait()
        else:
            assert action == "stop"
            gate.set()
        return {"test_action": action}

    monkeypatch.setattr(flex_console, "control", control)
    thread = threading.Thread(
        target=lambda: result.append(request(console, "POST", "/api/inspect", {"profile": profile_template()}))
    )
    thread.start()
    assert started.wait(2)
    try:
        assert request(console, "POST", "/api/stop", {"profile": profile_template()})[0] == 200
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert result[0][0] == 200
    assert len(set(loops)) == 1


def test_console_shutdown_finishes_async_cleanup_before_closing_loop():
    from ot2_bridge.flex_console import ConsoleRuntime

    runtime = ConsoleRuntime()
    started = threading.Event()
    closed_on_live_loop = []
    errors = []

    async def pending():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed_on_live_loop.append(not asyncio.get_running_loop().is_closed())

    def work():
        try:
            runtime.run(pending())
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=work)
    thread.start()
    assert started.wait(2)
    runtime.close()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert closed_on_live_loop == [True]
    assert runtime.loop.is_closed()
    assert errors == ["Bridge request cancelled during shutdown; physical stop is not confirmed"]


def test_console_repeated_sila_checks_use_the_owned_runtime(console):
    pytest.importorskip("sila2")
    pytest.importorskip("unitelabs.cdk")
    from unitelabs.cdk import Connector, SiLAServerConfig
    from unitelabs.opentrons_ot2 import OpentronsOt2Config
    from unitelabs.opentrons_ot2.features.motion_control import MotionControlFeature
    from unitelabs.opentrons_ot2.io import OT2MotionController
    from ot2_bridge.ot2_console import profile_template as ot2_profile

    loop_errors = []

    async def setup():
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: loop_errors.append(context))
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
        return controller, connector

    controller, connector = console.state.runtime.run(setup())
    profile = ot2_profile()
    profile["real"]["port"] = int(connector.sila_server._address.rsplit(":", 1)[1])
    try:
        for _ in range(3):
            status, body = request(console, "POST", "/api/inspect", {"profile": profile})
            assert status == 200, body
            assert json.loads(body)["is_simulating"] is True
    finally:

        async def cleanup():
            await connector.stop()
            await controller.disconnect()

        console.state.runtime.run(cleanup())
    assert loop_errors == []


def test_console_serves_real_assets_and_reviews_sim_only(console):
    assert request(console, "GET", "/", token=False)[0] == 200
    assert request(console, "GET", "/app.js", token=False)[0] == 200
    status, body = request(console, "POST", "/api/review", {"profile": profile_template()})
    assert status == 200, body
    review = json.loads(body)
    assert review["plan"]["operations"][0]["action"] == "pick_up_tip"
    status, data = request(console, "GET", "/api/bundle")
    assert status == 200 and data.startswith(b"PK")


@pytest.mark.parametrize(
    "headers,token", [({}, False), ({"Origin": "https://evil.test"}, True), ({"Host": "evil.test"}, True)]
)
def test_foreign_requests_cannot_review_or_run(console, headers, token):
    status, _ = request(console, "POST", "/api/review", {"profile": profile_template()}, headers=headers, token=token)
    assert status == 400
    assert console.state.review is None


def test_unqualified_hardware_plan_cannot_launch(console):
    _, body = request(console, "POST", "/api/review", {"profile": profile_template()})
    fingerprint = json.loads(body)["fingerprint"]
    status, _ = request(
        console, "POST", "/api/run", {"fingerprint": fingerprint, "mode": "real-only", "hardware": True}
    )
    assert status == 400
    assert console.state.job["status"] == "idle"


def test_changed_review_cannot_launch(console):
    _, body = request(console, "POST", "/api/review", {"profile": profile_template()})
    old = json.loads(body)["fingerprint"]
    request(console, "POST", "/api/review", {"profile": profile_template()})
    status, _ = request(console, "POST", "/api/run", {"fingerprint": old, "mode": "sim-only", "hardware": False})
    assert status == 400
    assert not console.state.busy


def test_unknown_mode_and_malformed_request_rejected(console):
    assert request(console, "POST", "/api/home", {"profile": profile_template()})[0] == 400
    assert request(console, "POST", "/api/review", [])[0] == 400


def test_console_ot2_default_and_flex_selection_are_separate(console):
    _, body = request(console, "GET", "/api/profile")
    ot2 = json.loads(body)
    assert ot2["schema"] == "ot2.profile/1"
    assert json.loads(request(console, "GET", "/api/profile/flex")[1])["schema"] == "flex.profile/1"
    status, body = request(console, "POST", "/api/review", {"profile": ot2})
    assert status == 200
    review = json.loads(body)
    assert [op["action"] for op in review["plan"]["operations"]] == ["home", "read_position"]
    assert request(console, "GET", "/api/bundle")[1].startswith(b"PK")
    # UI cannot opt into connector simulation, even with a valid reviewed plan.
    assert (
        request(
            console, "POST", "/api/run", {"fingerprint": review["fingerprint"], "mode": "real-only", "hardware": False}
        )[0]
        == 400
    )


def test_stop_from_another_instrument_tab_is_rejected_before_signal(console, monkeypatch):
    from ot2_bridge import flex_console
    from ot2_bridge.ot2_console import profile_template as ot2_profile

    called = []

    async def control(*args, **kwargs):
        called.append(args)
        return {}

    monkeypatch.setattr(flex_console, "control", control)
    with console.state.lock:
        console.state.busy = True
        console.state.active_profile = profile_template()
    status, body = request(console, "POST", "/api/stop", {"profile": ot2_profile()})
    assert status == 400 and b"Stop target differs" in body
    assert not console.state.stop_requested.is_set()
    assert called == []
    status, _ = request(console, "POST", "/api/stop", {"profile": profile_template()})
    assert status == 200
    assert console.state.stop_requested.is_set()
    assert len(called) == 1


def test_pending_stop_blocks_new_review_and_launch(console):
    _, body = request(console, "POST", "/api/review", {"profile": profile_template()})
    review = json.loads(body)
    console.state.stopping = True
    assert request(console, "POST", "/api/review", {"profile": profile_template()})[0] == 400
    assert (
        request(
            console, "POST", "/api/run", {"fingerprint": review["fingerprint"], "mode": "sim-only", "hardware": True}
        )[0]
        == 400
    )


def test_console_physical_ot2_run_routes_and_records_without_matterix(console, monkeypatch):
    import time
    from ot2_bridge import flex_console
    from ot2_bridge.ot2_console import profile_template as ot2_profile
    from ot2_bridge.flex_runner import run_plan
    from ot2_bridge.ot2_console import OT2HomeAdapter
    from test_ot2_console import TestTransport

    async def connected(plan, *, mode, hardware, emit, stop_requested):
        assert hardware is True and mode == "real-only"
        return await run_plan(
            plan,
            mode=mode,
            real=OT2HomeAdapter(plan["profile"], TestTransport()),
            emit=emit,
            stop_requested=stop_requested,
        )

    monkeypatch.setattr(flex_console, "run_connected", connected)
    profile = ot2_profile()
    profile["real"]["server_uuid"] = "bound-ot2"
    _, body = request(console, "POST", "/api/review", {"profile": profile})
    review = json.loads(body)
    assert (
        request(
            console, "POST", "/api/run", {"fingerprint": review["fingerprint"], "mode": "real-only", "hardware": True}
        )[0]
        == 202
    )
    deadline = time.monotonic() + 2
    while console.state.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert console.state.job["status"] == "completed"
    assert console.state.active_profile is None
    from pathlib import Path

    report = json.loads((Path(console.state.job["output"]) / "report.json").read_text())
    assert len(report["steps"]) == 2
    assert report["comparison_scope"].startswith("completion-only")
