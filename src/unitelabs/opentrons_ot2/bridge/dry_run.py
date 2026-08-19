"""
Physical feasibility dry-run for OT-2 workflows before real execution.

Replays the adapted side effects against an in-process simulator controller
and rejects workflows whose targets fall outside configured deck bounds,
reference unknown wells, or violate the pipette volume envelope.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError, InstrumentProfile
from .adapter import OT2StepError, OT2WorkflowAdapter
from .models import OT2Command, WorkflowStep

_MOUNT_AXIS = {"LEFT": "Z", "RIGHT": "A"}


@dataclass(frozen=True)
class DryRunResult:
    """Outcome of the physical-feasibility dry-run."""

    ok: bool
    reason: str | None = None
    error_class: str | None = None
    failed_step_index: int | None = None


class PhysicalInfeasibilityError(RuntimeError):
    """The workflow cannot be executed within configured physical limits."""


def _workflow_commands(
    adapter: OT2WorkflowAdapter,
    steps: Sequence[WorkflowStep],
) -> list[tuple[int, OT2Command]]:
    """Adapt every step, skipping setup and non-OT-2 actions."""
    commands: list[tuple[int, OT2Command]] = []
    for index, step in enumerate(steps):
        try:
            command = adapter.adapt(step)
        except OT2StepError as error:
            raise _InfeasibleStep(index, str(error)) from error
        if command is None:
            continue
        if not command.side_effecting:
            commands.append((index, command))
            continue
        commands.append((index, command))
    return commands


class _InfeasibleStep(PhysicalInfeasibilityError):
    """An external step cannot be represented by the connector contract."""

    def __init__(self, step_index: int, reason: str) -> None:
        super().__init__(reason)
        self.step_index = step_index


def dry_run_workflow(
    config: DigitalTwinConfig,
    adapter: OT2WorkflowAdapter,
    steps: Sequence[WorkflowStep],
) -> DryRunResult:
    """
    Validate workflow geometry against the pinned configuration.

    This is a pure, fast, hardware-free gate: it resolves every MoveToWell /
    PickUpTip target from the configuration, checks deck bounds, and verifies
    aspirate/dispense volumes against the instrument profile.
    """
    try:
        commands = _workflow_commands(adapter, steps)
        for step_index, command in commands:
            _validate_command(config, command, step_index)
    except (DigitalTwinConfigurationError, _InfeasibleStep) as error:
        failed_index = error.step_index if isinstance(error, _InfeasibleStep) else None
        return DryRunResult(
            ok=False,
            reason=str(error),
            error_class="PhysicalInfeasibilityError",
            failed_step_index=failed_index,
        )
    except PhysicalInfeasibilityError as error:
        return DryRunResult(
            ok=False,
            reason=str(error),
            error_class="PhysicalInfeasibilityError",
            failed_step_index=step_index,
        )
    return DryRunResult(ok=True)


def _validate_command(config: DigitalTwinConfig, command: OT2Command, step_index: int) -> None:
    profile = _mount_profile(config, command, step_index)
    if command.endpoint == "MoveTo":
        position = command.parameters.get("position")
        x = float(getattr(position, "x", float("nan")))
        y = float(getattr(position, "y", float("nan")))
        z = float(getattr(position, "z", float("nan")))
        _check_point(config, x, y, z, step_index)
    elif command.endpoint in ("MoveToWell", "PickUpTip"):
        labware_id = str(command.parameters.get("labware_id", ""))
        well = str(command.parameters.get("well", ""))
        try:
            point = config.resolve_well(labware_id, well, use_approach=False)
        except DigitalTwinConfigurationError as error:
            message = f"Unknown well {labware_id}/{well}: {error}"
            raise PhysicalInfeasibilityError(message) from error
        _check_point(config, point.x, point.y, point.z, step_index)
    elif command.endpoint in ("Aspirate", "Dispense"):
        volume = float(command.parameters.get("volume", 0.0))
        mount = profile.mount
        if not math.isfinite(volume) or volume <= 0 or volume > profile.maximum_volume:
            message = f"Volume {volume} is outside {mount} envelope 0-{profile.maximum_volume} (step {step_index})"
            raise PhysicalInfeasibilityError(message)


def _mount_profile(
    config: DigitalTwinConfig,
    command: OT2Command,
    step_index: int,
) -> InstrumentProfile:
    """Resolve the instrument profile for a command, defaulting to the single mount."""
    raw_mount = command.parameters.get("mount", "")
    mount = str(getattr(raw_mount, "value", raw_mount)).upper()
    if mount:
        try:
            return config.instrument(mount)
        except DigitalTwinConfigurationError as error:
            message = f"Unknown mount {mount!r}: {error}"
            raise PhysicalInfeasibilityError(message) from error
    if len(config.instruments) == 1:
        return config.instruments[0]
    message = f"Step {step_index} does not identify a pipette mount; add 'mount' or a pipette alias"
    raise PhysicalInfeasibilityError(message)


def _check_point(config: DigitalTwinConfig, x: float, y: float, z: float, step_index: int) -> None:
    if not all(math.isfinite(v) for v in (x, y, z)):
        message = f"Non-finite target at step {step_index}"
        raise PhysicalInfeasibilityError(message)
    deck = config.deck_bounds
    if not (
        deck.minimum.x <= x <= deck.maximum.x
        and deck.minimum.y <= y <= deck.maximum.y
        and deck.minimum.z <= z <= deck.maximum.z
    ):
        message = f"Target ({x}, {y}, {z}) outside deck bounds at step {step_index}"
        raise PhysicalInfeasibilityError(message)


__all__ = [
    "DryRunResult",
    "PhysicalInfeasibilityError",
    "dry_run_workflow",
]
