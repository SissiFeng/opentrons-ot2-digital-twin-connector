import http.client
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
