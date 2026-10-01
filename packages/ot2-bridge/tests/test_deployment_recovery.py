"""CPU-only restart, migration, origin and rehearsal acceptance; no robot or GPU."""

import asyncio
import base64
import copy
import http.client
import json
import socket
import threading

import pytest

from ot2_bridge.application_records import ApplicationRecords, plan_identity
from ot2_bridge.flex_console import ConsoleState, make_server
from ot2_bridge.gateway import GatewayBroker
from ot2_bridge.gateway_agent import DeviceExecutor, hub_url
from ot2_bridge.gateway_credentials import issue
from ot2_bridge.instruments import make_plan
from ot2_bridge.network import hub_endpoint, hub_json
from ot2_bridge.wire import fingerprint
from test_flex_console import request
from test_gateway_application import Backend, command, configuration


def stored_job(plan, mode="real-only", status="running"):
    return {
        "run_id": plan["binding"]["run_id"],
        "profile": plan["profile"],
        "mode": mode,
        "status": status,
        "events": [],
        **plan_identity(plan),
    }


def test_restarted_hub_retains_unknown_dispatch_and_excludes_second_owner(tmp_path):
    p, entry, _ = configuration(tmp_path)
    root = tmp_path / "runs"
    records = ApplicationRecords(root)
    with pytest.raises(RuntimeError, match="Another Hub"):
        ApplicationRecords(root)
    plan = make_plan(p, "crashed-run")
    records.save_job(stored_job(plan))
    records.intent(p["device_id"], command("begin", plan))
    records.close()
    restored = ConsoleState(root, {"gateways": [entry]})
    try:
        assert restored.job["status"] == "held"
        assert restored.job["recovery_required"]
        restored.review = make_plan(p, "new-run")
        with pytest.raises(ValueError, match="Reconcile"):
            restored.launch("real-only", True, fingerprint(restored.review))
        broker = restored.gateways
        session = broker.register(p["device_id"], p)["session"]
        assert broker.poll(p["device_id"], session, wait=0) is None
        with pytest.raises(RuntimeError, match="reconcile"):
            restored.runtime.run(broker.request(p["device_id"], "begin", restored.review))
        with pytest.raises(ValueError, match="Check connector"):
            restored.runtime.run(restored.reconcile(p, "Device inspected and idle", True))
        assert restored.job["status"] == "held"
    finally:
        restored.runtime.close()
        restored.records.close()


def test_unfinished_legacy_plan_is_imported_as_held(tmp_path):
    p, _, _ = configuration(tmp_path)
    folder = tmp_path / "runs" / "old-run"
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text(json.dumps(make_plan(p, "old-run")))
    records = ApplicationRecords(folder.parent)
    try:
        assert records.latest_job()["recovery_required"]
        assert records.latest_job()["status"] == "held"
    finally:
        records.close()


def test_reconciliation_is_explicit_and_preserves_failed_outcomes(tmp_path):
    p, _, _ = configuration(tmp_path)
    state = ConsoleState(tmp_path / "runs")
    job = stored_job(make_plan(p, "sim-failed"), "sim-only", "held")
    job.update(recovery_required=True, report={"status": "held", "error": "Native startup failed"})
    state.records.save_job(job)
    try:
        with pytest.raises(ValueError, match="Confirm"):
            state.runtime.run(state.reconcile(p, "Native process is idle", False))
        state.runtime.run(state.reconcile(p, "Native process checked and closed", True))
        assert not state.recovery()
        assert state.job["status"] == "held"
        assert state.job["report"]["error"] == "Native startup failed"
        assert state.records.db.execute("SELECT count(*) FROM reconciliations").fetchone()[0] == 1
    finally:
        state.runtime.close()
        state.records.close()


def test_revoked_gateway_cannot_keep_polling_or_submit_late_results(tmp_path):
    p, entry, secret = configuration(tmp_path)
    records = ApplicationRecords(tmp_path / "runs")
    broker = GatewayBroker([entry], records)
    key = p["device_id"]
    old = broker.authorize(key, "Bearer " + secret)
    session = broker.register(key, p, old)["session"]

    async def scenario():
        pending = asyncio.create_task(broker.request(key, "begin", make_plan(p, "old-run")))
        await asyncio.sleep(0)
        delivered = broker.poll(key, session, wait=0, credential=old)
        issue(entry, "replacement", tmp_path / "new-token", replace=True)
        with pytest.raises(ValueError, match="credential"):
            broker.authorize(key, "Bearer " + secret)
        with pytest.raises(RuntimeError, match="replaced/revoked"):
            await pending
        with pytest.raises(ValueError, match="active"):
            broker.result(key, session, {"id": delivered["id"], "error": "", "value": {}}, old)
        current = broker.authorize(key, "Bearer " + (tmp_path / "new-token").read_text().strip(), "replacement")
        new_session = broker.register(key, p, current)["session"]
        assert new_session != session
        with pytest.raises(RuntimeError, match="reconcile"):
            await broker.request(key, "begin", make_plan(p, "new-run"))
        assert records.device(key)["fault"]

    try:
        asyncio.run(scenario())
    finally:
        broker.close()
        records.close()


