"""Fail-closed bridge execution with identity preflight and append-only audit."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from .adapter import OT2WorkflowAdapter
from .models import EndpointKind, ExecutionRecord, OT2Command, PreflightExpectation, PreflightReport, WorkflowStep


class BridgeTransport(Protocol):
    """Transport subset required by the executor."""

    async def execute(self, command: OT2Command) -> object:
        """Execute one contract-validated invocation."""
        ...


class OT2PreflightError(RuntimeError):
    """Connector identity, calibration, or pipette evidence failed closed."""


class OT2StateReconciliationError(RuntimeError):
    """A side effect completed without a monotonic state revision."""


class OT2BridgeExecutor:
    """Gate and execute OT-2 side effects without automatic retries."""

    def __init__(
        self,
        transport: BridgeTransport,
        adapter: OT2WorkflowAdapter,
        audit_path: str | Path,
    ) -> None:
        self._transport = transport
        self._adapter = adapter
        self._audit_path = Path(audit_path)
        self._preflight: PreflightReport | None = None
        self._lock = asyncio.Lock()

    @property
    def preflight_report(self) -> PreflightReport | None:
        """Return the currently active preflight evidence."""
        return self._preflight

    async def preflight(self, expected: PreflightExpectation) -> PreflightReport:
        """Pin identity, calibration, pipettes, and state before side effects."""
        async with self._lock:
            self._preflight = None
            device = _mapping(await self._read("DeviceInformationProvider", "DeviceInformation"))
            configuration = _mapping(await self._read("DeckConfigurationProvider", "ConfigurationInformation"))
            pipettes = _sequence(await self._read("PipetteController", "Pipettes"))
            state = _mapping(await self._read("RobotStateProvider", "CurrentState"))
            errors: list[str] = []
            _expect(errors, "contract_id", device, expected.contract_id)
            _expect(errors, "config_id", device, expected.config_id)
            _expect(errors, "configuration.config_id", configuration, expected.config_id, key="config_id")
            _expect(errors, "calibration_id", configuration, expected.calibration_id)
            _expect(errors, "robot_serial_number", device, expected.robot_serial_number)
            if configuration.get("calibration_confirmed") is not True:
                errors.append("calibration_confirmed is not true")
            actual_models: dict[str, str] = {}
            for item_value in pipettes:
                item = _mapping(item_value)
                mount = _enum_text(item.get("mount"))
                if not mount:
                    errors.append("pipette record is missing mount")
                    continue
                actual_model = str(item.get("actual_model", ""))
                actual_models[mount] = actual_model
                if item.get("attached") is not True:
                    errors.append(f"{mount} pipette is not attached")
                if item.get("configuration_matches") is not True:
                    errors.append(f"{mount} pipette does not match configured identity")
                expected_model = expected.pipette_models.get(mount)
                if expected_model is None:
                    errors.append(f"unexpected configured pipette mount {mount}")
                elif actual_model != expected_model:
                    errors.append(f"{mount} pipette model {actual_model!r} does not match expected {expected_model!r}")
            missing_mounts = sorted(set(expected.pipette_models) - set(actual_models))
            errors.extend(f"expected pipette mount {mount} is missing" for mount in missing_mounts)
            revision = _integer(state, "revision")
            simulation = _boolean(state, "simulation")
            if errors:
                raise OT2PreflightError("; ".join(errors))
            report = PreflightReport.create(
                contract_id=expected.contract_id,
                config_id=expected.config_id,
                calibration_id=expected.calibration_id,
                robot_serial_number=expected.robot_serial_number,
                state_revision=revision,
                simulation=simulation,
                pipette_models=actual_models,
            )
            self._preflight = report
            return report

    async def execute_step(self, step: WorkflowStep) -> object:
        """Adapt and execute one step, returning ``None`` for non-OT-2/setup steps."""
        command = self._adapter.adapt(step)
        if command is None:
            return None
        return await self.execute(command)

    async def execute(self, command: OT2Command) -> object:
        """Execute one invocation and audit every side effect exactly once."""
        if not command.side_effecting:
            return await self._transport.execute(command)
        async with self._lock:
            report = self._preflight
            if report is None:
                msg = "Successful preflight is required before OT-2 side effects"
                raise OT2PreflightError(msg)
            state_before = await self._current_state()
            response: object = None
            state_after: object = None
            error_text = ""
            try:
                response = await self._transport.execute(command)
                state_after = await self._current_state()
                before_revision = _integer(_mapping(state_before), "revision")
                after_revision = _integer(_mapping(state_after), "revision")
                if after_revision <= before_revision:
                    msg = (
                        f"State revision did not advance after {command.action}: "
                        f"before={before_revision}, after={after_revision}"
                    )
                    raise OT2StateReconciliationError(msg)
            except Exception as error:
                error_text = f"{type(error).__name__}: {error}"
                if state_after is None:
                    try:
                        state_after = await self._current_state()
                    except Exception as state_error:  # noqa: BLE001 - preserve the original side-effect failure
                        state_after = {"state_read_error": f"{type(state_error).__name__}: {state_error}"}
                self._append_record(
                    ExecutionRecord.create(
                        command=command,
                        contract_id=report.contract_id,
                        config_id=report.config_id,
                        state_before=state_before,
                        state_after=state_after,
                        response=response,
                        error=error_text,
                    )
                )
                raise
            self._append_record(
                ExecutionRecord.create(
                    command=command,
                    contract_id=report.contract_id,
                    config_id=report.config_id,
                    state_before=state_before,
                    state_after=state_after,
                    response=response,
                    error="",
                )
            )
            return response

    async def _read(self, feature: str, endpoint: str) -> object:
        command = self._adapter.registry.command(
            step_id=f"__preflight_{feature}_{endpoint}__",
            action="bridge.preflight",
            feature_identifier=feature,
            endpoint=endpoint,
            parameters={},
            kind=EndpointKind.PROPERTY,
            side_effecting=False,
        )
        return await self._transport.execute(command)

    async def _current_state(self) -> object:
        return await self._read("RobotStateProvider", "CurrentState")

    def _append_record(self, record: ExecutionRecord) -> None:
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_mapping(), sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"Expected structured connector response, got {type(value).__name__}"
        raise OT2PreflightError(msg)
    return value


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        msg = f"Expected connector list response, got {type(value).__name__}"
        raise OT2PreflightError(msg)
    return value


def _enum_text(value: object) -> str:
    if isinstance(value, str):
        return value.rsplit(".", maxsplit=1)[-1].upper()
    return str(value or "").rsplit(".", maxsplit=1)[-1].upper()


def _expect(
    errors: list[str],
    label: str,
    actual: Mapping[str, object],
    expected: str,
    *,
    key: str | None = None,
) -> None:
    actual_value = actual.get(key or label)
    if actual_value != expected:
        errors.append(f"{label} {actual_value!r} does not match expected {expected!r}")


def _integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int):
        msg = f"Connector field {key!r} is not an integer"
        raise OT2PreflightError(msg)
    return result


def _boolean(value: Mapping[str, object], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        msg = f"Connector field {key!r} is not boolean"
        raise OT2PreflightError(msg)
    return result
