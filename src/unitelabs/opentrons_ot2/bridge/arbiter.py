"""
Mode-driven execution arbitration for OT-2 bridge side effects.

Brings the PoC arbiter (sim_only / real_only / sim_first_then_real /
shadow) to the contract-pinned bridge.  A simulator transport replays
the same workflow before any real hardware side effect, and shadow mode
compares simulator vs real state after each step.

Modes:
  SIM_ONLY             — preflight + replay on the simulator only.
  REAL_ONLY            — preflight + run on real hardware; simulator never consulted.
  SIM_FIRST_THEN_REAL  — preflight, simulator dry-run gate, then real execution.
  SHADOW               — run simulator and real in lockstep and report divergence.
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..digital_twin.config import DigitalTwinConfig
from .dry_run import DryRunResult, dry_run_workflow
from .executor import OT2BridgeExecutor, OT2PreflightError
from .models import EndpointKind, PreflightExpectation, PreflightReport, WorkflowStep
from .workflow import ParsedWorkflow


class RunMode(str, enum.Enum):
    """Execution mode for the OT-2 arbiter."""

    SIM_ONLY = "sim_only"
    REAL_ONLY = "real_only"
    SIM_FIRST_THEN_REAL = "sim_first_then_real"
    SHADOW = "shadow"


class StepRunner(Protocol):
    """A minimal runner consumed by the arbiter (an OT2BridgeExecutor)."""

    async def execute_step(self, step: WorkflowStep) -> object:
        """Run one workflow step and return its response."""
        ...


class DryRunFunction(Protocol):
    """Replay a workflow against a simulator executor."""

    def __call__(
        self,
        executor: OT2BridgeExecutor,
        steps: Sequence[WorkflowStep],
    ) -> DryRunResult | Awaitable[DryRunResult]:
        """Replay a workflow against a simulator executor."""
        ...


@dataclass
class DivergenceAlert:
    """A state divergence between simulator and real execution."""

    step_index: int
    field: str
    predicted: object
    actual: object
    tolerance: float | None


@dataclass
class ArbiterResult:
    """Outcome of an arbiter run."""

    mode: RunMode
    preflight: PreflightReport | None = None
    preflight_error: str | None = None
    dry_run: DryRunResult | None = None
    halted_before_real: bool = False
    halt_reason: str | None = None
    real_responses: list[object] = field(default_factory=list)
    sim_responses: list[object] = field(default_factory=list)
    divergence_alerts: list[DivergenceAlert] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the arbiter completed without halting before real."""
        if self.preflight_error is not None or self.preflight is None:
            return False
        if self.halted_before_real:
            return False
        return self.mode in (
            RunMode.SIM_ONLY,
            RunMode.REAL_ONLY,
            RunMode.SIM_FIRST_THEN_REAL,
            RunMode.SHADOW,
        )