def test_replacement_needs_fresh_identity_check_and_recorded_reconciliation(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "device-locks"))
    p, entry, _ = configuration(tmp_path)
    records = ApplicationRecords(tmp_path / "runs")
    records.hold(p["device_id"], "Unknown physical outcome")
    broker = GatewayBroker([entry], records)
    session = broker.register(p["device_id"], p)["session"]
    executor = DeviceExecutor(p, Backend(p), tmp_path / "ledger")

    async def roundtrip(kind, payload=None):
        pending = asyncio.create_task(broker.request(p["device_id"], kind, payload))
        await asyncio.sleep(0)
        cmd = broker.poll(p["device_id"], session, wait=0)
        value = await executor.dispatch(cmd)
        broker.result(p["device_id"], session, {"id": cmd["id"], "error": "", "value": value})
        return await pending

    async def scenario():
        with pytest.raises(ValueError, match="Check connector"):
            broker.reconcile(p["device_id"], "Physical device inspected")
        await roundtrip("inspect")
        await roundtrip("reconcile", {"note": "Previous gateway stopped; device inspected and idle"})
        broker.reconcile(p["device_id"], "Previous gateway stopped; device inspected and idle")
        assert not records.device(p["device_id"])["fault"]
        plan = make_plan(p, "after-inspection")
        await roundtrip("begin", plan)
        await roundtrip("finish", plan["binding"])
        assert records.device(p["device_id"])["active_run"] is None

    try:
        asyncio.run(scenario())
    finally:
        broker.close()
        records.close()


def test_transport_change_preserves_qualified_identity_but_not_full_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "locks"))
    p, _, _ = configuration(tmp_path)
    moved = copy.deepcopy(p)
    moved["real"]["port"] += 1
    first, second = make_plan(p, "sim"), make_plan(moved, "real")
    a, b = plan_identity(first), plan_identity(second)
    assert a["workflow_sha256"] == b["workflow_sha256"]
    assert a["qualified_profile_sha256"] == b["qualified_profile_sha256"]
    assert a["plan_sha256"] != b["plan_sha256"]
    executor = DeviceExecutor(moved, Backend(moved), tmp_path / "ledger")

    async def run():
        await executor.dispatch(command("begin", first))
        await executor.dispatch(command("finish", first["binding"]))
        wrong = copy.deepcopy(p)
        wrong["real"]["server_uuid"] = "other-device"
        with pytest.raises(ValueError, match="differs"):
            await executor.dispatch(command("begin", make_plan(wrong, "wrong")))

    asyncio.run(run())


def test_required_rehearsal_is_checked_against_durable_result(tmp_path, monkeypatch):
    p, entry, _ = configuration(tmp_path)
    state = ConsoleState(tmp_path / "runs", {"gateways": [entry], "require_rehearsal": True})
    completed = stored_job(make_plan(p, "rehearsal"), "sim-only", "completed")
    state.records.save_job(completed)
    state.review = make_plan(p, "hardware")

    async def fixture(*args):
        return {"status": "completed", "steps": [], "fixture": True}

    monkeypatch.setattr(state, "run_managed", fixture)
    try:
        with pytest.raises(ValueError, match="requires"):
            state.launch("real-only", True, fingerprint(state.review))
        with pytest.raises(ValueError, match="Rehearsal"):
            state.launch("real-only", True, fingerprint(state.review), "missing-run")
        state.launch("real-only", True, fingerprint(state.review), "rehearsal")
        state.worker.join(3)
        assert state.job["status"] == "completed"
        assert state.job["rehearsal_run_id"] == "rehearsal"
        assert json.loads((tmp_path / "runs/hardware/report.json").read_text())["rehearsal_run_id"] == "rehearsal"
        changed = copy.deepcopy(p)
        changed["real"]["server_uuid"] = "new-robot"
        assert state.records.rehearsal(make_plan(changed, "another")) is None
    finally:
        state.runtime.close()
        state.records.close()


def magicdns(monkeypatch, address="100.100.1.1"):
    original = socket.getaddrinfo
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]
            if str(host).endswith(".ts.net")
            else original(host, port, *a, **kw)
        ),
    )


def test_explicit_magicdns_https_validation(monkeypatch):
    magicdns(monkeypatch)
    assert hub_url("https://sim.example.ts.net") == "https://sim.example.ts.net"
    assert hub_url("http://sim.example.ts.net:8088") == "http://sim.example.ts.net:8088"
    for url in ("https://evil.example:443", "http://sim.example.ts.net@evil.com", "https://sim.example.ts.net/path"):
        with pytest.raises(ValueError):
            hub_url(url)
    magicdns(monkeypatch, "192.168.1.1")
    with pytest.raises(ValueError):
        hub_endpoint("https://sim.example.ts.net")


