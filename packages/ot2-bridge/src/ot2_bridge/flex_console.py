"""Local or authenticated Tailscale run UI, separate from the thin mapping core."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shlex
import threading
import uuid
import zipfile

from .instruments import make_plan, profile_template, validate_profile
from .flex_bundle import bundle_bytes
from .flex_cli import control, run_connected
from .wire import fingerprint
from .network import private_ipv4, is_tailscale_address


class ConsoleState:
    def __init__(self, output):
        self.token = secrets.token_urlsafe(32)
        self.output = Path(output).resolve()
        self.lock = threading.Lock()
        self.review = None
        self.job = {"status": "idle", "events": []}
        self.busy = False
        self.stopping = False
        self.active_profile = None
        self.stop_requested = threading.Event()

    def launch(self, mode, hardware, reviewed_fingerprint):
        """Consume a reviewed plan once; serialize this console's device actions."""
        with self.lock:
            if self.busy or self.stopping or not self.review:
                raise ValueError("Review the plan first; an active run cannot be replaced")
            if fingerprint(self.review) != reviewed_fingerprint:
                raise ValueError("Reviewed plan changed")
            plan = self.review
            if mode not in ("sim-only", "real-only", "shadow"):
                raise ValueError("Unsupported run mode")
            if hardware is not True:
                raise ValueError("The console connects to physical SiLA hardware; development simulation is CLI-only")
            if mode != "sim-only":
                validate_profile(plan["profile"], hardware=True)
            run_dir = self.output / plan["binding"]["run_id"]
            run_dir.mkdir(parents=True, exist_ok=False)
            (run_dir / "plan.json").write_text(json.dumps(plan, indent=2))
            self.review = None
            self.busy = True
            self.active_profile = plan["profile"]
            self.stop_requested.clear()
            self.job = {"status": "running", "events": [], "run_id": plan["binding"]["run_id"], "output": str(run_dir)}

        def emit(event):
            with self.lock:
                self.job["events"].append(event)

        def work():
            try:
                result = asyncio.run(
                    run_connected(
                        plan, mode=mode, hardware=hardware, emit=emit, stop_requested=self.stop_requested.is_set
                    )
                )
            except Exception as error:
                result = {"status": "held", "error": f"{type(error).__name__}: {error}"}
            try:
                (run_dir / "report.json").write_text(json.dumps(result, indent=2))
            except OSError as error:
                result = {**result, "status": "held", "error": f"Report could not be saved: {error}"}
            with self.lock:
                self.busy = False
                self.active_profile = None
                self.job["status"] = result["status"]
                self.job["report"] = result

        threading.Thread(target=work, daemon=True).start()
        return {"run_id": plan["binding"]["run_id"]}


def make_server(port, output, *, host="127.0.0.1", password_file=None, tailscale=False):
    host = private_ipv4(host)
    remote = not host.startswith("127.")
    if tailscale and not is_tailscale_address(host):
        raise ValueError("--tailscale requires binding to this machine's 100.64.0.0/10 Tailscale IPv4")
    if remote and not (password_file and tailscale):
        raise ValueError("A network console requires --password-file and --tailscale")
    expected_auth = None
    if password_file:
        password_path = Path(password_file)
        if password_path.stat().st_mode & 0o077:
            raise ValueError("Console password file must be private: chmod 600 FILE")
        password = password_path.read_text().strip()
        if len(password) < 24 or not password.isascii() or any(c.isspace() for c in password):
            raise ValueError("Console password must contain at least 24 ASCII characters without whitespace")
        expected_auth = "Basic " + base64.b64encode(f"bridge:{password}".encode()).decode()
    state = ConsoleState(output)
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
                "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def trusted(self, *, api=False):
            authority = f"{host}:{self.server.server_port}"
            if self.headers.get("Host") != authority:
                raise ValueError("Use the printed console URL")
            origin = self.headers.get("Origin")
            if origin is not None and origin != f"http://{authority}":
                raise ValueError("Cross-origin requests are not allowed")
            if api and not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), state.token):
                raise ValueError("Console session token is missing")

        def authenticated(self):
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

        def do_POST(self):
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
                    self.respond(200, {"plan": plan, "fingerprint": fingerprint(plan),
                                       "backend_bundle_directory": str(folder),
                                       "serve_command": f"cd {shlex.quote(str(folder))} && sh serve-sim.sh"})
                elif self.path == "/api/run":
                    with state.lock:
                        if state.review is None or fingerprint(state.review) != data["fingerprint"]:
                            raise ValueError("Reviewed plan changed; review and export again")
                    self.respond(202, state.launch(data["mode"], data["hardware"], data["fingerprint"]))
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
                        result = asyncio.run(
                            control(
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

    server = ThreadingHTTPServer((host, port), Handler)
    server.url = f"http://{host}:{server.server_port}"
    server.state = state
    return server


def serve(port=8088, output="bridge-runs", **kwargs):
    server = make_server(port, output, **kwargs)
    print(f"Bridge console: {server.url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