class OT2ExecutionArbiter:
    """Gate and dispatch bridge execution across simulator and real runners."""

    def __init__(
        self,
        real_executor: OT2BridgeExecutor,
        sim_executor: OT2BridgeExecutor | None = None,
        *,
        mode: RunMode = RunMode.SIM_FIRST_THEN_REAL,
        dry_run_fn: DryRunFunction | None = None,
        divergence_tolerance_mm: float = 1.0,
        config: DigitalTwinConfig | None = None,
    ) -> None:
        """Configure an arbiter for one workflow run."""
        self._real = real_executor
        self._sim = sim_executor
        self._mode = mode
        self._dry_run_fn = dry_run_fn
        self._divergence_tolerance_mm = divergence_tolerance_mm
        self._config = config

    @property
    def mode(self) -> RunMode:
        """Return the configured execution mode."""
        return self._mode

    async def preflight(self, expected: PreflightExpectation) -> PreflightReport:
        """Run preflight on the active execution target(s)."""
        if self._mode is RunMode.SIM_ONLY:
            if self._sim is None:
                msg = "SIM_ONLY requires a simulator executor"
                raise OT2PreflightError(msg)
            return await self._sim.preflight(expected)
        if self._mode is RunMode.REAL_ONLY:
            return await self._real.preflight(expected)
        if self._sim is None:
            msg = f"{self._mode.value} requires a simulator executor"
            raise OT2PreflightError(msg)
        # SIM_FIRST_THEN_REAL / SHADOW: preflight real (authoritative) and sim.
        report = await self._real.preflight(expected)
        await self._sim.preflight(expected)
        return report

    async def run(
        self,
        workflow: ParsedWorkflow,
        expected: PreflightExpectation,
    ) -> ArbiterResult:
        """Execute a parsed workflow under the configured mode."""
        result = ArbiterResult(mode=self._mode)
        try:
            result.preflight = await self.preflight(expected)
        except OT2PreflightError as error:
            result.preflight_error = str(error)
            result.halted_before_real = True
            result.halt_reason = f"preflight {type(error).__name__}: {error}"
            return result

        steps = tuple(workflow.steps)
        if self._mode is RunMode.SIM_ONLY:
            result.sim_responses = await self._run_all(self._sim, steps)
            return result

        if self._mode is RunMode.REAL_ONLY:
            result.real_responses = await self._run_all(self._real, steps)
            return result

        if self._mode is RunMode.SIM_FIRST_THEN_REAL:
            if self._dry_run_fn is None:
                if self._config is None:
                    result.halted_before_real = True
                    result.halt_reason = "SIM_FIRST_THEN_REAL requires a dry_run_fn or a config"
                    return result
                result.dry_run = dry_run_workflow(
                    self._config,
                    self._real._adapter,
                    steps,
                )
            else:
                dry_run = self._dry_run_fn(self._sim, steps)
                result.dry_run = await dry_run if inspect.isawaitable(dry_run) else dry_run
            if not result.dry_run.ok:
                result.halted_before_real = True
                result.halt_reason = (
                    f"sim dry-run {result.dry_run.error_class}: {result.dry_run.reason}"
                    if result.dry_run.error_class
                    else f"sim dry-run: {result.dry_run.reason}"
                )
                return result
            result.real_responses = await self._run_all(self._real, steps)
            return result

        # SHADOW: run sim and real in lockstep, comparing connector state
        # immediately after every side-effecting step (mirrors the PoC
        # per-action observation comparison). Divergence is an observation,
        # not a failure; the caller decides how to react.
        sim_responses, real_responses, alerts = await self._run_shadow(steps)
        result.sim_responses = sim_responses
        result.real_responses = real_responses
        result.divergence_alerts = alerts
        return result

    async def _run_all(
        self,
        executor: OT2BridgeExecutor | None,
        steps: Sequence[WorkflowStep],
    ) -> list[object]:
        """Run every step on one executor, preserving order."""
        if executor is None:
            msg = "An executor is required for this mode"
            raise OT2PreflightError(msg)
        return [await executor.execute_step(step) for step in steps]

    async def _run_shadow(
        self,
        steps: Sequence[WorkflowStep],
    ) -> tuple[list[object], list[object], list[DivergenceAlert]]:
        """Execute every step on both executors and compare state per step."""
        if self._sim is None or self._real is None:
            msg = f"{RunMode.SHADOW.value} requires a simulator executor"
            raise OT2PreflightError(msg)
        sim_responses: list[object] = []
        real_responses: list[object] = []
        alerts: list[DivergenceAlert] = []
        for index, step in enumerate(steps):
            command = self._real._adapter.adapt(step)
            if command is None:
                continue
            sim_responses.append(await self._sim.execute(command))
            real_responses.append(await self._real.execute(command))
            if not command.side_effecting:
                continue
            sim_state = await self._read_state(self._sim)
            real_state = await self._read_state(self._real)
            alerts.extend(self._compare_state(index, sim_state, real_state))
        return sim_responses, real_responses, alerts

    def _compare_state(
        self,
        step_index: int,
        sim_state: object,
        real_state: object,
    ) -> list[DivergenceAlert]:
        """Return alerts for connector-state fields that diverged after one step."""
        alerts: list[DivergenceAlert] = []
        state_fields = ("tip_presence", "tip_evidence", "liquid_volume", "liquid_volume_known", "homed")
        for state_field in state_fields:
            predicted = _deep_get(sim_state, ("mounts", 0, state_field))
            actual = _deep_get(real_state, ("mounts", 0, state_field))
            if _diverges(predicted, actual, self._divergence_tolerance_mm):
                alerts.append(
                    DivergenceAlert(
                        step_index=step_index,
                        field=state_field,
                        predicted=predicted,
                        actual=actual,
                        tolerance=self._divergence_tolerance_mm,
                    )
                )
        return alerts

    async def _read_state(self, executor: OT2BridgeExecutor) -> object:
        command = executor._adapter.registry.command(
            step_id="__shadow_state__",
            action="bridge.shadow",
            feature_identifier="RobotStateProvider",
            endpoint="CurrentState",
            parameters={},
            kind=EndpointKind.PROPERTY,
            side_effecting=False,
        )
        return await executor._transport.execute(command)


def _deep_get(value: object, path: tuple[str | int, ...]) -> object:
    current = value
    for key in path:
        if (isinstance(current, dict) and key in current) or (
            isinstance(current, list) and isinstance(key, int) and -len(current) <= key < len(current)
        ):
            current = current[key]
        else:
            return None
    return current


def _diverges(predicted: object, actual: object, tolerance: float = 1.0) -> bool:
    if predicted is None or actual is None:
        return predicted != actual
    if isinstance(predicted, float) and isinstance(actual, float):
        return abs(predicted - actual) > tolerance
    return predicted != actual


async def default_dry_run(
    executor: OT2BridgeExecutor,
    steps: Sequence[WorkflowStep],
) -> DryRunResult:
    """Replay a workflow against a simulator executor and capture the first failure."""
    for index, step in enumerate(steps):
        try:
            await executor.execute_step(step)
        except Exception as error:  # noqa: BLE001 - structured dry-run result
            return DryRunResult(
                ok=False,
                reason=str(error),
                error_class=type(error).__name__,
                failed_step_index=index,
            )
    return DryRunResult(ok=True)


__all__ = [
    "ArbiterResult",
    "DivergenceAlert",
    "DryRunFunction",
    "DryRunResult",
    "OT2ExecutionArbiter",
    "RunMode",
    "default_dry_run",
]
