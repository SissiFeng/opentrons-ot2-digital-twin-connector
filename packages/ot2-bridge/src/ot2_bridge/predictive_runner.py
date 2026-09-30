"""Example orchestrator policy for validate-first execution, outside the bridge core."""

from __future__ import annotations

import math
import asyncio
from collections.abc import Mapping

from .comparison import compare
from .mirror import PairResult, _execute
from .models import Evidence, Outcome, Status
from .validation import Check, CheckStatus, Prevalidator, ValidationContext, ValidationRejected
from .wire import fingerprint, plain


def validate_constraints(specs):
    """Validate explicit post-state checks before any simulation or real dispatch."""
    if not isinstance(specs, list) or not specs:
        raise ValueError("validate-first requires a non-empty validation_checks list")
    names = []
    for spec in specs:
        common = {"name", "field", "unit", "frame"}
        if set(spec) not in (common | {"min", "max"}, common | {"equals"}):
            raise ValueError("Check must define either min/max or equals with field/unit/frame")
        if any(not isinstance(spec[k], str) for k in common) or not spec["name"] or not spec["field"]:
            raise ValueError("Invalid validation check identity")
        if "min" in spec:
            if (
                any(
                    isinstance(spec[k], bool) or not isinstance(spec[k], (float, int)) or not math.isfinite(spec[k])
                    for k in ("min", "max")
                )
                or spec["min"] > spec["max"]
            ):
                raise ValueError("Invalid numeric check range")
        elif not isinstance(spec["equals"], (str, bool, int, float)) or (
            isinstance(spec["equals"], float) and not math.isfinite(spec["equals"])
        ):
            raise ValueError("Equality check requires a finite scalar")
        names.append(spec["name"])
    if len(set(names)) != len(names):
        raise ValueError("Duplicate check names")
    return tuple(names)


def validate_dependencies(plan):
    """Require comparable starting-state evidence for every declared dependency."""
    fields = plan.get("validation_start_fields", [])
    if not isinstance(fields, list) or any(not isinstance(f, str) or not f for f in fields):
        raise ValueError("validation_start_fields must be a list of field names")
    dependencies = set(fields) | {s["field"] for s in plan["validation_checks"]}
    missing = dependencies - set(plan["tolerances"])
    if missing:
        raise ValueError(f"Validation dependencies lack starting-state tolerances: {sorted(missing)}")


def evaluate_constraints(outcome, specs):
    """Check only supplied native facts; missing observations cannot certify a prediction."""
    results = []
    for spec in specs:
        fact = outcome.observation.facts.get(spec["field"]) if outcome.observation else None
        status, reason = CheckStatus.UNKNOWN, "Missing simulated fact or incompatible units/frame"
        if (
            fact is not None
            and fact.value is not None
            and fact.evidence is Evidence.SIMULATED
            and (fact.unit, fact.frame) == (spec["unit"], spec["frame"])
        ):
            if "equals" in spec:
                passed = type(fact.value) is type(spec["equals"]) and fact.value == spec["equals"]
                status = CheckStatus.PASSED if passed else CheckStatus.FAILED
                reason = f"Predicted {spec['field']}={fact.value!r}; required {spec['equals']!r}"
            elif isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
                status = CheckStatus.PASSED if spec["min"] <= fact.value <= spec["max"] else CheckStatus.FAILED
                reason = f"Predicted {spec['field']}={fact.value}; permitted [{spec['min']}, {spec['max']}]"
        results.append(Check(spec["name"], status, reason))
    return tuple(results)


async def run_validated(operation, staged, real, plan: Mapping, *, timeout, report):
    """Hold one sample-runner operation until DT and fresh-context checks pass.

    The CLI is the sole dispatcher. An integrating orchestrator MUST additionally
    hold its device/config lock across this entire call and prohibit out-of-band
    mutations. Fingerprints cannot detect an ABA change or replace that lock.
    """
    specs = plain(plan["validation_checks"])
    names = validate_constraints(specs)
    validate_dependencies(plan)

    async def context(*, check_start=False):
        observation = await real.snapshot(operation)
        if not observation.facts or any(
            f.value is None or f.evidence is Evidence.UNKNOWN for f in observation.facts.values()
        ):
            raise ValidationRejected("Fresh known starting-state observation required")
        if check_start:
            comparisons = compare(
                Outcome(Status.SUCCEEDED, observation),
                Outcome(Status.SUCCEEDED, staged.remote.last_observation),
                plan["tolerances"],
            )
            if not comparisons or any(c.status != "match" for c in comparisons):
                raise ValidationRejected("Real and DT starting states are not comparable or differ")
        return ValidationContext(
            fingerprint({"source": observation.source, "facts": observation.facts}), fingerprint(plan)
        )

    try:
        initial = await context(check_start=True)
    except asyncio.CancelledError:
        await staged.discard()
        raise
    except Exception as error:
        await staged.discard()
        report(
            {
                "operation": plain(operation),
                "mode": "validate-first",
                "validation": None,
                "sim": None,
                "real": None,
                "comparisons": [],
                "dispatch": "held",
                "hold_reason": f"{type(error).__name__}: {error}",
            }
        )
        return None
    validator = Prevalidator(
        staged,
        timeout=timeout,
        validity=plan.get("validation_validity_s", 5),
        required_checks=names,
        evaluate=lambda _op, outcome: evaluate_constraints(outcome, specs),
    )
    receipt = await validator.validate(operation, initial)
    record = {
        "operation": plain(operation),
        "mode": "validate-first",
        "validation": plain(receipt),
        "sim": plain(receipt.sim),
        "real": None,
        "comparisons": [],
        "dispatch": "held",
    }
    try:
        # Do local preparation before the final context read and atomic claim.
        call = real.prepare(operation)
        sim = validator.claim(receipt, operation, await context())
    except Exception as error:
        record["hold_reason"] = f"{type(error).__name__}: {error}"
        report(record)
        return None
    # No awaits between claim and starting the prepared real call.
    task = asyncio.create_task(_execute(call))
    try:
        real_result = await asyncio.shield(task)
    except asyncio.CancelledError:
        task.cancel()
        from .mirror import _settle

        real_result = await _settle(task)
        record["real"] = plain(real_result)
        record["dispatch"] = "interrupted"
        report(record)
        raise
    pair = PairResult(operation, real_result, sim, compare(real_result.outcome, sim.outcome, plan["tolerances"]))
    record.update(plain(pair))
    record["dispatch"] = "started"
    record["validation_lead_s"] = real_result.started - sim.finished
    report(record)
    return pair
