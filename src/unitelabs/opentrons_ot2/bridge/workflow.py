"""Read external phase-based workflow JSON without changing its action vocabulary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import WorkflowStep


class WorkflowFormatError(ValueError):
    """The external workflow is malformed or ambiguous."""


@dataclass(frozen=True)
class ParsedWorkflow:
    """Flattened workflow with source phase and thread ownership retained."""

    name: str
    version: str
    steps: tuple[WorkflowStep, ...]
    has_parallel_phases: bool


def _required_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"{field} must be a non-empty string"
        raise WorkflowFormatError(msg)
    return value


def _step(
    raw: object,
    *,
    phase_name: str,
    thread_name: str | None,
) -> WorkflowStep:
    if not isinstance(raw, dict):
        msg = f"Step in phase {phase_name!r} must be an object"
        raise WorkflowFormatError(msg)
    params = raw.get("params", {})
    if not isinstance(params, dict) or not all(isinstance(key, str) for key in params):
        msg = f"Step {raw.get('step_id', '<unknown>')!r} params must be an object with string keys"
        raise WorkflowFormatError(msg)
    description = raw.get("description", "")
    if not isinstance(description, str):
        msg = f"Step {raw.get('step_id', '<unknown>')!r} description must be a string"
        raise WorkflowFormatError(msg)
    return WorkflowStep(
        step_id=_required_string(raw.get("step_id"), "step_id"),
        action=_required_string(raw.get("action"), "action"),
        params=dict(params),
        description=description,
        phase_name=phase_name,
        thread_name=thread_name,
    )


def _load(source: str | Path | dict[str, object]) -> dict[str, object]:
    if isinstance(source, dict):
        return source
    try:
        value = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"Unable to load workflow {source}: {error}"
        raise WorkflowFormatError(msg) from error
    if not isinstance(value, dict):
        msg = "Workflow root must be an object"
        raise WorkflowFormatError(msg)
    return value


def parse_workflow(source: str | Path | dict[str, object]) -> ParsedWorkflow:
    """Parse sequential and parallel phases while preserving external steps."""
    data = _load(source)
    phases = data.get("phases")
    if not isinstance(phases, list):
        msg = "Workflow phases must be a list"
        raise WorkflowFormatError(msg)
    steps: list[WorkflowStep] = []
    has_parallel = False
    for phase_index, raw_phase in enumerate(phases):
        if not isinstance(raw_phase, dict):
            msg = f"phases[{phase_index}] must be an object"
            raise WorkflowFormatError(msg)
        phase_name_value = raw_phase.get("phase_name", raw_phase.get("name", f"phase_{phase_index + 1}"))
        phase_name = _required_string(phase_name_value, f"phases[{phase_index}].phase_name")
        sequential = raw_phase.get("steps", [])
        if not isinstance(sequential, list):
            msg = f"Phase {phase_name!r} steps must be a list"
            raise WorkflowFormatError(msg)
        steps.extend(_step(item, phase_name=phase_name, thread_name=None) for item in sequential)
        threads = raw_phase.get("parallel_threads", [])
        if not isinstance(threads, list):
            msg = f"Phase {phase_name!r} parallel_threads must be a list"
            raise WorkflowFormatError(msg)
        if threads:
            has_parallel = True
        for thread_index, raw_thread in enumerate(threads):
            if not isinstance(raw_thread, dict):
                msg = f"Phase {phase_name!r} parallel_threads[{thread_index}] must be an object"
                raise WorkflowFormatError(msg)
            thread_name = _required_string(
                raw_thread.get("thread_name", raw_thread.get("name")),
                f"parallel_threads[{thread_index}].thread_name",
            )
            thread_steps = raw_thread.get("steps")
            if not isinstance(thread_steps, list):
                msg = f"Parallel thread {thread_name!r} steps must be a list"
                raise WorkflowFormatError(msg)
            steps.extend(_step(item, phase_name=phase_name, thread_name=thread_name) for item in thread_steps)
    identifiers = [step.step_id for step in steps]
    if len(identifiers) != len(set(identifiers)):
        msg = "Workflow step_id values must be globally unique"
        raise WorkflowFormatError(msg)
    return ParsedWorkflow(
        name=_required_string(data.get("workflow_name"), "workflow_name"),
        version=_required_string(str(data.get("version", "1.0")), "version"),
        steps=tuple(steps),
        has_parallel_phases=has_parallel,
    )
