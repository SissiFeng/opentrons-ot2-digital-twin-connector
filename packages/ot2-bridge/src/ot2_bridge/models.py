"""Transport-independent operation identities and immutable observations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType


@dataclass(frozen=True)
class Operation:
    """One semantic operation; schema defines parameters, units and capabilities.

    IDs are supplied by the orchestrator. The bridge does not allocate workflow
    IDs, infer actions from axis motion, or implement durable deduplication.
    """

    run_id: str
    operation_id: str
    device_id: str
    action: str
    parameters: Mapping[str, object]
    schema: str = "ot2.operation/1"

    def __post_init__(self) -> None:
        for value in (self.run_id, self.operation_id, self.device_id, self.action, self.schema):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Operation identities must be non-empty strings")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("Operation parameters must be a mapping")
        object.__setattr__(self, "parameters", _freeze(self.parameters))


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Operation parameter keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Operation parameters must contain finite JSON values")


class Evidence(str, Enum):
    """Provenance of a fact, preserved independently of its value."""

    HARDWARE = "hardware_reported"
    TRACKED = "software_tracked"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Fact:
    """One normalized scalar; adapters own unit and frame conversion."""

    value: float | int | str | bool | None
    evidence: Evidence
    unit: str = ""
    frame: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, Evidence):
            raise ValueError("Fact evidence must be an Evidence value")
        if self.value is not None and not isinstance(self.value, (float, int, str, bool)):
            raise ValueError("Fact values must be scalars")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Fact values must be finite")


@dataclass(frozen=True)
class Observation:
    """Adapter-normalized post-operation facts with source time and revision.

    Capture after backend completion, before its next operation. Source time is
    informational; the bridge compares operation boundaries, not wall clocks.
    """

    source: str
    facts: Mapping[str, Fact] = field(default_factory=dict)
    revision: str = ""
    observed_at: str = ""

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("Observation source is required")
        if not all(isinstance(key, str) and isinstance(value, Fact) for key, value in self.facts.items()):
            raise ValueError("Observation facts must map names to Fact values")
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))


class Status(str, Enum):
    """Execution outcome; UNKNOWN includes uncertain effects after disconnect."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Outcome:
    """Backend completion evidence, never an instruction to proceed or retry."""

    status: Status
    observation: Observation | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, Status):
            raise ValueError("Outcome status must be a Status value")
        if self.observation is not None and not isinstance(self.observation, Observation):
            raise ValueError("Outcome observation must be an Observation")

    def require_success(self) -> None:
        """Raise in a workflow when real execution did not confirm success."""
        if self.status is not Status.SUCCEEDED:
            raise RuntimeError(f"Backend {self.status.value}: {self.error}")
