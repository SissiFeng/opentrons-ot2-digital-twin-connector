"""One-step predictive validation evidence, independent of dispatch policy.

The orchestrator owns its device lock, context revisions and real dispatch.
A validator is single-use: the simulation has advanced even if validation fails.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from .mirror import Adapter, Execution
from .models import Operation, Outcome, Status
from .wire import fingerprint


class CheckStatus(str, Enum):
    """Explicit validation verdict; success of execution alone is insufficient."""

    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Check:
    """One named check supplied by a deployment's validation model."""

    name: str
    status: CheckStatus
    reason: str = ""

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or not isinstance(self.status, CheckStatus):
            raise ValueError("Checks require a name and CheckStatus")


@dataclass(frozen=True)
class ValidationContext:
    """Caller-owned revisions covering the starting state and all validation configuration."""

    state_revision: str
    configuration_revision: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in (self.state_revision, self.configuration_revision)):
            raise ValueError("Non-empty state/configuration revisions required")


@dataclass(frozen=True)
class ValidationReceipt:
    """Immutable prediction evidence; never a physical safety guarantee."""

    operation: Operation
    context: ValidationContext
    sim: Execution
    checks: tuple[Check, ...]
    required_checks: tuple[str, ...]
    issued_at: float
    expires_at: float

    @property
    def passed(self) -> bool:
        """Require successful execution and an explicit pass for every required check."""
        checks = {check.name: check.status for check in self.checks}
        return (
            self.sim.outcome.status is Status.SUCCEEDED
            and all(checks.get(name) is CheckStatus.PASSED for name in self.required_checks)
            and all(check.status is CheckStatus.PASSED for check in self.checks)
        )


class ValidationRejected(RuntimeError):
    """The caller must hold dispatch and reconcile or obtain a new prediction."""


class Prevalidator:
    """Produce and consume one local validation receipt without controlling hardware.

    Hold the orchestrator's device/config lock from initial context capture
    through claim and dispatch. A revision check alone cannot provide that lock.
    Unknowns, expiry, stale context or changed parameters fail closed.
    """

    def __init__(
        self,
        sim: Adapter,
        *,
        timeout: float,
        validity: float,
        required_checks: Sequence[str],
        evaluate: Callable[[Operation, Outcome], Sequence[Check]],
    ):
        for value in (timeout, validity):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("Timeout and validity must be finite positive numbers")
        names = tuple(required_checks)
        if not names or any(not isinstance(n, str) or not n.strip() for n in names) or len(set(names)) != len(names):
            raise ValueError("Explicit unique required checks are mandatory")
        self.sim, self.timeout, self.validity = sim, timeout, validity
        self.required_checks, self.evaluate = names, evaluate
        self._started, self._consumed, self._receipt = False, False, None

    async def validate(self, operation: Operation, context: ValidationContext) -> ValidationReceipt:
        """Run DT first, await completion, and evaluate only declared model checks."""
        if self._started:
            raise ValidationRejected("Validator already used; reconcile simulation before a new validation")
        if not isinstance(context, ValidationContext):
            raise ValueError("ValidationContext required")
        call = self.sim.prepare(operation)
        self._started = True
        started = time.monotonic()
        try:
            outcome = await asyncio.wait_for(call(), self.timeout)
            if not isinstance(outcome, Outcome):
                raise TypeError("Adapter must return Outcome")
            if time.monotonic() - started > self.timeout:
                outcome = Outcome(Status.UNKNOWN, error="DT completed after validation deadline")
        except asyncio.CancelledError:
            self._consumed = True
            raise
        except Exception as error:
            outcome = Outcome(Status.UNKNOWN, error=f"{type(error).__name__}: {error}")
        execution = Execution(outcome, started, time.monotonic())
        checks = ()
        if outcome.status is Status.SUCCEEDED:
            try:
                checks = tuple(self.evaluate(operation, outcome))
                if any(not isinstance(c, Check) for c in checks) or len({c.name for c in checks}) != len(checks):
                    raise ValueError("Evaluator returned invalid or duplicate checks")
            except Exception as error:
                checks = (Check("evaluator", CheckStatus.UNKNOWN, f"{type(error).__name__}: {error}"),)
        issued = execution.finished
        self._receipt = ValidationReceipt(
            operation, context, execution, checks, self.required_checks, issued, issued + self.validity
        )
        return self._receipt

    def claim(self, receipt: ValidationReceipt, operation: Operation, current: ValidationContext) -> Execution:
        """Consume evidence once immediately before caller-owned real dispatch.

        Returns the original prediction for subsequent boundary comparison, so
        the simulation is not executed a second time. All rejection is terminal.
        """
        if self._consumed or receipt is not self._receipt or receipt is None:
            raise ValidationRejected("Receipt is foreign or already consumed")
        self._consumed = True
        if fingerprint(receipt.operation) != fingerprint(operation) or receipt.context != current:
            raise ValidationRejected("Operation, starting state or configuration changed")
        if time.monotonic() >= receipt.expires_at:
            raise ValidationRejected("Validation receipt expired")
        if not receipt.passed:
            raise ValidationRejected("DT failed, timed out, or required checks did not pass")
        return receipt.sim
