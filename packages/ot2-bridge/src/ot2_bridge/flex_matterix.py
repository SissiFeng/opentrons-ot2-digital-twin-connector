"""One-shot or remote Matterix Flex launcher; invoke in the Ubuntu Isaac environment."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

from .instruments import native_adapter, validate_plan, is_ot2
from .wire import operation_from_dict, plain


def check_checkout(path, revision, paths=()):
    """Fail before importing Isaac if the reviewed runtime source has changed."""
    root = Path(path).resolve(strict=True)
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise ValueError(f"{root}: expected {revision}, got {actual}")
    changes = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal", "--", *paths],
        text=True,
    )
    if changes.strip():
        raise ValueError(f"{root}: reviewed runtime files have local changes; use a clean pinned checkout")
    return root


def main():
    """Initialize once, reuse the exact PR59 native sequence, and never auto-repeat."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--matterix-root", required=True)
    parser.add_argument("--assets-root", required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", default="flex-sim-result.json")
    # Parse source arguments before importing Isaac (and before initializing its process).
    known, _ = parser.parse_known_args()
    plan = validate_plan(json.loads(Path(known.plan).read_text()))
    profile = plan["profile"]
    ot2 = is_ot2(profile)
    if not known.serve and Path(known.output).exists():
        raise ValueError("Output already exists; choose a new report path before starting simulation")
    root = check_checkout(known.matterix_root, profile["matterix_revision"], ("source", "scripts"))
    assets = check_checkout(known.assets_root, profile["assets_revision"])
    os.environ["MATTERIX_PATH"] = str(root)
    os.environ["MATTERIX_ASSETS_DATA_DIR"] = str(assets)
    # Use the pinned worktree without changing the user's editable installations.
    # All four packages are checked again after importing below.
    for name in ("matterix", "matterix_assets", "matterix_sm", "matterix_tasks"):
        sys.path.insert(0, str(root / "source" / name))
    if ot2:
        entry = assets / "robots/ot2/OT2_dual_multi.usda"
        if not entry.is_file() or entry.read_bytes().startswith(b"version https://git-lfs"):
            raise ValueError("OT-2 dual-mount asset is missing; run git lfs pull in the pinned assets checkout")

    from isaaclab.app import AppLauncher

    AppLauncher.add_app_launcher_args(parser)
    args = parser.parse_args()
    launcher = AppLauncher(args)
    app = launcher.app

    import gymnasium as gym
    import matterix_tasks
    import matterix_sm
    import matterix_assets
    import matterix
    import torch
    from isaaclab_tasks.utils.parse_cfg import parse_env_cfg
    from matterix_sm import StateMachine
    from .matterix_runtime import MatterixRuntime
    from .remote import SimulationService

    async def run():
        for module in (matterix_tasks, matterix_sm, matterix_assets, matterix):
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise ValueError(f"{module.__name__} imported from a different checkout")
        cfg = parse_env_cfg(profile["task"], device=args.device, num_envs=1)
        cfg.terminations.time_out = None
        actions = cfg.workflows[profile["workflow"]]
        env = gym.make(profile["task"], cfg=cfg).unwrapped
        try:
            obs, _ = env.reset()
            runtime = MatterixRuntime(
                env,
                StateMachine(num_envs=1, dt=env.step_dt, device=env.device),
                obs,
                {"asset_name": "ot2" if ot2 else "flex"},
                timeout=80,
                is_running=app.is_running,
            )
            adapter = native_adapter(profile, actions, runtime)
            for value in plan["operations"]:
                adapter.prepare(operation_from_dict(value))
            if args.serve:
                service = SimulationService(
                    adapter, binding=plan["binding"], initial_observation=runtime.observe(), timeout=85
                )
                server = await service.listen(args.port)
                print(
                    f"{'OT-2' if ot2 else 'FLEX'} BRIDGE READY 127.0.0.1:{args.port} run={plan['binding']['run_id']}",
                    flush=True,
                )
                async with server:
                    while app.is_running():
                        if not runtime.busy:
                            app.update()
                        await asyncio.sleep(0.01)
            else:
                result = {"binding": plan["binding"], "mode": "matterix-only", "status": "completed", "steps": []}
                try:
                    for item in plan["operations"]:
                        operation = operation_from_dict(item)
                        outcome = await adapter.prepare(operation)()
                        result["steps"].append({"operation": item, "sim": {"outcome": plain(outcome)}})
                        expected = operation.action == "pick_up_tip"
                        fact = outcome.observation.facts.get("pipette.tip_attached") if outcome.observation else None
                        if outcome.status.value != "succeeded" or (
                            not ot2 and (fact is None or fact.value is not expected)
                        ):
                            result["status"] = "held"
                            break
                except (Exception, asyncio.CancelledError) as error:
                    result["status"] = "held"
                    result["error"] = f"{type(error).__name__}: {error}"
                with Path(args.output).open("x", encoding="utf-8") as output:
                    json.dump(result, output, indent=2)
                print(json.dumps({"status": result["status"], "report": args.output}), flush=True)
                if result["status"] != "completed":
                    raise RuntimeError("Matterix single-run test did not pass; inspect report")
        finally:
            env.close()

    try:
        with torch.inference_mode():
            asyncio.run(run())
    finally:
        app.close()


if __name__ == "__main__":
    main()
