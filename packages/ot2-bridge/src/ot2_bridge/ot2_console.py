"""OT-2 first-connection mappings for the shared console; no device-state owner."""

from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

from .models import Evidence, Fact, Observation, Operation, Outcome, Status
from .wire import fingerprint, plain
from .network import sila_tunnel_host

SCHEMA = "ot2.home-check/1"
MATTERIX_REVISION = "f38d10d86a91c86ddc0e21db1927b68565d8378f"
ASSETS_REVISION = "98da840f8bb2770ac5ec82a483dca3c4cf635206"


def profile_template():
    return {
        "schema": "ot2.profile/1",
        "device_id": "ot2-lab",
        "task": "Matterix-OT2-DualMulti-Home-v1",
        "workflow": "move_home",
        "matterix_revision": MATTERIX_REVISION,
        "assets_revision": ASSETS_REVISION,
        "pipettes": {
            "left": {"model": "p10_multi_v1.6", "channels": 8},
            "right": {"model": "p300_multi_v2.0", "channels": 8},
            "identity_source": "operator_reported",
        },
        "real": {"backend": "ot2-sila", "host": "127.0.0.1", "port": 50051, "server_uuid": ""},
    }


def validate_profile(profile, *, hardware=False):
    template = profile_template()
    if not isinstance(profile, dict) or set(profile) != set(template):
        raise ValueError("Expected the OT-2 profile fields")
    for key in ("schema", "task", "workflow", "matterix_revision", "assets_revision", "pipettes"):
        if profile[key] != template[key]:
            raise ValueError(f"Unsupported OT-2 {key}")
    if not isinstance(profile["device_id"], str) or not profile["device_id"].strip():
        raise ValueError("OT-2 device_id is required")
    real = profile["real"]
    if not isinstance(real, dict) or set(real) != set(template["real"]) or real["backend"] != "ot2-sila":
        raise ValueError("Invalid OT-2 connector binding")
    sila_tunnel_host(real["host"])
    if type(real["port"]) is not int or not 1 <= real["port"] <= 65535:
        raise ValueError("Invalid SiLA port")
    if not isinstance(real["server_uuid"], str) or (hardware and not real["server_uuid"].strip()):
        raise ValueError("Check the connector and bind its server UUID before real execution")
    return profile


def make_plan(profile, run_id):
    validate_profile(profile)
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    ops = [
        Operation(run_id, "01-home", profile["device_id"], "home", {"axes": "XYZABC"}, SCHEMA),
        Operation(run_id, "02-readback", profile["device_id"], "read_position", {}, SCHEMA),
    ]
    return {
        "binding": {
            "schema": SCHEMA,
            "run_id": run_id,
            "device_id": profile["device_id"],
            "profile_sha256": fingerprint(profile),
        },
        "profile": copy.deepcopy(profile),
        "operations": plain(ops),
    }


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"binding", "profile", "operations"}:
        raise ValueError("Invalid OT-2 plan")
    if fingerprint(plan) != fingerprint(make_plan(plan["profile"], plan["binding"]["run_id"])):
        raise ValueError("OT-2 plan differs from its reviewed workflow")
    return plan


def validate_operation(operation, profile):
    if plain(operation) not in make_plan(profile, operation.run_id)["operations"]:
        raise ValueError("Operation is not part of the OT-2 home/readback workflow")


class OT2HomeAdapter:
    """Map home/readback to the OT-2 motion feature; retain firmware provenance."""

    def __init__(self, profile, transport, *, hardware=True, stop_requested=lambda: False):
        validate_profile(profile, hardware=True)
        if type(hardware) is not bool:
            raise ValueError("An explicit real/simulation target is required")
        self.profile, self.transport = copy.deepcopy(profile), transport
        self.hardware, self.stop_requested = hardware, stop_requested

    async def snapshot(self, revision):
        info = await self.transport.describe()
        if info["server_uuid"] != self.profile["real"]["server_uuid"]:
            raise ValueError("OT-2 server UUID differs from the reviewed binding")
        if info["is_simulating"] is not (not self.hardware):
            raise ValueError(
                "OT-2 connector mode mismatch: this run requires physical hardware"
                if self.hardware
                else "Development test requires an explicitly simulated connector"
            )
        evidence = Evidence.HARDWARE if self.hardware else Evidence.SIMULATED
        facts = {}
        for axis in "XYZABC":
            value = info["position"].get(axis.lower())
            homed = info["homed"].get(axis.lower())
            if type(value) not in (int, float) or not math.isfinite(value) or type(homed) is not bool:
                raise ValueError(f"Invalid OT-2 {axis} readback")
            facts[f"axis.{axis}"] = Fact(value, evidence, "mm", "ot2.machine")
            facts[f"homed.{axis}"] = Fact(homed, Evidence.TRACKED if self.hardware else Evidence.SIMULATED)
        return Observation(
            "ot2-firmware" if self.hardware else "ot2-connector-simulation",
            facts,
            revision,
            datetime.now(timezone.utc).isoformat(),
        )

    def prepare(self, operation):
        validate_operation(operation, self.profile)

        async def execute():
            try:
                await self.snapshot("preflight")
                if self.stop_requested():
                    raise RuntimeError("Stop requested; no OT-2 command dispatched")
                if operation.action == "home":
                    await self.transport.command("MotionControlFeature", "Home", Axes="XYZABC")
                observation = await self.snapshot(operation.operation_id)
                if not all(observation.facts[f"homed.{axis}"].value for axis in "XYZABC"):
                    return Outcome(Status.FAILED, observation, "Connector did not report all axes homed")
                return Outcome(Status.SUCCEEDED, observation)
            except Exception as error:
                return Outcome(Status.UNKNOWN, error=f"{type(error).__name__}: {error}; no automatic retry")

        return execute


class NativeOT2HomeAdapter:
    """Use the pinned native move_home action, followed by a fresh observation."""

    def __init__(self, profile, workflow_actions, runtime):
        validate_profile(profile)
        actions = tuple(workflow_actions)
        if len(actions) != 1 or type(actions[0]).__name__ != "MoveToJointConfigCfg":
            raise ValueError("OT-2 native move_home structure changed")
        action = actions[0]
        if action.agent_assets != "ot2" or tuple(action.target_joint_positions) != (0.2, 0.18, 0.0, 0.0):
            raise ValueError("OT-2 native home binding changed")
        self.profile, self.actions, self.runtime = profile, actions, runtime

    def prepare(self, operation):
        validate_operation(operation, self.profile)

        async def execute():
            if operation.action == "home":
                outcome = await self.runtime(operation, self.actions)
            else:
                outcome = Outcome(Status.SUCCEEDED, self.runtime.observe(operation.operation_id))
            if outcome.status is Status.SUCCEEDED:
                facts = outcome.observation.facts if outcome.observation else {}
                for index, target in enumerate((0.2, 0.18, 0.0, 0.0)):
                    fact = facts.get(f"joint.{index}")
                    if (
                        fact is None
                        or type(fact.value) not in (int, float)
                        or not math.isfinite(fact.value)
                        or abs(fact.value - target) > 0.005
                    ):
                        return Outcome(
                            Status.FAILED,
                            outcome.observation,
                            "Matterix home pose readback is missing or outside tolerance",
                        )
            return outcome

        return execute