def test_loopback_proxy_requires_password_and_exact_public_origin(tmp_path, monkeypatch):
    magicdns(monkeypatch)
    url = "https://sim.example.ts.net"
    with pytest.raises(ValueError, match="Trusted proxy"):
        make_server(0, tmp_path / "bad", public_url=url, trusted_proxy_loopback=True)
    with pytest.raises(ValueError, match="HTTPS requires"):
        make_server(0, tmp_path / "bad", public_url=url)
    password = tmp_path / "password"
    password.write_text("fixture-only-browser-password-12345")
    password.chmod(0o600)
    server = make_server(0, tmp_path / "runs", password_file=password, public_url=url, trusted_proxy_loopback=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    auth = "Basic " + base64.b64encode(f"bridge:{password.read_text()}".encode()).decode()
    try:
        assert request(server, "GET", "/", headers={"Host": "sim.example.ts.net"})[0] == 401
        headers = {"Authorization": auth, "Host": "sim.example.ts.net", "Origin": url}
        assert request(server, "GET", "/api/site", headers=headers)[0] == 200
        # Proxy may rewrite Host to its local backend. Public Origin is still exact.
        assert request(server, "GET", "/api/site", headers={"Authorization": auth, "Origin": url})[0] == 200
        for invalid in ({"Host": "another.example.ts.net"}, {"Origin": "https://evil.example"}):
            assert request(server, "GET", "/api/site", headers={**headers, **invalid})[0] == 400
        assert request(server, "GET", "/api/site", token=False, headers=headers)[0] == 400
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_local_gateway_listener_does_not_expose_browser_api(tmp_path):
    p, entry, secret = configuration(tmp_path)
    site = tmp_path / "site.json"
    site.write_text(json.dumps({"gateways": [entry]}))
    server = make_server(0, tmp_path / "runs", site_config=site, local_gateway_port=0)
    try:
        local = server.local_gateway
        conn = http.client.HTTPConnection("127.0.0.1", local.server_port)
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 404
        response.read()
        conn.close()
        with pytest.raises(RuntimeError, match="HTTP 400"):
            hub_json(server.gateway_url, "/gateway/register", {"profile": p}, {"Content-Type": "application/json"})
        result = hub_json(
            server.gateway_url,
            "/gateway/register",
            {"profile": p},
            {"Content-Type": "application/json", "X-Device-ID": p["device_id"], "Authorization": "Bearer " + secret},
        )
        assert result["session"]
    finally:
        server.server_close()


def test_gateway_http_never_follows_redirect_or_proxy(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Redirect(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "http://not-allowed.example")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("http_proxy", "http://not-allowed.example")
    try:
        with pytest.raises(RuntimeError, match="HTTP 302"):
            hub_json(f"http://127.0.0.1:{server.server_port}", "/gateway/register", {}, {})
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_https_pins_validated_address_and_verifies_ca_and_hostname(tmp_path, monkeypatch):
    import shutil
    import ssl
    import subprocess
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("TLS fixture requires openssl to create a temporary certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=sim.example.ts.net",
            "-addext",
            "subjectAltName=DNS:sim.example.ts.net",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )

    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Length", "16")
            self.end_headers()
            self.wfile.write(b'{"fixture":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    magicdns(monkeypatch)
    original = socket.create_connection
    targets = []

    def fixture_route(target, timeout, source_address=None):
        targets.append(target)
        assert target == ("100.100.1.1", server.server_port)
        return original(("127.0.0.1", target[1]), timeout, source_address)

    monkeypatch.setattr(socket, "create_connection", fixture_route)
    url = f"https://sim.example.ts.net:{server.server_port}"
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            hub_json(url, "/gateway/register", {}, {})
        monkeypatch.setattr(ssl, "_create_default_https_context", lambda: ssl.create_default_context(cafile=cert))
        assert hub_json(url, "/gateway/register", {}, {}) == {"fixture": True}
        with pytest.raises(ssl.SSLCertVerificationError):
            hub_json(url.replace("sim.example", "other.example"), "/gateway/register", {}, {})
        assert len(targets) == 3
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_process_death_preserves_committed_run_and_dispatch(tmp_path):
    import subprocess
    import sys

    p, _, _ = configuration(tmp_path)
    root = tmp_path / "runs"
    script = """
import json, os, signal, sys
from ot2_bridge.application_records import ApplicationRecords, plan_identity
from ot2_bridge.instruments import make_plan
profile = json.loads(open(sys.argv[1]).read())
plan = make_plan(profile, 'killed-process')
records = ApplicationRecords(sys.argv[2])
records.save_job({'run_id': 'killed-process', 'profile': profile, 'mode': 'real-only',
                 'status': 'running', 'events': [], **plan_identity(plan)})
records.intent(profile['device_id'], {'id': 'delivered-once', 'kind': 'begin', 'payload': plan})
os.kill(os.getpid(), signal.SIGKILL)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "device.json"), str(root)],
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == -9, result.stderr.decode()
    restored = ApplicationRecords(root)
    try:
        assert restored.latest_job()["recovery_required"]
        assert restored.device(p["device_id"])["fault"]
        assert restored.db.execute("SELECT state FROM dispatch WHERE id='delivered-once'").fetchone() == ("unknown",)
    finally:
        restored.close()
