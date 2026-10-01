"""Application-owned outbound gateway mailbox. No network policy in the mapping core."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
import json
from pathlib import Path
import secrets
import threading
import time
import uuid

from .instruments import validate_plan, validate_profile
from .application_records import qualified_profile
from .gateway_credentials import credential_digest, read_registry
from .wire import fingerprint, plain, observation_from_dict, outcome_from_dict


def private_secret(path):
    path = Path(path)
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Credential file must be private (chmod 600): {path}")
    value = path.read_text().strip()
    if len(value) < 24 or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("Credential must contain at least 24 ASCII characters without whitespace")
    return value


class GatewayBroker:
    """One outbound session per configured device; delivered requests are never retried.

    All mailbox mutations use one lock. HTTP workers poll this mailbox while the
    console's asyncio loop awaits futures. A missing response latches uncertainty
    across restarts until an operator explicitly records reconciliation.
    """

    def __init__(self, entries=(), records=None):
        self.condition = threading.Condition()
        self.devices = {}
        self.closed = False
        self.records = records
        for entry in entries:
            profile = validate_profile(json.loads(Path(entry["profile_file"]).read_text()), hardware=True)
            key = profile["device_id"]
            if key in self.devices:
                raise ValueError("Duplicate gateway device ID")
            self.devices[key] = dict(
                profile=profile,
                name=entry.get("name", key),
                credentials=entry,
                credential=None,
                session=None,
                seen=0,
                pending={},
                fault=records.device(key)["fault"] if records else "",
                inspected=False,
            )
            read_registry(entry)

    def authorize(self, key, bearer, gateway_id="default"):
        with self.condition:
            item = self.devices.get(key)
            if item is None:
                raise ValueError("Gateway credential rejected")
            self._credential_current(key)
            registry = read_registry(item["credentials"])
            expected = registry.get(gateway_id)
            if (
                not bearer.startswith("Bearer ")
                or not expected
                or not secrets.compare_digest(credential_digest(bearer[7:]), expected)
            ):
                raise ValueError("Gateway credential rejected")
            return (gateway_id, expected)

    def hold(self, key, reason):
        with self.condition:
            self.devices[key]["fault"] = reason
            self.devices[key]["inspected"] = False
            if self.records:
                self.records.hold(key, reason)

    def _credential_current(self, key):
        d = self.devices[key]
        credential = d["credential"]
        if credential and read_registry(d["credentials"]).get(credential[0]) != credential[1]:
            d["session"], d["seen"], d["credential"] = None, 0, None
            self.hold(key, "Gateway credential replaced/revoked. Stop the old gateway and reconcile before a new run.")
            for item in d["pending"].values():
                if not item["future"].done():
                    item["future"].set_exception(RuntimeError(d["fault"]))
            self.condition.notify_all()

    def reconcile(self, key, note):
        with self.condition:
            d = self.devices[key]
            if d["pending"] or not d["inspected"]:
                raise ValueError("Wait for pending commands and Check connector before recording reconciliation")
            if self.records:
                self.records.reconcile(key, note)
            d["fault"] = ""

    def catalog(self):
        with self.condition:
            for key in self.devices:
                self._credential_current(key)
            return [
                dict(
                    id=key,
                    name=d["name"],
                    profile=d["profile"],
                    online=bool(d["session"]) and time.monotonic() - d["seen"] < 35,
                    fault=d["fault"],
                )
                for key, d in self.devices.items()
            ]

    def match(self, profile):
        item = self.devices.get(profile["device_id"])
        if item is None or fingerprint(profile) != fingerprint(item["profile"]):
            raise ValueError("Select the configured device profile; browser binding edits are not accepted")
        return profile["device_id"]

    def register(self, key, profile, credential=None):
        validate_profile(profile, hardware=True)
        with self.condition:
            d = self.devices[key]
            self._credential_current(key)
            if fingerprint(qualified_profile(profile)) != fingerprint(qualified_profile(d["profile"])):
                raise ValueError("Gateway device/model/calibration profile differs from the configured profile")
            if profile["device_id"] != key:
                raise ValueError("Credential belongs to another device")
            if d["session"] and time.monotonic() - d["seen"] < 35:
                raise ValueError("A gateway session is already online")
            if d["session"]:
                self.hold(key, "Previous gateway disconnected. Verify its commands have ended before reconciliation.")
            if d["pending"]:
                raise ValueError("Wait for the interrupted request; reconcile before a new run")
            if credential and read_registry(d["credentials"]).get(credential[0]) != credential[1]:
                raise ValueError("Gateway credential was revoked")
            d["session"] = secrets.token_urlsafe(32)
            d["credential"] = credential
            d["inspected"] = False
            d["seen"] = time.monotonic()
            return {"session": d["session"]}

    def _session(self, key, session, credential=None):
        self._credential_current(key)
        d = self.devices[key]
        if self.closed or not session or not secrets.compare_digest(session, d["session"] or ""):
            raise ValueError("Gateway session is no longer active")
        if credential is not None and credential != d["credential"]:
            raise ValueError("Gateway session belongs to another installation")
        return d

    def poll(self, key, session, wait=15, credential=None):
        with self.condition:
            d = self._session(key, session, credential)
            d["seen"] = time.monotonic()
            deadline = time.monotonic() + wait
            while not self.closed:
                for item in d["pending"].values():
                    if not item["delivered"] and not item["future"].done():
                        item["delivered"] = True  # Lost HTTP response => UNKNOWN, never requeue.
                        return item["command"]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(min(remaining, 1))
                self._session(key, session, credential)
            d["seen"] = time.monotonic()
            return None

    def result(self, key, session, data, credential=None):
        with self.condition:
            d = self._session(key, session, credential)
            item = d["pending"].get(data["id"])
            if not item or not item["delivered"] or item["future"].done():
                raise ValueError("Result is late, duplicate or unknown; no operation will be replayed")
            if set(data) != {"id", "value", "error"}:
                raise ValueError("Invalid gateway result")
            if self.records:
                self.records.result(key, item["command"], data)
            if item["command"]["kind"] == "inspect" and not data["error"]:
                value = data["value"]
                if (
                    not isinstance(value, dict)
                    or value.get("server_uuid") != d["profile"]["real"]["server_uuid"]
                    or value.get("is_simulating") is not False
                ):
                    self.hold(key, "Connector identity or physical mode differs; reconciliation rejected")
                    item["future"].set_exception(ValueError(d["fault"]))
                    return
                d["inspected"] = True
            if data["error"]:
                if item["command"]["kind"] != "inspect":
                    self.hold(
                        key, "Gateway command failed; verify the physical outcome and reconcile before a new run."
                    )
                item["future"].set_exception(RuntimeError(str(data["error"])))
            else:
                item["future"].set_result(data["value"])

    async def request(self, key, kind, payload=None, *, timeout=120):
        future = Future()
        command_id = str(uuid.uuid4())
        with self.condition:
            d = self.devices[key]
            self._credential_current(key)
            if self.closed or not d["session"] or time.monotonic() - d["seen"] >= 35:
                raise RuntimeError("Device gateway is offline; start it on the device-side computer")
            if d["fault"] and kind not in ("stop", "inspect", "finish", "reconcile"):
                raise RuntimeError(d["fault"])
            d["pending"][command_id] = dict(
                future=future,
                delivered=False,
                command={
                    "id": command_id,
                    "kind": kind,
                    "payload": payload,
                    "expires_at": time.time() + timeout,
                },
            )
            if self.records:
                try:
                    self.records.intent(key, d["pending"][command_id]["command"])
                except BaseException:
                    d["pending"].pop(command_id)
                    raise
            self.condition.notify_all()
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            with self.condition:
                self.hold(key, "Gateway response missing: physical outcome unknown. Reconcile before a new run.")
            raise
        finally:
            with self.condition:
                d["pending"].pop(command_id, None)

    def close(self):
        with self.condition:
            self.closed = True
            for d in self.devices.values():
                for item in d["pending"].values():
                    if not item["future"].done():
                        item["future"].set_exception(RuntimeError("Application closed; physical stop not confirmed"))
            self.condition.notify_all()


class GatewayAdapter:
    """Normalized operation/observation adapter; no SiLA dependency at the hub."""

    def __init__(self, broker, plan):
        self.broker, self.plan = broker, validate_plan(plan)
        self.key = broker.match(plan["profile"])

    async def begin(self):
        await self.broker.request(self.key, "begin", self.plan)

    async def snapshot(self, revision):
        return observation_from_dict(await self.broker.request(self.key, "snapshot", revision))

    def prepare(self, op):
        if plain(op) not in self.plan["operations"]:
            raise ValueError("Operation was not reviewed")

        async def execute():
            return outcome_from_dict(await self.broker.request(self.key, "execute", plain(op)))

        return execute

    async def finish(self):
        await self.broker.request(self.key, "finish", self.plan["binding"], timeout=20)
