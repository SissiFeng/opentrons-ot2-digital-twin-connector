"""Local or authenticated Tailscale run UI, separate from the thin mapping core."""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import CancelledError
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shlex
import threading
import uuid
import zipfile
from urllib.parse import urlsplit, parse_qs

from .gateway import GatewayBroker, GatewayAdapter
from .application_records import ApplicationRecords, plan_identity
from .managed_matterix import ManagedMatterix
from .flex_runner import run_plan
from .remote import RemoteSimulation

from .instruments import make_plan, profile_template, validate_profile
from .flex_bundle import bundle_bytes
from .flex_cli import control, run_connected
from .wire import fingerprint
from .network import private_ipv4, is_tailscale_address, hub_endpoint


class ConsoleRuntime:
    """Keep every console gRPC channel on one event loop until shutdown.

    HTTP handlers use separate threads. Creating/closing an asyncio loop per
    request leaves gRPC completion callbacks pointing at those closed loops.
    The console owns this runtime; the thin bridge adapters do not own threads.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.lock = threading.Lock()
        self.closing = False
        self.thread = threading.Thread(target=self._serve, name="bridge-asyncio", daemon=True)
        self.thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            # Let each transport's finally block close its channel on the same
            # live loop. Cancelling an await does not stop the physical robot.
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    def run(self, coroutine):
        with self.lock:
            if self.closing or self.loop.is_closed():
                coroutine.close()
                raise RuntimeError("Bridge console is shutting down; physical stop is not confirmed")
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result()
        except CancelledError as error:
            raise RuntimeError("Bridge request cancelled during shutdown; physical stop is not confirmed") from error

    def close(self):
        with self.lock:
            if self.closing:
                return
            self.closing = True
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("Bridge shutdown is still pending; physical stop is not confirmed")


class ConsoleState:
    def __init__(self, output, site=None):
        self.token = secrets.token_urlsafe(32)
        self.output = Path(output).resolve()
        self.records = ApplicationRecords(self.output)
        self.lock = threading.Lock()
        self.review = None
        self.job = self.records.latest_job()
        self.worker = None
        self.busy = False
        self.stopping = False
        self.active_profile = None
        self.stop_requested = threading.Event()
        self.managed = site is not None
        self.require_rehearsal = (site or {}).get("require_rehearsal", False)
        if type(self.require_rehearsal) is not bool:
            self.records.close()
            raise ValueError("require_rehearsal must be true or false")
        try:
            self.gateways = GatewayBroker((site or {}).get("gateways", []), self.records)
            self.native = ManagedMatterix((site or {}).get("matterix"), (site or {}).get("matterix_by_device"))
            self.runtime = ConsoleRuntime()
        except BaseException:
            self.records.close()
            raise

    def recovery(self):
        return [job for job in self.records.jobs() if job.get("recovery_required")]

    def launch(self, mode, hardware, reviewed_fingerprint, rehearsal_run_id=None):
        """Consume a reviewed plan once; serialize this console's device actions."""
        with self.lock:
            if self.busy or self.stopping or not self.review:
                raise ValueError("Review the plan first; an active run cannot be replaced")
            if self.recovery():
                raise ValueError("Reconcile the held run before starting another run")
            if fingerprint(self.review) != reviewed_fingerprint:
                raise ValueError("Reviewed plan changed")
            plan = self.review
            if mode not in ("sim-only", "real-only", "shadow"):
                raise ValueError("Unsupported run mode")
            if hardware is not True:
                raise ValueError("The console connects to physical SiLA hardware; development simulation is CLI-only")
            if mode != "sim-only":
                validate_profile(plan["profile"], hardware=True)
                if rehearsal_run_id and self.records.rehearsal(plan, rehearsal_run_id) != rehearsal_run_id:
                    raise ValueError("Rehearsal must be a completed simulation of this workflow and qualified profile")
                if self.require_rehearsal and not rehearsal_run_id:
                    raise ValueError("This site requires a completed matching rehearsal before hardware execution")
            elif rehearsal_run_id:
                raise ValueError("A simulation cannot reference an earlier rehearsal as hardware authorization")
            if self.managed:
                key = self.gateways.match(plan["profile"])
                if mode != "sim-only" and self.gateways.devices[key]["fault"]:
                    raise ValueError(self.gateways.devices[key]["fault"])
            run_dir = self.output / plan["binding"]["run_id"]
            run_dir.mkdir(parents=True, exist_ok=False)
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2))
            self.review = None
            self.busy = True
            self.active_profile = plan["profile"]
            self.stop_requested.clear()
            self.job = {
                "status": "running",
                "events": [],
                "run_id": plan["binding"]["run_id"],
                "output": str(run_dir),
                "profile": plan["profile"],
                "mode": mode,
                **plan_identity(plan),
                "rehearsal_run_id": rehearsal_run_id,
            }
            self.records.save_job(self.job)

        def emit(event):
            with self.lock:
                self.job["events"].append(event)
                self.records.save_job(self.job)

        def work():
            try:
                result = self.runtime.run(
                    self.run_managed(plan, mode, run_dir, emit)
                    if self.managed
                    else run_connected(
                        plan, mode=mode, hardware=hardware, emit=emit, stop_requested=self.stop_requested.is_set
                    )
                )
            except Exception as error:
                result = {"status": "held", "error": f"{type(error).__name__}: {error}"}
            result.update(**plan_identity(plan), rehearsal_run_id=rehearsal_run_id)
            try:
                (run_dir / "report.json").write_text(json.dumps(result, indent=2))
            except OSError as error:
                result = {**result, "status": "held", "error": f"Report could not be saved: {error}"}
            with self.lock:
                self.busy = False
                self.active_profile = None
                self.job["status"] = result["status"]
                self.job["report"] = result
                self.job["recovery_required"] = result["status"] != "completed"
                self.records.save_job(self.job)
                if self.managed and mode != "sim-only" and result["status"] != "completed":
                    self.gateways.hold(
                        plan["profile"]["device_id"],
                        "Run held; verify the device and record reconciliation before a new run.",
                    )

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        return {"run_id": plan["binding"]["run_id"]}

    async def reconcile(self, profile, note, confirmed_idle):
        if confirmed_idle is not True or not isinstance(note, str) or not 8 <= len(note.strip()) <= 2000:
            raise ValueError(
                "Confirm the device/native process is idle and record an inspection note (8-2000 characters)"
            )
        key = profile["device_id"]
        physical_hold = any(job["profile"]["device_id"] == key and job["mode"] != "sim-only" for job in self.recovery())
        if self.native.process and self.native.process.poll() is None:
            raise ValueError("The owned Matterix process is still running")
        if self.managed:
            self.gateways.match(profile)
            physical_hold = physical_hold or bool(self.gateways.devices[key]["fault"])
        if self.managed and physical_hold:
            if not self.gateways.devices[key]["inspected"]:
                raise ValueError("Check connector before recording reconciliation")
            await self.gateways.request(key, "reconcile", {"note": note.strip()}, timeout=20)
            self.gateways.reconcile(key, note.strip())
        else:
            self.records.reconcile(key, note.strip())
        with self.lock:
            self.job = self.records.latest_job()
            self.review = None
        return {"message": "Reconciliation recorded. Previous outcomes stay unchanged; review a new run."}

    async def run_managed(self, plan, mode, directory, emit):
        real = GatewayAdapter(self.gateways, plan) if mode != "sim-only" else None
        begun = False
        result = {"binding": plan["binding"], "mode": mode, "status": "held", "steps": []}
        try:
            simulation = None
            if mode != "real-only":
                port = await self.native.start(plan, directory, emit, self.stop_requested.is_set)
                simulation = RemoteSimulation(port, timeout=90)
            if self.stop_requested.is_set():
                raise RuntimeError("Stop requested before device dispatch")
            if real:
                await real.begin()
                begun = True
            result = await run_plan(
                plan, mode=mode, real=real, simulation=simulation, emit=emit, stop_requested=self.stop_requested.is_set
            )
        except (Exception, asyncio.CancelledError) as error:
            result["error"] = f"{type(error).__name__}: {error}. No automatic retry; physical stop not confirmed."
            if mode != "real-only" and self.native.state != "ready":
                self.native.state, self.native.error = "failed", str(error)
        finally:
            for cleanup in ([real.finish] if begun else []) + [self.native.close]:
                try:
                    await cleanup()
                except (Exception, asyncio.CancelledError) as error:
                    result["status"] = "held"
                    result.setdefault("cleanup_errors", []).append(f"{type(error).__name__}: {error}")
                    result["error"] = result.get("error", "") + " Cleanup incomplete; reconcile before another run."
        return result

    async def managed_control(self, profile, action):
        key = self.gateways.match(profile)
        if action == "home":
            raise ValueError("Review the Home workflow and use Run device")
        if (
            action == "stop"
            and self.busy
            and self.job.get("status") == "running"
            and self.job.get("mode") == "sim-only"
        ):
            return {"message": "Simulation stop requested; no device command sent"}
        return await self.gateways.request(key, action, timeout=35 if action == "inspect" else 120)


