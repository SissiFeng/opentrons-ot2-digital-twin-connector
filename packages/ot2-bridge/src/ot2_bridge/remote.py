"""Optional localhost JSON-lines transport to a persistent simulation runtime.

Use SSH forwarding between machines. This endpoint never controls hardware.
A prepared request reserves the runtime before the orchestrator dispatches real.
"""

from __future__ import annotations

import asyncio
import json
import time
import secrets
from dataclasses import dataclass

from .models import Operation, Outcome, Status
from .wire import dumps, observation_from_dict, operation_from_dict, outcome_from_dict, plain

LIMIT = 1024 * 1024


@dataclass
class Reservation:
    operation: Operation
    call: object
    token: str


class SimulationService:
    """Single-session runtime binding with explicit preflight and no retry-on-error."""

    def __init__(self, adapter, *, binding: dict, initial_observation, timeout: float = 60) -> None:
        self.adapter, self.binding = adapter, plain(binding)
        self.initial_observation, self.timeout = initial_observation, timeout
        self.pending = None
        self.running = False
        self.seen = set()
        self.fault = ""
        self.session_token = None

    async def dispatch(self, request: dict):
        """Process one bounded protocol request; only execute has simulation effects."""
        method = request.get("method")
        if method == "hello":
            if set(request) != {"method", "binding"} or request["binding"] != self.binding:
                raise ValueError("Session configuration mismatch")
            ready = not (self.session_token or self.pending or self.running or self.seen or self.fault)
            if ready:
                self.session_token = secrets.token_urlsafe(24)
            return {
                "binding": self.binding,
                "initial_observation": self.initial_observation,
                "ready": ready,
                "session": self.session_token if ready else None,
            }
        if self.session_token is None or not secrets.compare_digest(
            str(request.get("session", "")), self.session_token
        ):
            raise ValueError("Session handshake required")
        if method == "prepare":
            if set(request) != {"method", "operation", "session"}:
                raise ValueError("Invalid prepare envelope")
            if self.pending or self.running or self.fault:
                raise RuntimeError(f"Runtime busy or requires explicit recovery: {self.fault}")
            operation = operation_from_dict(request["operation"])
            if (operation.run_id, operation.device_id) != (self.binding["run_id"], self.binding["device_id"]):
                raise ValueError("Operation run/device does not match session binding")
            key = (operation.run_id, operation.operation_id)
            if key in self.seen:
                raise ValueError("Operation already dispatched; automatic replay forbidden")
            call = self.adapter.prepare(operation)
            token = secrets.token_urlsafe(24)
            self.pending = Reservation(operation, call, token)
            return {"token": token}
        if method not in ("execute", "discard") or set(request) != {"method", "token", "session"}:
            raise ValueError("Unknown request")
        reservation = self.pending
        if reservation is None or not secrets.compare_digest(str(request["token"]), reservation.token):
            raise ValueError("No matching reservation")
        self.pending = None
        if method == "discard":
            return {"discarded": True}
        self.running = True
        self.seen.add((reservation.operation.run_id, reservation.operation.operation_id))
        try:
            deadline = time.monotonic() + self.timeout
            outcome = await asyncio.wait_for(reservation.call(), self.timeout)
            if time.monotonic() > deadline:
                outcome = Outcome(Status.UNKNOWN, error="Runtime completed after deadline")
            if not isinstance(outcome, Outcome):
                raise TypeError("Runtime must return Outcome")
        except (Exception, asyncio.CancelledError) as error:
            outcome = Outcome(Status.UNKNOWN, error=f"{type(error).__name__}: {error}")
        finally:
            self.running = False
        if outcome.status is not Status.SUCCEEDED:
            self.fault = outcome.error or outcome.status.value
        return outcome

    async def handle(self, reader, writer) -> None:
        """Serve one request per connection with bounded input and idle time."""
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if not line or len(line) > LIMIT:
                raise ValueError("Missing or oversized request")
            result = await self.dispatch(json.loads(line))
            reply = {"result": result}
        except Exception as error:
            reply = {"error": f"{type(error).__name__}: {error}"}
        try:
            writer.write((dumps(reply) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def listen(self, port: int = 8765):
        """Bind loopback only; remote access uses an authenticated SSH tunnel."""
        return await asyncio.start_server(self.handle, "127.0.0.1", port, limit=LIMIT)


class RemoteSimulation:
    """Async staging keeps network preflight outside synchronous Adapter.prepare."""

    def __init__(self, port: int = 8765, *, timeout: float = 65) -> None:
        self.port, self.timeout = port, timeout
        self.ready = False
        self.session = None
        self.last_observation = None

    async def request(self, payload):
        """One request, no implicit retries after uncertain effects."""

        async def exchange():
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port, limit=LIMIT)
            try:
                writer.write(
                    (
                        dumps(payload if payload.get("method") == "hello" else {**payload, "session": self.session})
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                response = json.loads(await reader.readline())
                if "error" in response:
                    raise RuntimeError(response["error"])
                return response["result"]
            finally:
                writer.close()
                await writer.wait_closed()

        return await asyncio.wait_for(exchange(), self.timeout)

    async def connect(self, binding: dict):
        """Verify exact session binding before any operation is dispatched."""
        result = await self.request({"method": "hello", "binding": binding})
        if not result["ready"]:
            raise RuntimeError("Runtime already used or busy; inspect/recover explicitly")
        self.session = result["session"]
        self.ready = True
        self.last_observation = observation_from_dict(result["initial_observation"])
        return result["initial_observation"]

    async def stage(self, operation: Operation) -> StagedSimulation:
        """Validate and reserve one native sequence before starting the real call."""
        if not self.ready:
            raise RuntimeError("Connect and validate session first")
        response = await self.request({"method": "prepare", "operation": plain(operation)})
        return StagedSimulation(self, operation, response["token"])


class StagedSimulation:
    """Single-use reservation; discard explicitly if real dispatch is abandoned."""

    def __init__(self, remote: RemoteSimulation, operation: Operation, token: str):
        self.remote, self.operation, self.token = remote, operation, token
        self.used = False

    def prepare(self, candidate):
        """Validate the exact staged operation without starting either backend."""
        if candidate != self.operation or self.used:
            raise ValueError("Staged operation mismatch or already consumed")

        async def execute():
            if self.used:
                raise ValueError("Staged call already consumed")
            self.used = True
            self.remote.last_observation = None
            outcome = outcome_from_dict(await self.remote.request({"method": "execute", "token": self.token}))
            if outcome.status is Status.SUCCEEDED:
                self.remote.last_observation = outcome.observation
            return outcome

        return execute

    async def discard(self):
        """Release an unexecuted reservation; never cancel a running backend."""
        if self.used:
            raise ValueError("Staged call already consumed")
        self.used = True
        await self.remote.request({"method": "discard", "token": self.token})
