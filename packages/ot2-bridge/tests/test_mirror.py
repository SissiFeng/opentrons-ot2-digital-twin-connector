"""Exercise concurrency and failure semantics without either device stack."""

import asyncio
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from ot2_bridge import (
    CallableAdapter,
    Evidence,
    Execution,
    Fact,
    MatterixAdapter,
    Mirror,
    Observation,
    Operation,
    Outcome,
    Status,
    compare,
)


def operation(**changes):
    return dataclasses.replace(Operation("run-1", "step-1", "ot2", "aspirate", {"volume_ul": 10}), **changes)


def outcome(value=10, evidence=Evidence.TRACKED, unit="uL", frame=""):
    return Outcome(Status.SUCCEEDED, Observation("test", {"volume": Fact(value, evidence, unit, frame)}))


def adapter(call):
    return CallableAdapter(lambda op: call)


@pytest.mark.asyncio
async def test_real_and_sim_start_concurrently_and_compare_actual_completion():
    real_started, sim_started = asyncio.Event(), asyncio.Event()
    calls = []

    async def real():
        calls.append("real")
        real_started.set()
        await sim_started.wait()
        return outcome(10)

    async def sim():
        calls.append("sim")
        sim_started.set()
        await real_started.wait()
        return outcome(11, Evidence.SIMULATED)

    result = await asyncio.wait_for(
        Mirror(adapter(sim), sim_timeout=1, tolerances={"volume": 0.5}).run(operation(), adapter(real)), 2
    )
    assert sorted(calls) == ["real", "sim"]
    assert result.comparisons[0].status == "different"
    assert result.real.started <= result.sim.finished
    assert result.sim.started <= result.real.finished
    assert result.comparisons[0].real.evidence is Evidence.TRACKED


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["sim", "real"])
async def test_exception_never_cancels_peer_or_retries(broken):
    calls = []

    async def fail():
        calls.append("fail")
        raise ConnectionError("uncertain completion")

    async def succeed():
        await asyncio.sleep(0)
        calls.append("success")
        return outcome()

    result = await Mirror(adapter(fail if broken == "sim" else succeed), sim_timeout=1).run(
        operation(), adapter(fail if broken == "real" else succeed)
    )
    assert sorted(calls) == ["fail", "success"]
    assert getattr(result, broken).outcome.status is Status.UNKNOWN
    with pytest.raises(RuntimeError, match="uncertain completion"):
        getattr(result, broken).outcome.require_success()


@pytest.mark.asyncio
async def test_sim_timeout_does_not_cancel_real():
    sim_stopped = asyncio.Event()

    async def sim():
        try:
            await asyncio.Event().wait()
        finally:
            sim_stopped.set()

    async def real():
        await sim_stopped.wait()
        return outcome()

    result = await Mirror(adapter(sim), sim_timeout=0.01).run(operation(), adapter(real))
    assert result.real.outcome.status is Status.SUCCEEDED
    assert result.sim.outcome.status is Status.UNKNOWN
    assert "TimeoutError" in result.sim.outcome.error


@pytest.mark.asyncio
async def test_unsupported_sim_is_rejected_before_real_dispatch():
    called = False

    async def real():
        nonlocal called
        called = True
        return outcome()

    sim = MatterixAdapter("ot2", {}, lambda *args: None)
    with pytest.raises(ValueError, match="No Matterix builder"):
        await Mirror(sim, sim_timeout=1).run(operation(), adapter(real))
    assert not called


@pytest.mark.asyncio
async def test_external_orchestrator_completion_and_device_exclusion():
    async def sim():
        return outcome(evidence=Evidence.SIMULATED)

    mirror = Mirror(adapter(sim), sim_timeout=1)
    pending = mirror.start(operation())
    with pytest.raises(RuntimeError, match="already has"):
        mirror.start(operation(operation_id="step-2"))
    real = Execution(outcome(), 1, 2)
    with pytest.raises(ValueError, match="mismatched"):
        await pending.finish(operation(run_id="other"), real)
    with pytest.raises(ValueError, match="mismatched"):
        await pending.finish(operation(parameters={"volume_ul": 20}), real)
    result = await pending.finish(operation(), real)
    assert result.real is real
    next_pending = mirror.start(operation(operation_id="step-2"))
    with pytest.raises(ValueError):
        await pending.abandon()
    with pytest.raises(RuntimeError):
        mirror.start(operation())  # old handle must not release new device use
    await next_pending.abandon()


