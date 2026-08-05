"""Typed bridge inputs, commands, preflight results, and audit records."""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime


class EndpointKind(str, enum.Enum):
    """SiLA endpoint family."""

    COMMAND = "COMMAND"
    PROPERTY = "PROPERTY"


class ExecutionMode(str, enum.Enum):
    """Invocation behavior declared by the serialized SiLA FDL."""

    OBSERVABLE = "OBSERVABLE"
    UNOBSERVABLE = "UNOBSERVABLE"


@dataclasses.dataclass(frozen=True)
class WorkflowStep:
    """One external workflow step with its source shape preserved."""

    step_id: str
    action: str
    params: dict[str, object]
    description: str
    phase_name: str
    thread_name: str | None = None

    @property
    def device(self) -> str:
        """Return the action prefix used for routing."""
        return self.action.partition(".")[0]


@dataclasses.dataclass(frozen=True)
class OT2Command:
    """One contract-resolved OT-2 invocation."""

    step_id: str
    action: str
    feature_identifier: str
    endpoint: str
    parameters: dict[str, object]
    endpoint_kind: EndpointKind
    execution_mode: ExecutionMode
    side_effecting: bool

    @property
    def request_hash(self) -> str:
        """Return the stable audit identity of this invocation."""
        payload = {
            "action": self.action,
            "endpoint": self.endpoint,
            "endpoint_kind": self.endpoint_kind.value,
            "execution_mode": self.execution_mode.value,
            "feature_identifier": self.feature_identifier,
            "parameters": _canonical(self.parameters),
            "side_effecting": self.side_effecting,
            "step_id": self.step_id,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class PreflightExpectation:
    """Identity pins that must match before the bridge may issue side effects."""

    contract_id: str
    config_id: str
    calibration_id: str
    robot_serial_number: str
    pipette_models: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class PreflightReport:
    """Evidence captured by a successful startup gate."""

    checked_at: str
    contract_id: str
    config_id: str
    calibration_id: str
    robot_serial_number: str
    state_revision: int
    simulation: bool
    pipette_models: dict[str, str]

    @classmethod
    def create(
        cls,
        *,
        contract_id: str,
        config_id: str,
        calibration_id: str,
        robot_serial_number: str,
        state_revision: int,
        simulation: bool,
        pipette_models: dict[str, str],
    ) -> PreflightReport:
        """Create a UTC-timestamped report."""
        return cls(
            checked_at=datetime.now(UTC).isoformat(),
            contract_id=contract_id,
            config_id=config_id,
            calibration_id=calibration_id,
            robot_serial_number=robot_serial_number,
            state_revision=state_revision,
            simulation=simulation,
            pipette_models=pipette_models,
        )


@dataclasses.dataclass(frozen=True)
class ExecutionRecord:
    """Append-only record for one attempted side effect."""

    recorded_at: str
    step_id: str
    action: str
    request_hash: str
    contract_id: str
    config_id: str
    state_before: object
    state_after: object
    response: object
    error: str

    @classmethod
    def create(
        cls,
        *,
        command: OT2Command,
        contract_id: str,
        config_id: str,
        state_before: object,
        state_after: object,
        response: object,
        error: str,
    ) -> ExecutionRecord:
        """Create a UTC-timestamped audit record."""
        return cls(
            recorded_at=datetime.now(UTC).isoformat(),
            step_id=command.step_id,
            action=command.action,
            request_hash=command.request_hash,
            contract_id=contract_id,
            config_id=config_id,
            state_before=state_before,
            state_after=state_after,
            response=response,
            error=error,
        )

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible record."""
        return _canonical(dataclasses.asdict(self))


def _canonical(value: object) -> object:
    """Normalize bridge values for deterministic JSON and audit output."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonical(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return _canonical(value.value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
