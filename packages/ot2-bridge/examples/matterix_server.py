"""Launch inside the qualified Matterix/Isaac environment, never on the robot.

./matterix.sh -p /path/to/matterix_server.py --plan ... --profile ... --headless
"""

# ruff: noqa: E402 -- Isaac AppLauncher must precede native imports
import argparse
import asyncio
import json
from pathlib import Path

from isaaclab.app import AppLauncher

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--plan", required=True)
parser.add_argument("--profile", required=True)
parser.add_argument("--port", type=int, default=8765)
AppLauncher.add_app_launcher_args(parser)
args = parser.parse_args()
launcher = AppLauncher(args)
app = launcher.app

# Isaac must start before importing environments or native configuration classes.
import gymnasium as gym
import matterix_tasks  # noqa: F401
import torch
from isaaclab_tasks.utils.parse_cfg import parse_env_cfg
from matterix_sm import StateMachine

from ot2_bridge.cli import check_initial, validate_plan
from ot2_bridge.matterix_runtime import MatterixRuntime, NativeOT2Adapter
from ot2_bridge.remote import SimulationService
from ot2_bridge.wire import fingerprint, operation_from_dict, plain


async def serve():
    """Create the environment once; keep it alive across all bridge operations."""
    plan = json.loads(Path(args.plan).read_text())  # noqa: ASYNC240 -- startup config only
    profile = json.loads(Path(args.profile).read_text())  # noqa: ASYNC240 -- startup config only
    if fingerprint(profile) != plan["binding"]["profile_sha256"]:
        raise ValueError("Profile differs from the reviewed session binding")
    if profile["device_id"] != plan["binding"]["device_id"]:
        raise ValueError("Profile device mismatch")
    validate_plan(plan)
    cfg = parse_env_cfg(profile["task"], device=args.device, num_envs=1)
    # A bridge session spans many sequences; disable only the episode clock.
    # Genuine task terminations still invalidate the session in MatterixRuntime.
    cfg.terminations.time_out = None
    # This is an explicit initial configuration, not a per-operation reseed.
    env = gym.make(profile["task"], cfg=cfg).unwrapped
    try:
        for recipe in profile["recipes"]:
            for step in recipe["steps"]:
                if step["type"] == "tip" and step["asset_name"] not in env.scene.keys():  # noqa: SIM118 -- Isaac scene API
                    raise ValueError(f"Missing configured rigid tip asset: {step['asset_name']}")
        obs, _ = env.reset()
        sm = StateMachine(num_envs=1, dt=env.step_dt, device=env.device)
        runtime = MatterixRuntime(env, sm, obs, profile, is_running=app.is_running)
        adapter = NativeOT2Adapter(profile, runtime)
        for item in plan["operations"]:
            adapter.prepare(operation_from_dict(item))
        initial = runtime.observe()
        check_initial(plain(initial), plan["initial_sim"])
        service = SimulationService(adapter, binding=plan["binding"], initial_observation=initial)
        server = await service.listen(args.port)
        print(f"Matterix bridge ready on 127.0.0.1:{args.port}; persistent environment; no hardware access", flush=True)
        async with server:
            while app.is_running():
                # Keep the window responsive while waiting. Physics is advanced
                # by runtime execution, so idle time cannot trigger env auto-reset.
                if not runtime.busy:
                    app.update()
                await asyncio.sleep(0.01)
    finally:
        env.close()


try:
    with torch.inference_mode():
        asyncio.run(serve())
finally:
    app.close()