@pytest.mark.asyncio
async def test_caller_cancellation_reports_unknown_and_cleans_up():
    started = asyncio.Event()
    count = 0

    async def wait():
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        await asyncio.Event().wait()

    mirror = Mirror(adapter(wait), sim_timeout=10)
    reports = []
    task = asyncio.create_task(mirror.run(operation(), adapter(wait), report=reports.append))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = reports[0]
    assert result.real.outcome.status is Status.UNKNOWN
    assert result.sim.outcome.status is Status.UNKNOWN
    await mirror.start(operation()).abandon()


@pytest.mark.asyncio
async def test_abandon_before_sim_task_starts_is_clean():
    async def sim():
        raise AssertionError("should not run")

    mirror = Mirror(adapter(sim), sim_timeout=1)
    result = await mirror.start(operation()).abandon()
    assert result.outcome.status is Status.UNKNOWN
    await mirror.start(operation()).abandon()


@pytest.mark.asyncio
async def test_native_sequence_is_installed_once_and_awaited():
    submitted = []
    finished = asyncio.Event()

    async def submit(op, configs):
        submitted.append((op, configs))
        await finished.wait()
        return outcome(evidence=Evidence.SIMULATED)

    sim = MatterixAdapter("ot2", {"aspirate": lambda op: ("native-1", "native-2")}, submit)
    pending = Mirror(sim, sim_timeout=1).start(operation())
    joining = asyncio.create_task(pending.finish(operation(), Execution(outcome(), 1, 2)))
    await asyncio.sleep(0)
    assert not joining.done()
    finished.set()
    await joining
    assert submitted == [(operation(), ("native-1", "native-2"))]


@pytest.mark.parametrize(
    "real,sim,reason",
    [
        (outcome(), Outcome(Status.SUCCEEDED), "missing"),
        (outcome(evidence=Evidence.UNKNOWN), outcome(), "unknown"),
        (outcome(None), outcome(), "unknown"),
        (outcome(unit="mL"), outcome(), "unit"),
        (outcome(frame="deck-v1"), outcome(frame="world"), "frame"),
        (Outcome(Status.FAILED), outcome(), "execution"),
        (Outcome(Status.CANCELLED), outcome(), "execution"),
    ],
)
def test_incomparable_observations_never_report_match(real, sim, reason):
    comparison = compare(real, sim, {"volume": 0.1})[0]
    assert comparison.status == "unavailable"
    assert reason in comparison.reason


def test_boolean_is_not_numeric_and_tolerance_is_explicit():
    assert compare(outcome(True), outcome(1), {"volume": 0})[0].status == "different"
    assert compare(outcome(10), outcome(10.1), {"volume": 0.2})[0].status == "match"
    assert compare(outcome(), outcome(), {}) == ()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_bad_tolerance_is_rejected_before_execution(value):
    with pytest.raises(ValueError):
        Mirror(adapter(None), sim_timeout=1, tolerances={"volume": value})


def test_operation_parameters_are_copied_and_deeply_immutable():
    params = {"wells": ["A1"], "nested": {"volume_ul": 10}}
    op = operation(parameters=params)
    params["wells"].append("B1")
    assert op.parameters["wells"] == ("A1",)
    with pytest.raises(TypeError):
        op.parameters["nested"]["volume_ul"] = 20
    with pytest.raises(ValueError):
        operation(parameters={"volume_ul": float("nan")})


def test_import_works_with_stdlib_only_and_no_connector_initialization():
    root = Path(__file__).resolve().parents[1] / "src"
    code = (
        f"import sys; sys.path.insert(0, {str(root)!r}); import ot2_bridge; "
        "assert not any(n.startswith(('unitelabs', 'prefect', 'isaaclab', 'matterix_sm')) for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-I", "-S", "-c", code], check=True)


@pytest.mark.asyncio
async def test_report_reentrancy_does_not_unlock_the_next_pair():
    async def complete():
        return outcome()

    mirror = Mirror(adapter(complete), sim_timeout=1)
    handles = []
    await mirror.run(
        operation(),
        adapter(complete),
        report=lambda result: handles.append(mirror.start(operation(operation_id="next"))),
    )
    with pytest.raises(RuntimeError, match="pending"):
        mirror.start(operation(operation_id="third"))
    await handles[0].abandon()


@pytest.mark.asyncio
async def test_late_sim_completion_is_not_success_even_if_backend_suppresses_cancel():
    async def sim():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return outcome()

    async def real():
        return outcome()

    result = await Mirror(adapter(sim), sim_timeout=0.01).run(operation(), adapter(real))
    assert result.sim.outcome.status is Status.UNKNOWN
    assert "deadline" in result.sim.outcome.error