def make_server(
    port,
    output,
    *,
    host="127.0.0.1",
    password_file=None,
    tailscale=False,
    site_config=None,
    public_url=None,
    trusted_proxy_loopback=False,
    local_gateway_port=None,
):
    host = private_ipv4(host)
    remote = not host.startswith("127.")
    if tailscale and not is_tailscale_address(host):
        raise ValueError("--tailscale requires binding to this machine's 100.64.0.0/10 Tailscale IPv4")
    if remote and not (password_file and tailscale):
        raise ValueError("A network console requires --password-file and --tailscale")
    public_origin = hub_endpoint(public_url)[0] if public_url else None
    if trusted_proxy_loopback and (
        remote or not password_file or not public_origin or not public_origin.startswith("https://")
    ):
        raise ValueError("Trusted proxy requires a loopback listener, password file and exact HTTPS public URL")
    if public_origin and not trusted_proxy_loopback:
        _, _, address, public_port = hub_endpoint(public_origin)
        if public_origin.startswith("https:") or address != host or public_port != port:
            raise ValueError(
                "Direct public URL must resolve to the listener and use its HTTP port; "
                "HTTPS requires --trusted-proxy-loopback"
            )
    if local_gateway_port is not None and not site_config:
        raise ValueError("A separate localhost gateway listener requires --site-config")
    expected_auth = None
    if password_file:
        password_path = Path(password_file)
        if password_path.stat().st_mode & 0o077:
            raise ValueError("Console password file must be private: chmod 600 FILE")
        password = password_path.read_text().strip()
        if len(password) < 24 or not password.isascii() or any(c.isspace() for c in password):
            raise ValueError("Console password must contain at least 24 ASCII characters without whitespace")
        expected_auth = "Basic " + base64.b64encode(f"bridge:{password}".encode()).decode()
    site = json.loads(Path(site_config).read_text()) if site_config else None
    state = ConsoleState(output, site)
    static = Path(__file__).parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_args):
            pass  # Do not log local session tokens or hardware configuration.

        def respond(self, status, content, mime="application/json"):
            if not isinstance(content, bytes):
                content = json.dumps(content).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
                "script-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def trusted(self, *, api=False):
            authority = f"{host}:{self.server.server_port}"
            local_origin = f"http://127.0.0.1:{self.server.server_port}"
            origins = {local_origin} if getattr(self.server, "gateway_only", False) else {f"http://{authority}"}
            if public_origin and not getattr(self.server, "gateway_only", False):
                origins.add(public_origin)
            matched = [origin for origin in origins if urlsplit(origin).netloc == self.headers.get("Host")]
            if not matched:
                raise ValueError("Use the printed console URL")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in (origins if trusted_proxy_loopback else matched):
                raise ValueError("Cross-origin requests are not allowed")
            if api and not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), state.token):
                raise ValueError("Console session token is missing")

        def authenticated(self):
            if getattr(self.server, "gateway_only", False):
                self.respond(404, {"error": "Gateway-only listener"})
                return False
            if tailscale and not is_tailscale_address(self.client_address[0]):
                self.respond(403, {"error": "Connect through Tailscale"})
                return False
            if expected_auth is None or secrets.compare_digest(
                self.headers.get("Authorization", "").encode(), expected_auth.encode()
            ):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Lab Bridge"')
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return False

        def do_GET(self):
            if not self.authenticated():
                return
            try:
                self.trusted(api=self.path.startswith("/api/"))
                if self.path in ("/", "/app.js", "/style.css"):
                    name = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[self.path]
                    mime = {"/": "text/html; charset=utf-8", "/app.js": "text/javascript", "/style.css": "text/css"}[
                        self.path
                    ]
                    content = (static / name).read_bytes().replace(b"__BRIDGE_TOKEN__", state.token.encode())
                    self.respond(200, content, mime)
                elif self.path in ("/api/profile", "/api/profile/ot2", "/api/profile/flex"):
                    instrument = "flex" if self.path.endswith("/flex") else "ot2"
                    self.respond(200, profile_template(instrument))
                elif self.path == "/api/site":
                    self.respond(
                        200,
                        {
                            "managed": state.managed,
                            "devices": state.gateways.catalog(),
                            "native_configured": bool(state.native.config or state.native.runtime_by_device),
                            "require_rehearsal": state.require_rehearsal,
                            "recovery": [
                                {"run_id": job["run_id"], "device_id": job["profile"]["device_id"], "mode": job["mode"]}
                                for job in state.recovery()
                            ],
                        },
                    )
                elif self.path == "/api/native":
                    self.respond(200, state.native.snapshot())
                elif urlsplit(self.path).path == "/api/native/frame":
                    run_id = parse_qs(urlsplit(self.path).query).get("run_id", [""])[0]
                    self.respond(200, state.native.frame(run_id), "image/png")
                elif self.path == "/api/job":
                    with state.lock:
                        snapshot = json.loads(json.dumps(state.job))
                    self.respond(200, snapshot)
                elif self.path == "/api/bundle":
                    with state.lock:
                        plan = state.review
                    if plan is None:
                        raise ValueError("Review a new plan before exporting")
                    self.respond(200, bundle_bytes(plan), "application/zip")
                else:
                    self.respond(404, {"error": "Not found"})
            except (ValueError, OSError) as error:
                self.respond(400, {"error": str(error)})

        def gateway_post(self):
            try:
                self.trusted()
                if (
                    tailscale
                    and not getattr(self.server, "gateway_only", False)
                    and not is_tailscale_address(self.client_address[0])
                ):
                    raise ValueError("Connect through Tailscale")
                key = self.headers.get("X-Device-ID", "")
                credential = state.gateways.authorize(
                    key, self.headers.get("Authorization", ""), self.headers.get("X-Gateway-ID", "default")
                )
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("JSON required")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100_000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if self.path == "/gateway/register":
                    value = state.gateways.register(key, data["profile"], credential)
                elif self.path == "/gateway/poll":
                    value = {"command": state.gateways.poll(key, data["session"], credential=credential)}
                elif self.path == "/gateway/result":
                    state.gateways.result(key, data["session"], data["result"], credential)
                    value = {"accepted": True}
                else:
                    raise ValueError("Unknown gateway route")
                self.respond(200, value)
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                self.respond(400, {"error": str(error)})

        def do_POST(self):
            if self.path.startswith("/gateway/"):
                self.gateway_post()
                return
            if not self.authenticated():
                return
            try:
                self.trusted(api=True)
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("JSON required")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 100_000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if self.path == "/api/review":
                    if state.managed:
                        state.gateways.match(data["profile"])
                    plan = make_plan(data["profile"], str(uuid.uuid4()))
                    with state.lock:
                        if state.busy or state.stopping:
                            raise ValueError("Wait for the active run before reviewing another plan")
                        # Keep the exact bundle on the backend host as well as
                        # offering a download to a browser on another machine.
                        folder = state.output / "reviewed" / plan["binding"]["run_id"]
                        folder.mkdir(parents=True, exist_ok=False)
                        with zipfile.ZipFile(io.BytesIO(bundle_bytes(plan))) as archive:
                            archive.extractall(folder)
                        state.review = plan
                    self.respond(
                        200,
                        {
                            "plan": plan,
                            "managed": state.managed,
                            "fingerprint": fingerprint(plan),
                            "backend_bundle_directory": str(folder),
                            "serve_command": f"cd {shlex.quote(str(folder))} && sh serve-sim.sh",
                            **plan_identity(plan),
                            "rehearsal_run_id": state.records.rehearsal(plan),
                        },
                    )
                elif self.path == "/api/run":
                    with state.lock:
                        if state.review is None or fingerprint(state.review) != data["fingerprint"]:
                            raise ValueError("Reviewed plan changed; review and export again")
                    self.respond(
                        202,
                        state.launch(data["mode"], data["hardware"], data["fingerprint"], data.get("rehearsal_run_id")),
                    )
                elif self.path == "/api/reconcile":
                    profile = validate_profile(data["profile"])
                    with state.lock:
                        if state.busy or state.stopping:
                            raise ValueError("Wait for pending operations before reconciliation")
                        state.busy = True
                    try:
                        self.respond(
                            200,
                            state.runtime.run(state.reconcile(profile, data.get("note"), data.get("confirmed_idle"))),
                        )
                    finally:
                        with state.lock:
                            state.busy = False
                elif self.path in ("/api/inspect", "/api/home", "/api/stop"):
                    action = self.path.rsplit("/", 1)[1]
                    profile = validate_profile(data["profile"])
                    if action == "home" and data.get("hardware") is not True:
                        raise ValueError("Explicit physical hardware mode required")
                    with state.lock:
                        if state.stopping:
                            raise ValueError("Wait for the pending Stop response")
                        if state.busy and action != "stop":
                            raise ValueError("A run is active; only Stop is available")
                        if action == "home" and state.recovery():
                            raise ValueError("Reconcile the held run before homing")
                        if action == "stop":
                            if state.active_profile is not None and fingerprint(profile) != fingerprint(
                                state.active_profile
                            ):
                                raise ValueError(
                                    "Stop target differs from the active instrument profile; use the active run tab"
                                )
                            state.stopping = True
                            state.stop_requested.set()
                        else:
                            state.busy = True
                            state.active_profile = profile
                            state.stop_requested.clear()
                    try:
                        result = state.runtime.run(
                            state.managed_control(profile, action)
                            if state.managed
                            else control(
                                profile,
                                action,
                                hardware=data.get("hardware"),
                                stop_requested=state.stop_requested.is_set,
                            )
                        )
                    finally:
                        with state.lock:
                            if action == "stop":
                                state.stopping = False
                            else:
                                state.busy = False
                                state.active_profile = None
                    self.respond(200, result)
                else:
                    self.respond(404, {"error": "Not found"})
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                self.respond(400, {"error": f"{type(error).__name__}: {error}"})

    class ConsoleServer(ThreadingHTTPServer):
        def server_close(self):
            try:
                super().server_close()
            finally:
                if getattr(self, "local_gateway", None):
                    self.local_gateway.shutdown()
                    self.local_gateway.server_close()
                    self.local_gateway = None
                state.gateways.close()
                if not state.runtime.closing:
                    state.runtime.run(state.native.close())
                state.runtime.close()
                if state.worker:
                    state.worker.join(timeout=15)
                if state.worker and state.worker.is_alive():
                    raise RuntimeError(
                        "Run cleanup remains active; retain the Hub journal and reconcile before restarting"
                    )
                state.records.close()

    server = None
    try:
        server = ConsoleServer((host, port), Handler)
        server.local_gateway = None
        if local_gateway_port is not None:
            local = ThreadingHTTPServer(("127.0.0.1", local_gateway_port), Handler)
            local.gateway_only = True
            server.local_gateway = local
            server.gateway_url = f"http://127.0.0.1:{local.server_port}"
            threading.Thread(target=local.serve_forever, daemon=True).start()
    except BaseException:
        if server:
            ThreadingHTTPServer.server_close(server)
        state.runtime.close()
        state.records.close()
        raise
    server.url = public_origin or f"http://{host}:{server.server_port}"
    server.state = state
    return server


def serve(port=8088, output="bridge-runs", **kwargs):
    server = make_server(port, output, **kwargs)
    print(f"Bridge console: {server.url}", flush=True)
    if getattr(server, "gateway_url", None):
        print(f"Same-host gateway URL: {server.gateway_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
