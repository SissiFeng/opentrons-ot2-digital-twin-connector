"""Small concurrent operation pairing layer; no workflow or hardware policy."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .comparison import Comparison, compare
from .models import Operation, Outcome, Status

Call = Callable[[], Awaitable[Outcome]]


class Adapter(Protocol):
    """Map and validate without side effects, then return one completion call."""

    def prepare(self, operation: Operation) -> Call:
        """Reject unsupported operations before scheduling either backend."""
        ...


@dataclass(frozen=True)
class Execution:
    """Host monotonic timing and one backend's reported outcome."""

    outcome: Outcome
    started: float
    finished: float


@dataclass(frozen=True)
class PairResult:
    """Correlated results; no combined success flag or proceed decision."""

    operation: Operation
    real: Execution
    sim: Execution
    comparisons: tuple[Comparison, ...]


async def _settle(task: asyncio.Task[Execution]) -> Execution:
    try:
        return await task
    except asyncio.CancelledError:
        now = time.monotonic()
        return Execution(Outcome(Status.UNKNOWN, error="await cancelled before completion"), now, now)


async def _execute(call: Call) -> Execution:
    started = time.monotonic()
    try:
        outcome = await call()
        if not isinstance(outcome, Outcome):
            raise TypeError("Adapter completion must return Outcome")
    except asyncio.CancelledError:
        outcome = Outcome(Status.UNKNOWN, error="await cancelled; backend stop is not confirmed")
    except Exception as error:
        outcome = Outcome(Status.UNKNOWN, error=f"{type(error).__name__}: {error}")
    return Execution(outcome, started, time.monotonic())


class Mirror:
    """Pair one operation per device; the orchestrator supplies operation order.

    One instance belongs to one asyncio loop. It rejects overlapping operations
    on a device instead of maintaining another workflow queue. Session-local
    exclusion is not a distributed lock or durable idempotency guarantee.
    """

    def __init__(self, sim: Adapter, *, sim_timeout: float, tolerances: Mapping[str, float] | None = None) -> None:
        if isinstance(sim_timeout, bool) or not math.isfinite(sim_timeout) or sim_timeout <= 0:
            raise ValueError("sim_timeout must be finite and positive")
        self._sim = sim
        self._timeout = sim_timeout
        self._tolerances = dict(tolerances or {})
        # Validate before any operation can have side effects.
        compare(Outcome(Status.UNKNOWN), Outcome(Status.UNKNOWN), self._tolerances)
        self._active: set[str] = set()

    def start(self, operation: Operation) -> PendingMirror:
        """Start sim for an externally owned real operation.

        Call immediately before real dispatch. Finish with the matching real
        completion, or explicitly abandon to cancel only the sim await.
        """
        if operation.device_id in self._active:
            raise RuntimeError(f"Device {operation.device_id!r} already has a pending pair")
        call = self._sim.prepare(operation)

        async def bounded() -> Outcome:
            deadline = time.monotonic() + self._timeout
            outcome = await asyncio.wait_for(call(), timeout=self._timeout)
            if time.monotonic() > deadline:
                return Outcome(Status.UNKNOWN, error="sim completed after its deadline")
            return outcome

        self._active.add(operation.device_id)
        return PendingMirror(self, operation, asyncio.create_task(_execute(bounded)))

    async def run(
        self, operation: Operation, real: Adapter, *, report: Callable[[PairResult], None] | None = None
    ) -> PairResult:
        """Wrap one orchestrator-owned real call and mirror it concurrently.

        Exceptions become UNKNOWN, unless an adapter supplies a more precise
        outcome. Neither peer's failure cancels the other. Caller cancellation
        cancels both awaits. The optional report sink receives partial evidence
        before CancelledError propagates; it must be synchronous and non-throwing.
        """
        real_call = real.prepare(operation)
        pending = self.start(operation)
        real_task = asyncio.create_task(_execute(real_call))
        try:
            real_result = await asyncio.shield(real_task)
            result = await pending.finish(operation, real_result)
            if report is not None:
                report(result)
            return result
        except asyncio.CancelledError:
            real_task.cancel()
            pending._task.cancel()
            real_result, sim_result = await asyncio.gather(_settle(real_task), _settle(pending._task))
            result = pending._result(real_result, sim_result)
            if report is not None:
                report(result)
            raise


class PendingMirror:
    """A sim execution awaiting a correlated externally reported real result."""

    def __init__(self, owner: Mirror, operation: Operation, task: asyncio.Task[Execution]) -> None:
        self._owner = owner
        self.operation = operation
        self._task = task
        self._finished = False
        self._joining = False

    async def finish(self, operation: Operation, real: Execution) -> PairResult:
        """Join a real completion; mismatched identities or payloads are rejected."""
        if self._finished or self._joining or operation != self.operation:
            raise ValueError("Already finished or mismatched operation completion")
        if not isinstance(real, Execution) or not isinstance(real.outcome, Outcome):
            raise TypeError("Real completion must be an Execution")
        self._joining = True
        try:
            sim = await asyncio.shield(self._task)
            return self._result(real, sim)
        finally:
            self._joining = False

    def _result(self, real: Execution, sim: Execution) -> PairResult:
        self._finished = True
        self._owner._active.discard(self.operation.device_id)
        return PairResult(self.operation, real, sim, compare(real.outcome, sim.outcome, self._owner._tolerances))

    async def abandon(self) -> Execution:
        """Cancel only the sim await and release local exclusion; real is external."""
        if self._finished or self._joining:
            raise ValueError("Mirror is already finished or joining")
        self._joining = True
        self._task.cancel()
        try:
            return await _settle(self._task)
        finally:
            self._finished = True
            self._owner._active.discard(self.operation.device_id)
