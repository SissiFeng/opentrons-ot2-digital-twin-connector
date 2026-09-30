"""Explicit synthetic backends for laptop integration tests; no hardware fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .client_adapter import validate_client_operation
from .models import Evidence, Fact, Observation, Outcome, Status


class DemoBackend:
    """Deterministic state machine for exercising the bridge, not physical simulation."""

    def __init__(self, device_id="ot2", *, source, drift=0.0, fail_action=""):
        self.device_id, self.source, self.drift, self.fail_action = device_id, source, drift, fail_action
        self.volume, self.tip, self.well = 0.0, False, ""

    def observe(self, revision="initial"):
        # BOTH sides are synthetic. Never label a demonstration hardware_reported.
        return Observation(
            self.source,
            {
                "pipette.volume": Fact(self.volume, Evidence.SIMULATED, "uL"),
                "pipette.tip_attached": Fact(self.tip, Evidence.SIMULATED),
                "location.well": Fact(self.well, Evidence.SIMULATED),
            },
            revision,
            datetime.now(timezone.utc).isoformat(),
        )

    async def snapshot(self, operation):
        """Read synthetic current state without advancing it."""
        return self.observe(operation.operation_id)

    def prepare(self, operation):
        p = validate_client_operation(operation, self.device_id, {"LEFT": 1})

        async def execute():
            await asyncio.sleep(0.03)
            if operation.action == self.fail_action:
                return Outcome(Status.FAILED, self.observe(operation.operation_id), "Injected demo failure")
            if operation.action == "pick_up_tip":
                if self.tip:
                    return Outcome(Status.FAILED, error="Tip already attached")
                self.tip, self.well = True, p["well"]
            elif operation.action == "drop_tip":
                self.tip, self.volume = False, 0.0
            elif operation.action == "move_to_well":
                self.well = p["well"]
            elif operation.action == "aspirate":
                if not self.tip:
                    return Outcome(Status.FAILED, error="No tip attached")
                self.volume += p["volume_ul"] + self.drift
            elif operation.action == "dispense":
                if not self.tip or self.volume < p["volume_ul"]:
                    return Outcome(Status.FAILED, error="Insufficient volume or no tip")
                self.volume -= p["volume_ul"]
            return Outcome(Status.SUCCEEDED, self.observe(operation.operation_id))

        return execute


def operations(run_id="demo-run", device_id="ot2"):
    """A semantic transfer with explicit mount, channels, units and geometry inputs."""
    from .client_adapter import CLIENT_SCHEMA
    from .models import Operation

    base = {"mount": "LEFT", "channels": 1}
    steps = [
        ("home", {"axes": "XYZABC"}),
        ("pick_up_tip", {**base, "slot": "1", "well": "A1", "offset": [0, 0, 0], "tip_length_mm": 50, "speed": 20}),
        (
            "move_to_well",
            {
                **base,
                "slot": "2",
                "well": "A1",
                "origin": "bottom",
                "offset": [0, 0, 2],
                "tip_length_mm": 50,
                "speed": 20,
            },
        ),
        ("aspirate", {**base, "volume_ul": 20, "flow_rate_ul_s": 10}),
        (
            "move_to_well",
            {
                **base,
                "slot": "3",
                "well": "B1",
                "origin": "bottom",
                "offset": [0, 0, 2],
                "tip_length_mm": 50,
                "speed": 20,
            },
        ),
        ("dispense", {**base, "volume_ul": 20, "flow_rate_ul_s": 10, "empty_after": True}),
        ("drop_tip", {**base, "tiprack_diameter_mm": 5.23, "speed": 20}),
    ]
    return [
        Operation(run_id, str(i + 1), device_id, action, params, CLIENT_SCHEMA)
        for i, (action, params) in enumerate(steps)
    ]
