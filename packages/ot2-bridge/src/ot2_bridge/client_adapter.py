"""Semantic adapter for AccelerationConsortium/opentrons-sila-clients.

No SiLA import is needed: HTTP or other clients can implement the same methods.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone

from .mirror import Call
from .models import Evidence, Fact, Observation, Operation, Outcome, Status

CLIENT_SCHEMA = "ot2.client/1"
FIELDS = {
    "home": {"axes"},
    "move_to_well": {"mount", "channels", "slot", "well", "origin", "offset", "tip_length_mm", "speed"},
    "pick_up_tip": {"mount", "channels", "slot", "well", "offset", "tip_length_mm", "speed"},
    "drop_tip": {"mount", "channels", "tiprack_diameter_mm", "speed"},
    "aspirate": {"mount", "channels", "volume_ul", "flow_rate_ul_s"},
    "dispense": {"mount", "channels", "volume_ul", "flow_rate_ul_s", "empty_after"},
}


def validate_client_operation(operation: Operation, device_id: str, mounts: Mapping[str, int]) -> dict:
    """Reject unknown fields and unsupported mount/channel semantics before dispatch."""
    if operation.schema != CLIENT_SCHEMA or operation.device_id != device_id:
        raise ValueError("Client schema/device binding mismatch")
    p = dict(operation.parameters)
    if operation.action not in FIELDS or set(p) != FIELDS[operation.action]:
        raise ValueError(f"{operation.action}: expected fields {sorted(FIELDS.get(operation.action, []))}")
    if operation.action == "home":
        axes = p["axes"]
        if not isinstance(axes, str) or not axes or len(set(axes)) != len(axes) or set(axes) - set("XYZABC"):
            raise ValueError("axes must contain unique XYZABC letters")
        return p
    if p["mount"] not in mounts or type(p["channels"]) is not int or p["channels"] != mounts[p["mount"]]:
        raise ValueError("Unsupported mount/channel binding")
    for key in ("volume_ul", "flow_rate_ul_s", "tip_length_mm", "tiprack_diameter_mm", "speed"):
        if key in p:
            value = p[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be finite numeric")
            bare_nozzle = key == "tip_length_mm" and operation.action == "move_to_well" and value == 0
            if (
                value < 0 or (key in ("volume_ul", "flow_rate_ul_s", "tip_length_mm") and value == 0)
            ) and not bare_nozzle:
                raise ValueError(f"Invalid {key}")
    if "offset" in p:
        if not isinstance(p["offset"], (list, tuple)) or len(p["offset"]) != 3:
            raise ValueError("offset must contain three millimetre coordinates")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p["offset"]):
            raise ValueError("offset must be finite numeric")
    for key in ("slot", "well"):
        if key in p and (not isinstance(p[key], str) or not p[key].strip()):
            raise ValueError(f"{key} must be a non-empty string")
    if "origin" in p and p["origin"] not in ("top", "bottom", "center"):
        raise ValueError("Unsupported well origin")
    if "empty_after" in p and type(p["empty_after"]) is not bool:
        raise ValueError("empty_after must be boolean")
    return p


class OT2ClientAdapter:
    """Call upstream semantic methods and then obtain fresh observations.

    Observe is explicit because the upstream client exposes no public position
    getter or liquid ledger. The integration owner supplies its MotionCommands
    readback or other state owner. Command return values are never observations.
    """

    def __init__(
        self,
        device_id: str,
        client: object,
        observe: Callable[[Operation], Awaitable[Observation]],
        *,
        mounts: Mapping[str, int],
    ) -> None:
        self.device_id, self.client, self.observe = device_id, client, observe
        self.mounts = dict(mounts)
        if not self.mounts or any(
            k not in ("LEFT", "RIGHT") or type(v) is not int or v not in (1, 8) for k, v in self.mounts.items()
        ):
            raise ValueError("Explicit LEFT/RIGHT mount and 1/8 channel bindings required")

    async def snapshot(self, operation: Operation) -> Observation:
        """Read the external state owner before validation and again before dispatch."""
        return await self.observe(operation)

    def prepare(self, operation: Operation) -> Call:
        """Map one whole semantic call; preserve pickup, arc and empty-after behavior."""
        kwargs = validate_client_operation(operation, self.device_id, self.mounts)
        method = getattr(self.client, operation.action)
        if "mount" in kwargs:
            kwargs["mount"] = kwargs["mount"].lower()
            del kwargs["channels"]

        async def execute() -> Outcome:
            await method(**kwargs)
            try:
                observation = await self.observe(operation)
            except Exception as error:
                # The command completed; readback failure is not command failure.
                return Outcome(Status.SUCCEEDED, error=f"Observation unavailable: {type(error).__name__}: {error}")
            return Outcome(Status.SUCCEEDED, observation)

        return execute


def position_observation(position: Mapping[str, float], *, source: str, revision: str = "") -> Observation:
    """Normalize firmware-reported machine axes, without claiming encoder measurement."""
    if not position or set(position) - set("XYZABC"):
        raise ValueError("Expected firmware machine-axis positions")
    facts = {}
    for axis, value in position.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Axis positions must be finite numeric")
        facts[f"axis.{axis}"] = Fact(value, Evidence.HARDWARE, "mm", "ot2.machine")
    return Observation(source, facts, revision, datetime.now(timezone.utc).isoformat())
