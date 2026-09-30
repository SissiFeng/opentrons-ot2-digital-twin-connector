"""Compare explicit operation-boundary evidence without making decisions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .models import Evidence, Fact, Outcome, Status


@dataclass(frozen=True)
class Comparison:
    """One requested comparison: match, different, or unavailable."""

    field: str
    status: str
    real: Fact | None
    sim: Fact | None
    reason: str = ""


def compare(real: Outcome, sim: Outcome, tolerances: Mapping[str, float]) -> tuple[Comparison, ...]:
    """Compare only requested fields in matching units and coordinate frames."""
    results = []
    for name, tolerance in tolerances.items():
        if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("Comparison tolerances must be finite and non-negative")
        left = real.observation.facts.get(name) if real.observation else None
        right = sim.observation.facts.get(name) if sim.observation else None
        reason = ""
        if real.status is not Status.SUCCEEDED or sim.status is not Status.SUCCEEDED:
            reason = "execution did not succeed on both sides"
        elif left is None or right is None:
            reason = "missing observation"
        elif left.value is None or right.value is None or Evidence.UNKNOWN in (left.evidence, right.evidence):
            reason = "unknown evidence"
        elif (left.unit, left.frame) != (right.unit, right.frame):
            reason = "unit or frame mismatch"
        if reason:
            results.append(Comparison(name, "unavailable", left, right, reason))
            continue
        # The checks above guarantee both facts and values are present.
        numeric = all(
            isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool) for fact in (left, right)
        )
        same = (
            abs(left.value - right.value) <= tolerance
            if numeric
            else type(left.value) is type(right.value) and left.value == right.value
        )
        results.append(Comparison(name, "match" if same else "different", left, right))
    return tuple(results)
