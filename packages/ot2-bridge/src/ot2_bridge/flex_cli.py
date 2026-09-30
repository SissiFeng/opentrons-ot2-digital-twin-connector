"""OT-2 / Flex run console and field-test command line."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import uuid
from contextlib import nullcontext
from .flex_session import device_session

from .flex import FlexAdapter
from .instruments import make_plan, profile_template, validate_plan, validate_profile, is_ot2
from .ot2_console import OT2HomeAdapter
from .flex_bundle import bundle_bytes
from .flex_runner import run_plan
from .remote import RemoteSimulation


def read_profile(path):
    value = json.loads(Path(path).read_text())
    if "binding" in value:
        return validate_plan(value)["profile"]
    return validate_profile(value)


async def connected(profile, *, progress=None, stop_only=False):
    from .flex_sila import FlexSiLATransport

    from .ot2_sila import OT2SiLATransport

    real = profile["real"]
    cls = OT2SiLATransport if is_ot2(profile) else FlexSiLATransport
    features = ("MotionControlFeature",) if is_ot2(profile) else ("MotionController",)
    try:
        return await cls(real["host"], real["port"], progress=progress).connect(
            feature_names=features if stop_only else None
        )
    except ImportError as error:
        raise RuntimeError(
            "Install the optional SiLA client: pip install 'ot2-bridge[sila]' from the supplied wheel"
        ) from error


async def _run_connected(plan, *, mode, hardware, sim_port=8765, emit=None, stop_requested=lambda: False):
    """Connect selected backends; all execution policy stays in the caller layer."""
    validate_plan(plan)
    transport = None
    try:
        if mode != "sim-only":
            validate_profile(plan["profile"], hardware=True)
            transport = await connected(plan["profile"])
            cls = OT2HomeAdapter if is_ot2(plan["profile"]) else FlexAdapter
            real = cls(plan["profile"], transport, hardware=hardware, stop_requested=stop_requested)
        else:
            real = None
        simulation = RemoteSimulation(sim_port, timeout=90) if mode != "real-only" else None
        return await run_plan(
            plan, mode=mode, real=real, simulation=simulation, emit=emit, stop_requested=stop_requested
        )
    finally:
        if transport:
            await transport.close()


async def run_connected(plan, *, mode, hardware, sim_port=8765, emit=None, stop_requested=lambda: False):
    validate_plan(plan)
    guard = device_session(plan["profile"], run_id=plan["binding"]["run_id"]) if mode != "sim-only" else nullcontext()
    with guard:
        return await _run_connected(
            plan, mode=mode, hardware=hardware, sim_port=sim_port, emit=emit, stop_requested=stop_requested
        )


async def control(profile, action, *, hardware=None, stop_requested=lambda: False):
    """Inspect read-only state, or execute explicitly requested home/stop."""
    transport = await connected(profile, stop_only=action == "stop")
    try:
        if action == "inspect":
            return await transport.describe()
        if action == "home":
            validate_profile(profile, hardware=True)
            with device_session(profile):
                info = await transport.describe()
                real = profile["real"]
                if info["server_uuid"] != real["server_uuid"] or info["is_simulating"] is not (not hardware):
                    raise ValueError("Server identity or hardware mode mismatch")
                if stop_requested():
                    raise RuntimeError("Stop requested before homing dispatch")
                if is_ot2(profile):
                    return await transport.command("MotionControlFeature", "Home", Axes="XYZABC")
                if (info["pipette"]["id"], info["pipette"]["model"], info["pipette"]["channels"]) != (
                    real["pipette_id"],
                    real["pipette_model"],
                    1,
                ) or info["tip_present"]:
                    raise ValueError("Home requires the bound single-channel pipette and no attached tip")
                return await transport.command("MotionController", "Home")
        if action == "stop":
            identity = await transport.property("SiLAService", "ServerUUID")
            if not profile["real"]["server_uuid"] or identity != profile["real"]["server_uuid"]:
                raise ValueError("Stop requires the bound server UUID")
            if is_ot2(profile):
                response = await transport.command("MotionControlFeature", "EmergencyStop")
                return {
                    "command_response": response,
                    "homed": await transport.property("MotionControlFeature", "HomedFlags"),
                    "physical_stop_confirmed": False,
                }
            response = await transport.command("MotionController", "EmergencyStop")
            return {
                "command_response": response,
                "machine_status": await transport.property("MotionController", "MachineStatus"),
            }
        raise ValueError("Unknown control action")
    finally:
        await transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    profile = sub.add_parser("profile", help="Write an unqualified profile for site configuration")
    profile.add_argument("--instrument", choices=["ot2", "flex"], default="ot2")
    profile.add_argument("--output", default="instrument-profile.json")
    export = sub.add_parser("export", help="Export an exact one-run plan and portable Ubuntu bundle")
    export.add_argument("--profile", required=True)
    export.add_argument("--output", required=True)
    for name in ("inspect", "home", "stop"):
        command = sub.add_parser(name)
        command.add_argument("--profile", required=True)
        if name == "home":
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--hardware", action="store_true")
            mode.add_argument("--connector-simulator", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--plan", required=True)
    run.add_argument("--mode", required=True, choices=["sim-only", "real-only", "shadow"])
    run.add_argument("--sim-port", type=int, default=8765)
    run.add_argument("--output", required=True)
    target = run.add_mutually_exclusive_group()
    target.add_argument("--hardware", action="store_true")
    target.add_argument("--connector-simulator", action="store_true")
    console = sub.add_parser("console")
    console.add_argument("--port", type=int, default=8088)
    console.add_argument("--output", default="bridge-runs")
    console.add_argument("--listen", default="127.0.0.1", help="Explicit private IPv4 for the Ubuntu host")
    console.add_argument("--password-file", help="Private file containing the browser login password (user: bridge)")
    console.add_argument("--tailscale", action="store_true", help="Use the existing encrypted Tailscale network")
    args = parser.parse_args()
    try:
        if args.command == "profile":
            with Path(args.output).open("x") as file:
                json.dump(profile_template(args.instrument), file, indent=2)
            print(args.output)
        elif args.command == "export":
            plan = make_plan(read_profile(args.profile), str(uuid.uuid4()))
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=False)
            (out / "plan.json").write_text(json.dumps(plan, indent=2))
            (out / ("ot2-test.zip" if is_ot2(plan["profile"]) else "flex-test.zip")).write_bytes(bundle_bytes(plan))
            print(out.resolve())
        elif args.command in ("inspect", "home", "stop"):
            print(
                json.dumps(
                    asyncio.run(
                        control(read_profile(args.profile), args.command, hardware=getattr(args, "hardware", None))
                    ),
                    indent=2,
                )
            )
        elif args.command == "run":
            if args.mode != "sim-only" and not (args.hardware or args.connector_simulator):
                raise ValueError("Explicit --hardware or --connector-simulator is required")
            plan = validate_plan(json.loads(Path(args.plan).read_text()))
            out = Path(args.output)
            out.mkdir(parents=True, exist_ok=False)
            (out / "plan.json").write_text(json.dumps(plan, indent=2))
            try:
                report = asyncio.run(
                    run_connected(
                        plan,
                        mode=args.mode,
                        hardware=args.hardware,
                        sim_port=args.sim_port,
                        emit=lambda event: print(json.dumps(event), flush=True),
                    )
                )
            except (Exception, KeyboardInterrupt) as error:
                report = {
                    "binding": plan["binding"],
                    "status": "held",
                    "error": f"{type(error).__name__}: {error}. Physical stop is not confirmed.",
                }
            (out / "report.json").write_text(json.dumps(report, indent=2))
            if report["status"] != "completed":
                raise SystemExit(2)
        else:
            from .flex_console import serve

            serve(args.port, args.output, host=args.listen, password_file=args.password_file,
                  tailscale=args.tailscale)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
