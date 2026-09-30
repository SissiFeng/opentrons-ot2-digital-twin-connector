"""Flex mappings and reviewed single-tip workflow; no hardware state ownership."""

from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

from .models import Evidence, Fact, Observation, Operation, Outcome, Status
from .wire import fingerprint, plain

MATTERIX_REVISION = "18a96b78ee989fbf4e480e9db6a588c148c6adbe"
ASSETS_REVISION = "c57f12a04079a5f99405e1fcb818a3b7ccc52e5b"
CONNECTOR_REVISION = "2639016ee9f234949aaf596c2ab2b93694eb0e0b"
TASK = "Matterix-Flex-Pipetting-v1"
WORKFLOW = "tip_pickup_and_release"
SCHEMA = "flex.tip-cycle/1"


def profile_template():
    """Return editable configuration with no fabricated hardware coordinates."""
    return {
        "schema": "flex.profile/1",
        "device_id": "flex-lab",
        "mount": "LEFT",
        "task": TASK,
        "workflow": WORKFLOW,
        "matterix_revision": MATTERIX_REVISION,
        "assets_revision": ASSETS_REVISION,
        "real": {
            "backend": "flex-sila",
            "host": "127.0.0.1",
            "port": 50051,
            "server_uuid": "",
            "pipette_id": "",
            "pipette_model": "",
            "calibration_revision": "",
            "tiprack": "",
            "tip_length_mm": None,
            "speed_mm_s": 30.0,
            "pickup": None,
            "pickup_clearance": None,
            "drop": None,
            "drop_clearance": None,
            "park": None,
        },
    }


def validate_profile(profile, *, hardware=False):
    """Reject unsupported profiles and incomplete physical bindings before dispatch."""
    template = profile_template()
    if not isinstance(profile, dict) or set(profile) != set(template):
        raise ValueError("Profile must contain exactly the flex.profile/1 fields")
    for key in ("schema", "mount", "task", "workflow", "matterix_revision", "assets_revision"):
        if profile[key] != template[key]:
            raise ValueError(f"Unsupported {key}: this release binds the pinned left single-tip Flex workflow")
    if not isinstance(profile["device_id"], str) or not profile["device_id"].strip():
        raise ValueError("device_id is required")
    real = profile["real"]
    if not isinstance(real, dict) or set(real) != set(template["real"]):
        raise ValueError("Invalid real backend configuration fields")
    if real["backend"] != "flex-sila":
        raise ValueError("This launcher supports flex-sila; inject another Adapter in the runner API")
    if real["host"] not in ("127.0.0.1", "localhost"):
        raise ValueError("Use a localhost SSH tunnel for the SiLA connection")
    if type(real["port"]) is not int or not 1 <= real["port"] <= 65535:
        raise ValueError("Invalid connector port")
    _number(real["speed_mm_s"], 0, 100, "speed_mm_s")
    for key in ("server_uuid", "pipette_id", "pipette_model", "calibration_revision", "tiprack"):
        if not isinstance(real[key], str) or (hardware and not real[key].strip()):
            raise ValueError(f"Qualified hardware configuration requires {key}")
    if real["tip_length_mm"] is not None:
        _number(real["tip_length_mm"], 0, 100, "tip_length_mm")
    elif hardware:
        raise ValueError("Measured tip_length_mm is required")
    for key in ("pickup", "pickup_clearance", "drop", "drop_clearance", "park"):
        point = real[key]
        if point is None and not hardware:
            continue
        if not isinstance(point, dict) or set(point) != {"x", "y", "z"}:
            raise ValueError(f"{key} requires calibrated x/y/z in deck mm")
        for value in point.values():
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError(f"{key} coordinates must be finite numbers")
    for target, clearance in (("pickup", "pickup_clearance"), ("drop", "drop_clearance")):
        low, high = real[target], real[clearance]
        if low is not None and high is not None:
            if low["x"] != high["x"] or low["y"] != high["y"] or high["z"] <= low["z"]:
                raise ValueError(f"{clearance} must be vertically above {target}")
    clearances = [real[key] for key in ("pickup_clearance", "drop_clearance", "park")]
    if all(point is not None for point in clearances) and len({point["z"] for point in clearances}) != 1:
        raise ValueError("Pickup/drop clearance and park must share a reviewed travel height")
    return profile


def _number(value, low, high, name):
    if type(value) not in (float, int) or not math.isfinite(value) or not low < value <= high:
        raise ValueError(f"{name} must be greater than {low} and at most {high}")


def make_plan(profile, run_id):
    """Bind preview and both adapters to the same immutable semantic operations."""
    validate_profile(profile)
    operations = [
        Operation(
            run_id, "01-pickup", profile["device_id"], "pick_up_tip", {"mount": "LEFT", "tip": "Tips__A1"}, SCHEMA
        ),
        Operation(run_id, "02-drop", profile["device_id"], "drop_tip", {"mount": "LEFT", "tip": "Tips__A1"}, SCHEMA),
        Operation(run_id, "03-park", profile["device_id"], "park", {"mount": "LEFT"}, SCHEMA),
    ]
    return {
        "binding": {
            "schema": SCHEMA,
            "run_id": run_id,
            "device_id": profile["device_id"],
            "profile_sha256": fingerprint(profile),
        },
        "profile": copy.deepcopy(profile),
        "operations": plain(operations),
    }


def validate_plan(plan):
    """Require the exact registered workflow, including order, identities and profile hash."""
    if not isinstance(plan, dict) or set(plan) != {"binding", "profile", "operations"}:
        raise ValueError("Invalid Flex plan envelope")
    expected = make_plan(plan["profile"], plan["binding"]["run_id"])
    if fingerprint(plan) != fingerprint(expected):
        raise ValueError("Plan differs from its registered workflow or reviewed binding")
    return plan


