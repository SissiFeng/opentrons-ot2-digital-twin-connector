import base64
import asyncio
import json
import threading

import pytest

from ot2_bridge.flex_console import make_server
from ot2_bridge.network import private_ipv4
from ot2_bridge.ot2_console import profile_template, validate_profile
from ot2_bridge.ot2_sila import OT2SiLATransport
from ot2_bridge.flex_sila import FlexSiLATransport
from test_flex_console import request


@pytest.mark.asyncio
async def test_sila_connection_timeout_names_backend_target_and_closes_channel(monkeypatch):
    grpc = pytest.importorskip("grpc")
    pytest.importorskip("sila2")

    class UnreachableChannel:
        closed = False

        async def channel_ready(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    channel = UnreachableChannel()
    monkeypatch.setattr(grpc.aio, "insecure_channel", lambda *args, **kwargs: channel)
    transport = OT2SiLATransport("100.122.149.108", 15051, timeout=0.01)
    with pytest.raises(RuntimeError, match=r"100\.122\.149\.108:15051.*timed out.*Bridge backend.*No robot command"):
        await transport.connect()
    assert channel.closed
    assert transport.channel is None
    assert transport.features == {}


@pytest.mark.parametrize("host", ["0.0.0.0", "8.8.8.8", "169.254.169.254", "host.example", "::1", None, []])
def test_no_wildcard_public_dns_or_link_local_target(host):
    with pytest.raises(ValueError):
        private_ipv4(host)


def test_ot2_mac_tailscale_endpoint_is_explicit_and_flex_stays_loopback():
    profile = profile_template()
    profile["real"]["host"] = "100.122.149.108"
    profile["real"]["port"] = 15051
    validate_profile(profile)
    assert OT2SiLATransport(profile["real"]["host"], 15051).host == "100.122.149.108"
    with pytest.raises(ValueError):
        FlexSiLATransport("100.122.149.108")
    for host in ("192.168.1.2", "10.0.0.2", "172.16.0.3"):
        with pytest.raises(ValueError, match="Tailscale"):
            OT2SiLATransport(host)
        profile["real"]["host"] = host
        with pytest.raises(ValueError, match="Tailscale"):
            validate_profile(profile)


def test_remote_console_cannot_bind_without_protection(tmp_path):
    with pytest.raises(ValueError, match="network console requires"):
        make_server(0, tmp_path, host="100.119.227.39")
    with pytest.raises(ValueError, match="--tailscale requires"):
        make_server(0, tmp_path, host="192.168.1.2", tailscale=True)


def test_all_routes_require_browser_login_and_review_is_available_on_backend(tmp_path):
    password = "fixture-only-not-a-real-password"
    path = tmp_path / "password"
    path.write_text(password)
    path.chmod(0o600)
    server = make_server(0, tmp_path / "runs", password_file=path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    auth = {"Authorization": "Basic " + base64.b64encode(f"bridge:{password}".encode()).decode()}
    try:
        for route in ("/", "/app.js", "/api/profile"):
            assert request(server, "GET", route)[0] == 401
            assert request(server, "GET", route, headers=auth)[0] == 200
        assert request(server, "POST", "/api/review", {"profile": profile_template()})[0] == 401
        assert server.state.review is None
        assert request(server, "GET", "/", headers={"Authorization": "Basic wrong"})[0] == 401
        assert request(server, "GET", "/api/profile", token=False, headers=auth)[0] == 400
        assert request(server, "GET", "/", headers={**auth, "Host": "evil.test"})[0] == 400
        assert (
            request(
                server,
                "POST",
                "/api/review",
                {"profile": profile_template()},
                headers={**auth, "Origin": "https://evil.test"},
            )[0]
            == 400
        )
        status, body = request(server, "POST", "/api/review", {"profile": profile_template()}, headers=auth)
        assert status == 200, body
        reviewed = json.loads(body)
        from pathlib import Path

        folder = Path(reviewed["backend_bundle_directory"])
        assert json.loads((folder / "plan.json").read_text()) == reviewed["plan"]
        assert (folder / "serve-sim.sh").is_file()
        assert (folder / "bridge-src/ot2_bridge/flex_matterix.py").is_file()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_browser_password_permissions_and_entropy_requirement(tmp_path):
    password = tmp_path / "password"
    password.write_text("short")
    password.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        make_server(0, tmp_path, password_file=password)
    password.chmod(0o600)
    with pytest.raises(ValueError, match="24 ASCII"):
        make_server(0, tmp_path, password_file=password)
