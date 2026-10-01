import asyncio
import json
import struct
import subprocess
import sys

import pytest

from ot2_bridge.instruments import make_plan, profile_template
from ot2_bridge.managed_matterix import ManagedMatterix, NativeViewer


def test_native_viewer_encodes_only_actual_rgb_and_reports_failure(tmp_path):
    import numpy as np

    class Env:
        pixels = np.full((2, 3, 3), 120, dtype=np.uint8)

        def render(self):
            return self.pixels

    env = Env()
    binding = {"run_id": "fixture"}
    viewer = NativeViewer(env, tmp_path, binding, interval=0)
    viewer.capture()
    png = (tmp_path / "frame.png").read_bytes()
    assert png.startswith(b"\x89PNG")
    assert struct.unpack("!II", png[16:24]) == (3, 2)
    status = json.loads((tmp_path / "frame.json").read_text())
    assert status["status"] == "available" and status["binding"] == binding
    env.pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    viewer.capture()
    assert json.loads((tmp_path / "frame.json").read_text())["status"] == "unavailable"
    env.pixels = None
    viewer.capture()
    assert json.loads((tmp_path / "frame.json").read_text())["status"] == "unavailable"
    assert (tmp_path / "frame.png").read_bytes() == png  # Old image is not reported as live.


@pytest.mark.parametrize("behavior", ["ready", "wrong-binding", "exit", "timeout", "stop"])
def test_native_supervisor_readiness_binding_exit_and_cleanup(tmp_path, monkeypatch, behavior):
    # A real child process exercises lifecycle without pretending to be Isaac/GPU.
    plan = make_plan(profile_template("ot2"), "cpu-supervisor-fixture")
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    child = tmp_path / "child.py"
    child.write_text("""import json, pathlib, sys, time
args = sys.argv
mode = args[1]
if mode == 'exit':
    print('explicit CPU child startup failure', flush=True)
    sys.exit(7)
if mode in ('ready', 'wrong-binding'):
    plan = json.loads(pathlib.Path(args[args.index('--plan') + 1]).read_text())
    binding = plan['binding'] if mode == 'ready' else {'run_id': 'wrong'}
    pathlib.Path(args[args.index('--ready-file') + 1]).write_text(json.dumps({
        'binding': binding, 'port': int(args[args.index('--port') + 1])}))
time.sleep(60)
""")
    real_popen = subprocess.Popen
    launched = []

    def start(args, **kwargs):
        assert "--enable_cameras" in args and "--headless" in args
        assert args[args.index("--viewer-dir") + 1] == str(tmp_path / "viewer")
        process = real_popen([sys.executable, str(child), behavior, *args[3:]], **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    manager = ManagedMatterix(
        {"python": sys.executable, "matterix_root": str(tmp_path), "assets_root": str(tmp_path), "startup_timeout": 0.7}
    )
    events = []

    async def run():
        if behavior == "ready":
            port = await manager.start(plan, tmp_path, events.append, lambda: False)
            assert port > 0 and manager.snapshot()["status"] == "ready"
            await manager.close()
            await manager.close()
            assert manager.snapshot()["status"] == "stopped"
        else:
            with pytest.raises((ValueError, RuntimeError, TimeoutError)):
                await manager.start(plan, tmp_path, events.append, lambda: behavior == "stop")
            assert manager.snapshot()["status"] == "failed"
        assert launched and launched[0].poll() is not None

    asyncio.run(run())


def test_viewer_frame_rejects_wrong_run_and_unavailable_frame(tmp_path):
    manager = ManagedMatterix({})
    manager.directory = tmp_path
    manager.binding = {"run_id": "correct"}
    (tmp_path / "viewer").mkdir()
    (tmp_path / "viewer/frame.json").write_text(json.dumps({"status": "unavailable", "binding": manager.binding}))
    with pytest.raises(ValueError, match="different run"):
        manager.frame("other")
    with pytest.raises(ValueError, match="unavailable"):
        manager.frame("correct")