def validate_operation(operation, profile):
    candidates = make_plan(profile, operation.run_id)["operations"]
    if plain(operation) not in candidates:
        raise ValueError("Operation is not an exact member of the registered Flex workflow")


class NativeFlexAdapter:
    """Partition the pinned native workflow at semantic completion boundaries."""

    def __init__(self, profile, workflow_actions, submit):
        validate_profile(profile)
        self.profile, self.submit = copy.deepcopy(profile), submit
        actions = list(workflow_actions)
        # PR59 native workflow: wait + approach/seat/attach/lift + trash/release/retract + park.
        expected = [
            "WaitCfg",
            "MoveToJointConfigCfg",
            "MoveToJointConfigCfg",
            "SetFLEXTipAttachedCfg",
            "MoveToJointConfigCfg",
            "MoveToJointConfigCfg",
            "MoveToJointConfigCfg",
            "SetFLEXTipAttachedCfg",
            "WaitCfg",
            "MoveToJointConfigCfg",
            "MoveToJointConfigCfg",
        ]
        if [type(action).__name__ for action in actions] != expected:
            raise ValueError("Native workflow structure changed; adapter review is required")
        for index, attached in ((3, True), (7, False)):
            action = actions[index]
            if (action.attached, action.tip_child_id, action.tip_asset_name, action.pipette) != (
                attached,
                "Tips__A1",
                "flex_tips",
                "left",
            ):
                raise ValueError("Native tip binding changed")
        self.chunks = {"pick_up_tip": tuple(actions[:5]), "drop_tip": tuple(actions[5:10]), "park": tuple(actions[10:])}

    def prepare(self, operation):
        validate_operation(operation, self.profile)

        async def execute():
            return await self.submit(operation, self.chunks[operation.action])

        return execute


class FlexAdapter:
    """Map semantic operations to a replaceable asynchronous connector transport."""

    def __init__(self, profile, transport, *, hardware, stop_requested=lambda: False):
        validate_profile(profile, hardware=True)
        self.profile, self.transport, self.hardware = copy.deepcopy(profile), transport, hardware
        self.stop_requested = stop_requested

    async def snapshot(self, revision=""):
        values = await self.transport.snapshot()
        real = self.profile["real"]
        if values["is_simulating"] is not (not self.hardware):
            raise ValueError("Connector hardware/simulation mode does not match the selected run")
        if values["server_uuid"] != real["server_uuid"]:
            raise ValueError("Connected SiLA server UUID differs from the reviewed profile")
        pipette = values["pipette"]
        if (pipette["id"], pipette["model"], pipette["channels"]) != (real["pipette_id"], real["pipette_model"], 1):
            raise ValueError(
                "Pipette identity/model/channel count differs from the reviewed left single-channel binding"
            )
        if values["machine_error"] or values["estop"] != "DISENGAGED":
            raise ValueError("Connector reports an error or engaged/unavailable emergency stop")
        evidence = Evidence.HARDWARE if self.hardware else Evidence.SIMULATED
        return Observation(
            "flex-sila" if self.hardware else "flex-ot3-simulator",
            {
                "pipette.tip_attached": Fact(values["tip_present"], evidence),
                # Position is controller-reported; it is not independent vision or collision evidence.
                **{
                    f"position.{axis}": Fact(value, evidence, "mm", "flex.deck.LEFT")
                    for axis, value in values["position"].items()
                },
            },
            revision,
            datetime.now(timezone.utc).isoformat(),
        )

    def prepare(self, operation):
        validate_operation(operation, self.profile)
        real = self.profile["real"]

        async def command(feature, name, **params):
            if self.stop_requested():
                raise RuntimeError("Stop requested; no further physical command dispatched")
            return await self.transport.command(feature, name, **params)

        async def move(point):
            await command(
                "MotionController",
                "MoveTo",
                Mount="LEFT",
                **{k.upper(): v for k, v in point.items()},
                Speed=real["speed_mm_s"],
            )

        async def execute():
            try:
                before = await self.snapshot(operation.operation_id + ":before")
                attached = before.facts["pipette.tip_attached"].value
                if attached is not (operation.action == "drop_tip"):
                    return Outcome(Status.FAILED, before, "Unexpected tip state; reconcile before continuing")
                if operation.action == "pick_up_tip":
                    # Lift vertically before traversing to the rack. Subsequent travel
                    # points share the explicitly reviewed clearance height.
                    current = {axis: before.facts[f"position.{axis}"].value for axis in ("x", "y", "z")}
                    current["z"] = real["pickup_clearance"]["z"]
                    await move(current)
                    await move(real["pickup_clearance"])
                    await command(
                        "TipController",
                        "PickUpTip",
                        Mount="LEFT",
                        Location={k.upper(): v for k, v in real["pickup"].items()},
                        TipLength=real["tip_length_mm"],
                        PrepAfter=False,
                    )
                    await move(real["pickup_clearance"])
                elif operation.action == "drop_tip":
                    await move(real["drop_clearance"])
                    await command(
                        "TipController",
                        "DropTip",
                        Mount="LEFT",
                        Location={k.upper(): v for k, v in real["drop"].items()},
                        HomeAfter=False,
                    )
                    await move(real["drop_clearance"])
                else:
                    await move(real["park"])
                after = await self.snapshot(operation.operation_id)
                expected = operation.action == "pick_up_tip"
                status = Status.SUCCEEDED if after.facts["pipette.tip_attached"].value is expected else Status.FAILED
                return Outcome(status, after, "" if status is Status.SUCCEEDED else "Unexpected final tip sensor state")
            except Exception as error:
                # A command or observation failure can occur after physical side effects.
                return Outcome(Status.UNKNOWN, error=f"{type(error).__name__}: {error}")

        return execute
