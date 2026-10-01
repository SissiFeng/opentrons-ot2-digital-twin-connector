"""Device-side agent: outbound HTTP only, local configured backend, no arbitrary commands."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from pathlib import Path
import time

from .flex_cli import connected, control, read_profile
from .flex import FlexAdapter
from .ot2_console import OT2HomeAdapter
from .flex_session import device_session
from .gateway import private_secret
from .instruments import is_ot2, validate_plan, validate_profile
from .network import hub_endpoint, hub_json
from .application_records import qualified_profile
from .wire import fingerprint, operation_from_dict, plain


class SiLADeviceBackend:
    """Replace with a locally installed provider implementing the same methods."""

    def __init__(self, profile):
        self.profile = profile
        self.transport = self.adapter = None
        self.stopped = False

    async def inspect(self):
        return await control(self.profile, "inspect")

    async def begin(self, plan):
        self.stopped = False
        self.transport = await connected(self.profile)
        cls = OT2HomeAdapter if is_ot2(self.profile) else FlexAdapter
        self.adapter = cls(self.profile, self.transport, hardware=True, stop_requested=lambda: self.stopped)
        for op in plan["operations"]:
            self.adapter.prepare(operation_from_dict(op))

    async def snapshot(self, revision):
        return await self.adapter.snapshot(revision)

    async def execute(self, op):
        return await self.adapter.prepare(op)()

    async def stop(self):
        self.stopped = True
        return await control(self.profile, "stop")

    async def close(self):
        if self.transport:
            await self.transport.close()
        self.transport = self.adapter = None


class DeviceExecutor:
    """Gateway-owned exclusion/attempt ledger, not physical state authority."""

    def __init__(self, profile, backend, ledger):
        self.profile = validate_profile(profile, hardware=True)
        self.backend, self.ledger = backend, Path(ledger)
        self.ledger.mkdir(parents=True, exist_ok=True)
        self.plan = self.guard = None
        self.next_operation = 0
        self.lock = asyncio.Lock()
        self.fault = False
        self.stop_requested = False

    async def dispatch(self, command):
        if set(command) != {"id", "kind", "payload", "expires_at"}:
            raise ValueError("Invalid command envelope")
        # Durable attempt BEFORE action; lost responses cannot authorize replay.
        path = self.ledger / (fingerprint(command["id"]) + ".attempt")
        with path.open("x") as file:
            file.write(fingerprint(command))
            file.flush()
            os.fsync(file.fileno())
        directory_fd = os.open(self.ledger, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if (
            not isinstance(command["expires_at"], (int, float))
            or not time.time() < command["expires_at"] <= time.time() + 180
        ):
            raise ValueError("Expired command or clock mismatch; synchronize both host clocks")
        kind, payload = command["kind"], command["payload"]
        if kind == "stop":
            self.stop_requested = True
            return await self.backend.stop()
        async with self.lock:
            if time.time() >= command["expires_at"]:
                raise ValueError("Command expired while waiting; no device command dispatched")
            if kind == "inspect":
                return await self.backend.inspect()
            if kind == "reconcile":
                if self.plan:
                    raise ValueError(
                        "Gateway still owns an active run; wait for completion and restart it before reconciliation"
                    )
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("note"), str)
                    or len(payload["note"].strip()) < 8
                ):
                    raise ValueError("Operator reconciliation note required")
                self.fault = self.stop_requested = False
                return {"recorded": True, "physical_state_authority": "operator and connector"}
            if kind == "begin":
                if self.plan or self.fault:
                    raise ValueError("Reconcile the active/uncertain gateway run before restarting")
                plan = validate_plan(payload)
                if fingerprint(qualified_profile(plan["profile"])) != fingerprint(qualified_profile(self.profile)):
                    raise ValueError("Reviewed profile differs from the gateway's local configuration")
                self.guard = device_session(self.profile, run_id=plan["binding"]["run_id"])
                self.guard.__enter__()
                self.plan, self.next_operation, self.stop_requested = plan, 0, False
                try:
                    await self.backend.begin(plan)
                except BaseException:
                    self.fault = True
                    await self.close()
                    raise
                return {"ready": True}
            if kind == "finish":
                if not self.plan or payload != self.plan["binding"]:
                    raise ValueError("Run binding differs")
                await self.close()
                return {"closed": True}
            if not self.plan or self.fault or self.stop_requested:
                raise ValueError("No active run or execution held; no device command dispatched")
            if kind == "snapshot":
                return plain(await self.backend.snapshot(payload))
            if kind == "execute":
                expected = self.plan["operations"][self.next_operation : self.next_operation + 1]
                if expected != [payload]:
                    raise ValueError("Operation is duplicated or outside the reviewed sequence")
                self.next_operation += 1
                try:
                    outcome = await self.backend.execute(operation_from_dict(payload))
                    if outcome.status.value != "succeeded":
                        self.fault = True
                    return plain(outcome)
                except BaseException:
                    self.fault = True
                    raise
            raise ValueError("Unknown gateway command")

    async def close(self):
        try:
            await self.backend.close()
        finally:
            if self.guard:
                self.guard.__exit__(None, None, None)
            self.guard = self.plan = None


def hub_url(value):
    return hub_endpoint(value)[0]


async def serve_gateway(hub, profile, secret, executor, gateway_id="default"):

    async def post(action, body):
        def send():
            return hub_json(
                hub,
                "/gateway/" + action,
                body,
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + secret,
                    "X-Device-ID": profile["device_id"],
                    "X-Gateway-ID": gateway_id,
                },
            )

        return await asyncio.to_thread(send)

    session = (await post("register", {"profile": profile}))["session"]
    print(f"Gateway online: {profile['device_id']} → {hub}. No motion requested.", flush=True)
    tasks = set()
    failed = asyncio.Event()

    async def execute(command):
        result = {"id": command["id"], "value": None, "error": ""}
        try:
            result["value"] = plain(await executor.dispatch(command))
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        try:
            await post("result", {"session": session, "result": result})
        except Exception:
            failed.set()  # Never repeat a physical action to recover its response.

    try:
        while not failed.is_set():
            command = (await post("poll", {"session": session}))["command"]
            if command:
                if len(tasks) >= 4:
                    raise RuntimeError("Too many pending gateway requests")
                task = asyncio.create_task(execute(command))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        raise RuntimeError("Result delivery failed; physical outcome uncertain. Reconcile before restarting.")
    finally:
        # Await the actual in-flight response before releasing the local lock.
        # Disconnecting HTTP is not an emergency stop of the device.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await executor.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--gateway-id", default="default", help="Installation ID assigned by the Hub operator")
    parser.add_argument("--ledger", default=str(Path.home() / ".local/state/matterix-gateway"))
    parser.add_argument(
        "--provider", default="sila", help="Local module:factory(profile) implementing the backend contract"
    )
    args = parser.parse_args()
    profile = validate_profile(read_profile(args.profile), hardware=True)
    secret, hub = private_secret(args.token_file), hub_url(args.hub)

    async def run():
        if args.provider == "sila":
            backend = SiLADeviceBackend(profile)
        else:
            module, name = args.provider.split(":", 1)
            backend = getattr(importlib.import_module(module), name)(profile)
        executor = DeviceExecutor(profile, backend, args.ledger)
        await serve_gateway(hub, profile, secret, executor, args.gateway_id)

    try:
        asyncio.run(run())
    except (Exception, KeyboardInterrupt) as error:
        parser.exit(2, f"Gateway stopped: {type(error).__name__}: {error}. Physical stop is not confirmed.\n")


if __name__ == "__main__":
    main()
