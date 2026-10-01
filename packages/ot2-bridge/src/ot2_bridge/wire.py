"""Versioned JSON exchange and caller-owned trace serialization."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from enum import Enum

from .models import Evidence, Fact, Observation, Operation, Outcome, Status


def plain(value):
    """Convert immutable contract objects to finite JSON-compatible values."""
    if dataclasses.is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def dumps(value) -> str:
    """Canonical, finite JSON used both for transport and configuration fingerprints."""
    return json.dumps(plain(value), sort_keys=True, allow_nan=False, separators=(",", ":"))


def fingerprint(value) -> str:
    """Identify an exact reviewed configuration, not prove its physical correctness."""
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def operation_from_dict(data: dict) -> Operation:
    """Decode only the explicit operation contract."""
    if set(data) != {"schema", "run_id", "operation_id", "device_id", "action", "parameters"}:
        raise ValueError("Invalid operation envelope")
    return Operation(**data)


def observation_from_dict(data: dict) -> Observation:
    """Restore facts with their evidence, units and coordinate frames."""
    if set(data) != {"source", "facts", "revision", "observed_at"}:
        raise ValueError("Invalid observation envelope")
    return Observation(
        data["source"],
        {k: Fact(**{**v, "evidence": Evidence(v["evidence"])}) for k, v in data["facts"].items()},
        data["revision"],
        data["observed_at"],
    )


def outcome_from_dict(data: dict) -> Outcome:
    """Restore backend completion evidence."""
    if set(data) != {"status", "observation", "error"}:
        raise ValueError("Invalid outcome envelope")
    return Outcome(
        Status(data["status"]),
        observation_from_dict(data["observation"]) if data["observation"] is not None else None,
        data["error"],
    )
