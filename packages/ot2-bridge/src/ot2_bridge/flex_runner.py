"""Application-level execution policy outside the thin bridge adapters."""

from __future__ import annotations

import asyncio
import time

from .comparison import compare
from .instruments import validate_plan, is_ot2
from .mirror import Mirror
from .models import Status
from .wire import operation_from_dict, plain


async def run_plan(plan, *, mode, real=None, simulation=None, emit=None, stop_requested=lambda: False):
    """Run once, stop dispatch after uncertainty, never retry an operation.

    The caller supplies adapters; no SiLA or particular orchestrator is required.
    Concurrent mode uses semantic step barriers, not wall-clock synchronization.
    """
    validate_plan(plan)
    if mode not in ("sim-only", "real-only", "shadow"):
        raise ValueError("Supported modes: sim-only, real-only, shadow")
    if mode != "sim-only" and real is None:
        raise ValueError("Real adapter required")
    if mode != "real-only" and simulation is None:
        raise ValueError("Matterix connection required")
    ot2 = is_ot2(plan["profile"])
    tolerances = {} if ot2 else {"pipette.tip_attached": 0}
    report = {"binding": plan["binding"], "mode": mode, "status": "running", "steps": [], "events": []}

    report["comparison_scope"] = "completion-only; native frames kept separate" if ot2 else "tip-presence"

    def publish(event):
        item = {"elapsed_s": time.monotonic() - started, **plain(event)}
        report["events"].append(item)
        if emit:
            emit(item)

    class ReportingAdapter:
        def __init__(self, adapter, side):
            self.adapter, self.side = adapter, side

        def prepare(self, op):
            call = self.adapter.prepare(op)

            async def execute():
                if stop_requested():
                    raise RuntimeError("Stop requested before backend dispatch")
                outcome = await call()
                publish(
                    {"type": "backend-finished", "side": self.side, "operation_id": op.operation_id, "outcome": outcome}
                )
                return outcome

            return execute

    started = time.monotonic()
    try:
        operations = [operation_from_dict(op) for op in plan["operations"]]
        if real:
            for op in operations:
                real.prepare(op)  # Complete all static validation before either side moves.
            initial_real = await real.snapshot("initial")
            report["initial_real"] = plain(initial_real)
            if not ot2 and initial_real.facts["pipette.tip_attached"].value is not False:
                raise ValueError("Start with no tip attached; reconcile the real pipette")
        if simulation:
            initial_sim = await simulation.connect(plan["binding"])
            report["initial_sim"] = plain(initial_sim)
            if not ot2 and simulation.last_observation.facts.get("pipette.tip_attached") is None:
                raise ValueError("Matterix tip observation is missing")
            if not ot2 and simulation.last_observation.facts["pipette.tip_attached"].value is not False:
                raise ValueError("Matterix must start with no tip attached")
        for op in operations:
            if stop_requested():
                raise RuntimeError("Operator requested stop; no next step dispatched")
            publish({"type": "step-started", "operation_id": op.operation_id, "action": op.action})
            staged = await simulation.stage(op) if simulation else None
            if stop_requested():
                if staged:
                    await staged.discard()
                raise RuntimeError("Stop requested during staging; reservation discarded")
            if mode == "shadow":
                result = await Mirror(ReportingAdapter(staged, "sim"), sim_timeout=90, tolerances=tolerances).run(
                    op, ReportingAdapter(real, "real"), report=lambda pair: report["steps"].append(plain(pair))
                )
                step = plain(result)
                statuses = (result.real.outcome.status, result.sim.outcome.status)
            else:
                adapter = real if mode == "real-only" else staged
                outcome = await ReportingAdapter(adapter, "real" if mode == "real-only" else "sim").prepare(op)()
                step = {"operation": plain(op), mode.split("-")[0]: {"outcome": plain(outcome)}}
                statuses = (outcome.status,)
            if mode != "shadow":
                report["steps"].append(step)
            publish({"type": "step-finished", "step": step})
            if any(status is not Status.SUCCEEDED for status in statuses):
                report["status"] = "held"
                break
            if mode == "shadow" and not ot2:
                divergence = compare(result.real.outcome, result.sim.outcome, {"pipette.tip_attached": 0})
                tip = next((c for c in divergence if c.field == "pipette.tip_attached"), None)
                if tip is None or tip.status != "match":
                    report["status"] = "held"
                    report["error"] = "Tip observations differ or are unavailable; no next step dispatched"
                    break
        else:
            report["status"] = "completed"
    except (Exception, asyncio.CancelledError) as error:
        report["status"] = "held"
        report["error"] = f"{type(error).__name__}: {error}. No automatic retry."
        if mode != "sim-only":
            report["error"] += " Physical stop is not confirmed."
    finally:
        publish({"type": "run-finished", "status": report["status"], "error": report.get("error", "")})
    return report
