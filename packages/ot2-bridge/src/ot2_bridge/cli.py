"""Runnable integration harness. Workflow order and stop policy live here, outside the bridge."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
from pathlib import Path

from .client_adapter import OT2ClientAdapter, position_observation
from .demo import DemoBackend, operations
from .mirror import Mirror
from .models import Status
from .predictive_runner import run_validated, validate_constraints, validate_dependencies
from .remote import RemoteSimulation, SimulationService
from .wire import dumps, fingerprint, operation_from_dict, plain


def load(path):
    """Read an explicitly selected JSON file."""
    return json.loads(Path(path).read_text())


def save_report(records, output: Path):
    """Produce an escaped, offline operation timeline beside the JSONL evidence."""
    rows = []
    for record in records:
        op = record["operation"]
        differences = record.get("hold_reason", "") + "; ".join(
            f"{c['field']}: {c['status']}" for c in record["comparisons"]
        )
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{html.escape(str(v))}</td>"
                for v in (
                    op["operation_id"],
                    op["action"],
                    record["real"]["outcome"]["status"] if record["real"] else "NOT DISPATCHED",
                    record["sim"]["outcome"]["status"] if record["sim"] else "NOT DISPATCHED",
                    differences,
                )
            )
            + "</tr>"
        )
    output.write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>OT-2 bridge run</title>"
        "<style>body{font:16px system-ui;margin:40px;color:#183042}table{border-collapse:collapse;width:100%}"
        "td,th{padding:12px;text-align:left;border-bottom:1px solid #ddd}pre{white-space:pre-wrap}</style>"
        "<h1>OT-2 bridge run</h1><p>Operation-boundary evidence. Sources remain separate. "
        "Demo sources are synthetic; missing real telemetry remains unavailable.</p>"
        "<table><tr><th>Operation</th><th>Action</th><th>Real side</th><th>Simulation</th><th>Comparison</th></tr>"
        + "".join(rows)
        + "</table><h2>Full evidence</h2><pre>"
        + html.escape(json.dumps(records, indent=2))
        + "</pre></html>"
    )


def validate_plan(plan):
    """Validate identities and every input before starting a sequence."""
    from .client_adapter import validate_client_operation
    from .comparison import compare
    from .models import Outcome

    binding = plan["binding"]
    if binding["schema"] != "ot2.session/1" or not plan["operations"]:
        raise ValueError("Expected a non-empty ot2.session/1 plan")
    seen = set()
    parsed = []
    for item in plan["operations"]:
        op = operation_from_dict(item)
        if (op.run_id, op.device_id) != (binding["run_id"], binding["device_id"]) or op.operation_id in seen:
            raise ValueError("Plan contains mismatched identities or duplicate operation IDs")
        seen.add(op.operation_id)
        validate_client_operation(op, binding["device_id"], plan.get("mounts", {"LEFT": 1}))
        parsed.append(op)
    compare(Outcome(Status.UNKNOWN), Outcome(Status.UNKNOWN), plan["tolerances"])
    return parsed


async def execute_plan(plan, remote, real, out: Path, *, sim_timeout=70, mode="shadow"):
    """Run a caller-owned sequence; stop on failures or differences, never retry."""
    out.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 -- tiny caller-owned local trace setup
    parsed = validate_plan(plan)
    if mode not in ("shadow", "validate-first"):
        raise ValueError("Unknown execution mode")
    if mode == "validate-first":
        validate_constraints(plan.get("validation_checks"))
        validate_dependencies(plan)
    for op in parsed:
        if real is not None:
            real.prepare(op)
    records, exit_code = [], 0
    # Exclusive creation prevents an accidental rerun from erasing evidence.
    with (out / "trace.jsonl").open("x") as trace:
        try:
            for operation in parsed:
                real.prepare(operation)  # Local capability check before reserving remote.
                staged = await remote.stage(operation)
                mirror = Mirror(staged, sim_timeout=sim_timeout, tolerances=plan["tolerances"])

                def report(result):
                    record = plain(result)
                    records.append(record)
                    trace.write(dumps(record) + "\n")
                    trace.flush()

                if mode == "validate-first":

                    def report_validation(record):
                        records.append(record)
                        trace.write(dumps(record) + "\n")
                        trace.flush()

                    result = await run_validated(
                        operation, staged, real, plan, timeout=sim_timeout, report=report_validation
                    )
                    if result is None:
                        print(f"{operation.operation_id}: {operation.action} | HELD; real NOT DISPATCHED")
                        exit_code = 5
                        break
                else:
                    result = await mirror.run(operation, real, report=report)
                print(
                    f"{operation.operation_id}: {operation.action} | real={result.real.outcome.status.value} "
                    f"sim={result.sim.outcome.status.value} | "
                    + ", ".join(f"{c.field}={c.status}" for c in result.comparisons)
                )
                if any(side.outcome.status is not Status.SUCCEEDED for side in (result.real, result.sim)):
                    exit_code = 2
                    break
                if any(c.status == "different" for c in result.comparisons):
                    exit_code = 3
                    break
                if plan.get("require_comparable", False) and any(c.status == "unavailable" for c in result.comparisons):
                    exit_code = 4
                    break
        finally:
            save_report(records, out / "report.html")
    return exit_code


def demo_plan():
    """Build a synthetic seven-operation transfer with explicit initial state."""
    return {
        "binding": {
            "schema": "ot2.session/1",
            "device_id": "ot2",
            "run_id": "demo-run",
            "mode": "synthetic-demo",
            "configuration_revision": "demo/1",
        },
        "operations": plain(operations()),
        "initial_sim": {"pipette.volume": 0.0, "pipette.tip_attached": False},
        "tolerances": {"pipette.volume": 0.01, "pipette.tip_attached": 0},
        "require_comparable": True,
        "validation_checks": [
            {"name": "pipette_capacity", "field": "pipette.volume", "unit": "uL", "frame": "", "min": 0, "max": 300}
        ],
        "validation_validity_s": 5,
    }


def check_initial(observation, expected):
    """Validate explicitly expected initial facts without overwriting either backend."""
    for name, value in expected.items():
        fact = observation["facts"].get(name)
        if fact is None or fact["evidence"] == "unknown" or fact["value"] != value:
            raise ValueError(f"Initial simulation state mismatch: {name}")


async def run_demo(args):
    """Exercise actual loopback communication with two explicitly synthetic peers."""
    plan = demo_plan()
    sim = DemoBackend(source="demo-sim", drift=args.drift, fail_action=args.fail_action)
    service = SimulationService(sim, binding=plan["binding"], initial_observation=sim.observe())
    server = await service.listen(0)
    async with server:
        remote = RemoteSimulation(server.sockets[0].getsockname()[1])
        initial = await remote.connect(plan["binding"])
        check_initial(initial, plan["initial_sim"])
        real = DemoBackend(source="demo-real")
        return await execute_plan(plan, remote, real, Path(args.out), mode=getattr(args, "mode", "shadow"))


async def run_connected(args):
    """Connect a remote simulation with either an explicit demo peer or the upstream client."""
    plan = load(args.plan)
    validate_plan(plan)
    remote = RemoteSimulation(args.port)
    check_initial(await remote.connect(plan["binding"]), plan["initial_sim"])
    if args.demo_real:
        return await execute_plan(
            plan, remote, DemoBackend(plan["binding"]["device_id"], source="demo-real"), Path(args.out), mode=args.mode
        )
    if not args.client_config or not args.hardware:
        raise ValueError("Select --demo-real or explicitly supply --hardware --client-config")
    if plan["binding"].get("mode") != "hardware" or not plan["binding"].get("qualified_mapping_revision"):
        raise ValueError("Hardware requires a reviewed hardware session binding, not the synthetic example")
    config_data = load(args.client_config)
    if fingerprint(config_data) != plan["binding"].get("client_config_sha256"):
        raise ValueError("Client configuration does not match the reviewed session")
    from ot2_sila_client.client import Ot2SilaClient
    from ot2_sila_client.config import LiveCalibrationProvider, Ot2Config
    from ot2_sila_client.transport.sila_transport import SilaTransport

    config = Ot2Config.model_validate(config_data)
    motion = await SilaTransport.connect(config.host, config.port, tls=config.tls, timeout_ms=config.connect_timeout_ms)
    try:
        if await motion.is_simulating():
            raise RuntimeError("Hardware session refuses a simulated connector")
        if config.require_simulation:
            raise ValueError("Hardware session conflicts with require_simulation=True")
        pipettes = config.build_pipettes()
        client = Ot2SilaClient(
            motion,
            config.build_deck_layout(),
            pipettes,
            calibration=LiveCalibrationProvider(motion),
            default_speed_mm_s=config.default_speed_mm_s,
            travel_height_mm=config.travel_height_mm,
        )

        async def observe(operation):
            return position_observation(
                await motion.get_position(), source="ot2-firmware", revision=operation.operation_id
            )

        real = OT2ClientAdapter(plan["binding"]["device_id"], client, observe, mounts=plan["mounts"])
        # Ensure declared channel counts agree with the loaded pipette models.
        for mount, channels in plan["mounts"].items():
            if pipettes[mount.lower()].channels != channels:
                raise ValueError("Declared channels differ from the actual configured pipette model")
        return await execute_plan(plan, remote, real, Path(args.out), mode=getattr(args, "mode", "shadow"))
    finally:
        await motion.close()


def main():
    """Expose dependency-free demo, simulation connection and evidence inspection."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run synthetic peers through the actual network transport")
    demo.add_argument("--out", default="bridge-demo")
    demo.add_argument("--drift", type=float, default=0)
    demo.add_argument("--fail-action", default="", choices=["", "home", "aspirate", "dispense"])
    demo.add_argument("--mode", choices=["shadow", "validate-first"], default="shadow")
    run = sub.add_parser("run", help="Run an explicit plan against a waiting Matterix runtime")
    run.add_argument("--plan", required=True)
    run.add_argument("--mode", choices=["shadow", "validate-first"], default="shadow")
    run.add_argument("--port", type=int, default=8765)
    run.add_argument("--out", required=True)
    run.add_argument("--demo-real", action="store_true")
    run.add_argument("--hardware", action="store_true")
    run.add_argument("--client-config")
    inspect = sub.add_parser("inspect", help="Validate a plan without connecting to hardware")
    inspect.add_argument("--plan", required=True)
    args = parser.parse_args()
    if args.command == "inspect":
        plan = load(args.plan)
        validate_plan(plan)
        print(f"{len(plan['operations'])} valid operation envelopes; binding {fingerprint(plan['binding'])}")
        print("No hardware or native Matterix execution performed.")
        return
    if args.command == "run" and args.demo_real and (args.hardware or args.client_config):
        parser.error("--demo-real cannot be combined with hardware configuration")
    try:
        code = asyncio.run(run_demo(args) if args.command == "demo" else run_connected(args))
    except (ValueError, RuntimeError, OSError, asyncio.TimeoutError) as error:
        parser.exit(2, f"{type(error).__name__}: {error}\n")
    raise SystemExit(code)
