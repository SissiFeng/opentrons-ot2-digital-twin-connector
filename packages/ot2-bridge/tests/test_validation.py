"""Pre-dispatch prevention, stale evidence and explicit check coverage."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ot2_bridge import CallableAdapter, Outcome, Status
from ot2_bridge.cli import run_demo
from ot2_bridge.demo import DemoBackend, operations
from ot2_bridge.validation import Check, CheckStatus, Prevalidator, ValidationContext, ValidationRejected

CTX = ValidationContext("state-1", "config-1")


def validator(*, outcome=None, checks=None, timeout=1, validity=1):
    call = AsyncMock(return_value=outcome or Outcome(Status.SUCCEEDED))
    sim = CallableAdapter(lambda _op: call)
    return Prevalidator(
        sim,
        timeout=timeout,
        validity=validity,
        required_checks=["capacity"],
        evaluate=lambda _op, _result: checks if checks is not None else [Check("capacity", CheckStatus.PASSED)],
    ), call


async def test_receipt_is_single_use_and_exact_operation_context_bound():
    gate, call = validator()
    op = operations()[0]
    receipt = await gate.validate(op, CTX)
    assert receipt.passed and gate.claim(receipt, op, CTX) is receipt.sim
    call.assert_awaited_once()
    with pytest.raises(ValidationRejected, match="consumed"):
        gate.claim(receipt, op, CTX)
    with pytest.raises(ValidationRejected, match="already used"):
        await gate.validate(op, CTX)


@pytest.mark.parametrize(
    "current", [ValidationContext("state-2", "config-1"), ValidationContext("state-1", "config-2")]
)
async def test_stale_state_or_configuration_never_authorizes_dispatch(current):
    gate, _ = validator()
    op = operations()[0]
    receipt = await gate.validate(op, CTX)
    with pytest.raises(ValidationRejected, match="changed"):
        gate.claim(receipt, op, current)
    with pytest.raises(ValidationRejected, match="consumed"):
        gate.claim(receipt, op, CTX)


async def test_changed_parameters_and_foreign_receipt_rejected():
    gate, _ = validator()
    op = operations()[0]
    receipt = await gate.validate(op, CTX)
    with pytest.raises(ValidationRejected, match="foreign"):
        gate.claim(replace(receipt), op, CTX)
    with pytest.raises(ValidationRejected, match="changed"):
        gate.claim(receipt, replace(op, parameters={"axes": "XYZ"}), CTX)


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [Check("capacity", CheckStatus.UNKNOWN)],
        [Check("capacity", CheckStatus.FAILED)],
        [Check("different_check", CheckStatus.PASSED)],
        [Check("capacity", CheckStatus.PASSED)] * 2,
    ],
)
async def test_successful_dt_execution_is_not_a_validation_pass(checks):
    gate, _ = validator(checks=checks)
    op = operations()[0]
    receipt = await gate.validate(op, CTX)
    assert not receipt.passed
    with pytest.raises(ValidationRejected):
        gate.claim(receipt, op, CTX)


async def test_expired_receipt(monkeypatch):
    gate, _ = validator()
    op = operations()[0]
    receipt = await gate.validate(op, CTX)
    monkeypatch.setattr("ot2_bridge.validation.time.monotonic", lambda: receipt.expires_at)
    with pytest.raises(ValidationRejected, match="expired"):
        gate.claim(receipt, op, CTX)


async def test_timeout_and_cancel_never_create_usable_permission():
    async def slow():
        await asyncio.sleep(1)
        return Outcome(Status.SUCCEEDED)

    gate = Prevalidator(
        CallableAdapter(lambda _op: slow),
        timeout=0.001,
        validity=1,
        required_checks=["capacity"],
        evaluate=lambda *_: [Check("capacity", CheckStatus.PASSED)],
    )
    receipt = await gate.validate(operations()[0], CTX)
    assert receipt.sim.outcome.status is Status.UNKNOWN and not receipt.passed
    gate = Prevalidator(
        CallableAdapter(lambda _op: slow), timeout=1, validity=1, required_checks=["capacity"], evaluate=lambda *_: []
    )
    task = asyncio.create_task(gate.validate(operations()[0], CTX))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ValidationRejected):
        gate.claim(None, operations()[0], CTX)


@pytest.mark.parametrize("fail,drift,expected", [("", 0, 0), ("aspirate", 0, 5), ("", 400, 5)])
async def test_network_validate_first_prevents_real_dispatch(tmp_path, monkeypatch, fail, drift, expected):
    dispatched = []
    original = DemoBackend.prepare

    def prepare(self, op):
        call = original(self, op)

        async def execute():
            if self.source == "demo-real":
                dispatched.append(op.operation_id)
            return await call()

        return execute

    monkeypatch.setattr(DemoBackend, "prepare", prepare)
    args = SimpleNamespace(out=str(tmp_path), drift=drift, fail_action=fail, mode="validate-first")
    assert await run_demo(args) == expected
    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    if expected == 5:
        assert dispatched == ["1", "2", "3"]
        assert records[-1]["real"] is None and records[-1]["dispatch"] == "held"
        if drift:
            assert records[-1]["sim"]["outcome"]["status"] == "succeeded"
            assert records[-1]["validation"]["checks"][0]["status"] == "failed"
    else:
        assert len(dispatched) == 7
        assert all(r["sim"]["finished"] <= r["real"]["started"] for r in records)
        assert all(r["validation_lead_s"] >= 0 for r in records)


async def test_state_mutation_during_prediction_holds_real_dispatch(tmp_path, monkeypatch):
    original = DemoBackend.snapshot
    reads = 0

    async def snapshot(self, op):
        nonlocal reads
        if self.source == "demo-real":
            reads += 1
            if reads == 2:
                self.volume = 1  # Out-of-band mutation after prediction began.
        return await original(self, op)

    monkeypatch.setattr(DemoBackend, "snapshot", snapshot)
    args = SimpleNamespace(out=str(tmp_path), drift=0, fail_action="", mode="validate-first")
    assert await run_demo(args) == 5
    record = json.loads((tmp_path / "trace.jsonl").read_text())
    assert record["real"] is None and "changed" in record["hold_reason"]


async def test_starting_state_mismatch_does_not_run_either_side(tmp_path, monkeypatch):
    original = DemoBackend.snapshot

    async def snapshot(self, op):
        self.volume = 1
        return await original(self, op)

    monkeypatch.setattr(DemoBackend, "snapshot", snapshot)
    args = SimpleNamespace(out=str(tmp_path), drift=0, fail_action="", mode="validate-first")
    assert await run_demo(args) == 5
    record = json.loads((tmp_path / "trace.jsonl").read_text())
    assert record["real"] is None and record["sim"] is None


def test_missing_observation_or_unit_mismatch_is_unknown():
    from ot2_bridge import Evidence, Fact, Observation
    from ot2_bridge.predictive_runner import evaluate_constraints

    specs = [{"name": "capacity", "field": "volume", "unit": "uL", "frame": "", "min": 0, "max": 300}]
    for outcome in (
        Outcome(Status.SUCCEEDED),
        Outcome(Status.SUCCEEDED, Observation("sim", {"volume": Fact(20, Evidence.SIMULATED, "mL")})),
    ):
        assert evaluate_constraints(outcome, specs)[0].status is CheckStatus.UNKNOWN


@pytest.mark.parametrize("original,replacement", [(1, True), ({"nested": [1]}, {"nested": [True]})])
async def test_exact_binding_distinguishes_json_parameter_types(original, replacement):
    gate, _ = validator()
    op = replace(operations()[0], parameters={"value": original})
    receipt = await gate.validate(op, CTX)
    with pytest.raises(ValidationRejected, match="changed"):
        gate.claim(receipt, replace(op, parameters={"value": replacement}), CTX)


def test_validation_cannot_omit_its_starting_state_dependencies():
    from ot2_bridge.cli import demo_plan
    from ot2_bridge.predictive_runner import validate_dependencies

    plan = demo_plan()
    del plan["tolerances"]["pipette.volume"]
    with pytest.raises(ValueError, match="dependencies"):
        validate_dependencies(plan)
    plan = demo_plan()
    plan["validation_start_fields"] = ["axis.X"]
    with pytest.raises(ValueError, match=r"axis\.X"):
        validate_dependencies(plan)
