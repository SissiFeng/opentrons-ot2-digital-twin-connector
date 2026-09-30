"""Application process supervision and native-frame artifacts, outside bridge mapping."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import time
import zlib


PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False))
    temporary.replace(path)


class NativeViewer:
    """Capture actual Matterix env.render() RGB; never synthesize a substitute scene."""

    def __init__(self, env, directory, binding, interval=0.5):
        self.env, self.directory, self.binding = env, Path(directory), binding
        self.directory.mkdir(parents=True, exist_ok=True)
        self.interval, self.last = interval, 0
        self.sequence = 0

    def capture(self):
        if time.monotonic() - self.last < self.interval:
            return
        self.last = time.monotonic()
        metadata = {
            "binding": self.binding,
            "source": "matterix-native-rgb",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "sequence": self.sequence,
        }
        try:
            rgb = self.env.render()
            if rgb is None or rgb.ndim != 3 or rgb.shape[2] != 3 or str(rgb.dtype) != "uint8":
                raise ValueError("Native renderer did not return an RGB uint8 frame")
            if not rgb.any():
                raise ValueError("Native renderer is warming up; no visible scene received yet")
            height, width, _ = rgb.shape
            if not (0 < width <= 4096 and 0 < height <= 4096):
                raise ValueError("Native frame size is outside viewer bounds")
            pixels = rgb.tobytes()
            stride = width * 3
            rows = b"".join(b"\x00" + pixels[i : i + stride] for i in range(0, len(pixels), stride))

            def chunk(kind, data):
                return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows, 1))
                + chunk(b"IEND", b"")
            )
            temporary = self.directory / "frame.tmp"
            temporary.write_bytes(png)
            temporary.replace(self.directory / "frame.png")
            self.sequence += 1
            metadata.update(status="available", sequence=self.sequence, width=width, height=height)
        except Exception as error:
            metadata.update(status="unavailable", error=f"{type(error).__name__}: {error}")
        atomic_json(self.directory / "frame.json", metadata)


class ManagedMatterix:
    def __init__(self, config, runtime_by_device=None):
        self.config = config
        self.runtime_by_device = runtime_by_device or {}
        self.process = None
        self.directory = None
        self.binding = None
        self.state = "not-started"
        self.error = ""
        self.close_lock = asyncio.Lock()

    def snapshot(self):
        value = {"status": self.state, "error": self.error, "binding": self.binding, "viewer": None}
        if self.process and self.process.poll() is not None and self.state in ("starting", "ready"):
            value.update(status="exited", error=f"Matterix exited ({self.process.returncode})")
        if self.directory:
            with suppress(OSError, ValueError):
                value["viewer"] = json.loads((self.directory / "viewer/frame.json").read_text())
            try:
                with (self.directory / "matterix.log").open("rb") as file:
                    file.seek(0, 2)
                    file.seek(max(0, file.tell() - 6000))
                    value["log_tail"] = file.read().decode(errors="replace")
            except OSError:
                pass
        return value

    def frame(self, run_id):
        directory, binding = self.directory, self.binding
        if not binding or run_id != binding["run_id"] or directory is None:
            raise ValueError("Viewer belongs to a different run")
        metadata = json.loads((directory / "viewer/frame.json").read_text())
        if metadata.get("status") != "available" or metadata.get("binding") != binding:
            raise ValueError("Native frame is unavailable")
        return (directory / "viewer/frame.png").read_bytes()

    async def start(self, plan, directory, emit, stopped):
        await self.close()
        config = self.runtime_by_device.get(plan["profile"]["device_id"], self.config)
        if not config:
            raise ValueError("Native Matterix runtime is not configured. Run the one-time Ubuntu setup.")
        self.directory, self.binding = Path(directory), plan["binding"]
        self.state, self.error = "starting", ""
        ready = self.directory / "native-ready.json"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        python = Path(config["python"]).absolute()  # noqa: ASYNC240 - local setup path only
        if not python.is_file():
            raise ValueError("Configured Isaac Python executable is missing")
        for name in ("matterix_root", "assets_root"):
            if not Path(config[name]).is_dir():  # noqa: ASYNC240 - local setup path only
                raise ValueError(f"Configured {name} is missing")
        args = [
            str(python),
            "-m",
            "ot2_bridge.flex_matterix",
            "--plan",
            str(self.directory / "plan.json"),
            "--matterix-root",
            config["matterix_root"],
            "--assets-root",
            config["assets_root"],
            "--serve",
            "--port",
            str(port),
            "--ready-file",
            str(ready),
            "--viewer-dir",
            str(self.directory / "viewer"),
            "--enable_cameras",
        ]
        if config.get("headless", True):
            args.append("--headless")
        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        env.update(config.get("env", {}))
        env["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        with (self.directory / "matterix.log").open("wb") as log:
            self.process = subprocess.Popen(  # noqa: ASYNC220 - bounded spawn; wait is async
                args,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=config["matterix_root"],
                start_new_session=True,
            )
        emit(
            {
                "type": "simulation-starting",
                "message": "Starting native Matterix; building the scene may take several minutes",
            }
        )
        deadline = time.monotonic() + config.get("startup_timeout", 600)
        try:
            while time.monotonic() < deadline:
                if stopped():
                    raise RuntimeError("Stop requested during Matterix startup; no next operation dispatched")
                if self.process.poll() is not None:
                    raise RuntimeError(f"Matterix exited ({self.process.returncode}); open the native runtime log")
                if ready.is_file():
                    value = json.loads(ready.read_text())
                    if value != {"binding": self.binding, "port": port}:
                        raise ValueError("Native readiness binding differs from the reviewed plan")
                    self.state = "ready"
                    emit({"type": "simulation-ready", "message": "Native Matterix is ready"})
                    return port
                await asyncio.sleep(0.2)
            raise TimeoutError("Matterix startup timed out; inspect the native runtime log")
        except BaseException as error:
            self.error = f"{type(error).__name__}: {error}"
            await self.close()
            self.state = "failed"
            raise

    async def close(self):
        async with self.close_lock:
            process = self.process
            if process and process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), 5)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await asyncio.to_thread(process.wait)
            self.process = None
            if self.state in ("starting", "ready"):
                self.state = "stopped"
