"""Persistent Matterix StateMachine integration; imports Isaac only in the launcher."""

from __future__ import annotations

import asyncio
import copy
import importlib
import math
import time
from datetime import datetime, timezone

from .client_adapter import validate_client_operation
from .models import Evidence, Fact, Observation, Operation, Outcome, Status
from .wire import plain


class NativeOT2Adapter:
    """Native liquid composition plus exact, deployment-authored motion/tip recipes.

    A recipe binds the complete semantic input, not just an action name. This
    prevents a different well, offset or tip length silently reusing a path.
    PR58 collection tip attachment is deliberately not inferred from its asset.
    """

    def __init__(self, profile: dict, submit) -> None:
        self.profile, self.submit = copy.deepcopy(profile), submit
        for axis, transform in self.profile.get("axis_transform", {}).items():
            if axis not in "XYZA" or len(axis) != 1 or set(transform) != {"index", "scale", "offset_mm"}:
                raise ValueError("Invalid axis transform")
            if type(transform["index"]) is not int or transform["index"] not in range(4):
                raise ValueError("Invalid native joint index")
            for key in ("scale", "offset_mm"):
                value = transform[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("Invalid axis transform number")
            if transform["scale"] == 0:
                raise ValueError("Axis scale cannot be zero")
        self.liquid = importlib.import_module("matterix_sm.compositional_actions.ot2_liquid_handling")
        self.motion = importlib.import_module("matterix_sm.primitive_actions.move_to_joint_config")
        self.semantic = importlib.import_module("matterix_sm.semantic_actions.ot2_action")
        self.spaces = importlib.import_module("matterix_sm.robot_action_spaces")

    def prepare(self, operation: Operation):
        """Construct native configs without stepping or mutating the environment."""
        p = validate_client_operation(operation, self.profile["device_id"], {"LEFT": 1})
        asset = self.profile["asset_name"]
        if operation.action in ("aspirate", "dispense"):
            cls = self.liquid.AspirateOT2Cfg if operation.action == "aspirate" else self.liquid.DispenseOT2Cfg
            configs = [cls(asset_name=asset, volume=p["volume_ul"], flow_rate=p["flow_rate_ul_s"])]
        else:
            matches = [
                r
                for r in self.profile["recipes"]
                if r["action"] == operation.action and r["parameters"] == plain(operation.parameters)
            ]
            if len(matches) != 1:
                raise ValueError("Exactly one reviewed Matterix recipe must match the entire operation")
            configs = []
            for step in matches[0]["steps"]:
                kind = step["type"]
                if kind == "joints":
                    if set(step) != {"type", "positions_m", "timeout_s", "threshold_m"}:
                        raise ValueError("Invalid joint recipe fields")
                    positions = step["positions_m"]
                    numbers = [*positions, step["timeout_s"], step["threshold_m"]]
                    if any(
                        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numbers
                    ):
                        raise ValueError("Recipe values must be finite numbers")
                    if step["timeout_s"] <= 0 or step["threshold_m"] <= 0:
                        raise ValueError("Recipe timeout and threshold must be positive")
                    if len(positions) != 4:
                        raise ValueError("OT2 joint recipe requires X/Y/Z/A coordinates")
                    configs.append(
                        self.motion.MoveToJointConfigCfg(
                            agent_assets=asset,
                            target_joint_positions=tuple(positions),
                            joint_indices=(0, 1, 2, 3),
                            timeout=step["timeout_s"],
                            position_threshold=step["threshold_m"],
                            settling_time=0.05,
                            action_space_info=self.spaces.OT2_JOINT_ACTION_SPACE,
                        )
                    )
                elif kind == "tip":
                    if set(step) != {"type", "asset_name", "attached"} or type(step["attached"]) is not bool:
                        raise ValueError("Tip recipes require one rigid tip asset and explicit attachment")
                    configs.append(
                        self.semantic.SetOT2TipAttachedCfg(
                            asset_name=asset, tip_asset_name=step["asset_name"], attached=step["attached"]
                        )
                    )
                else:
                    raise ValueError(f"Unsupported native recipe step: {kind}")
            if not configs:
                raise ValueError("Empty native recipe")

        async def execute():
            return await self.submit(operation, tuple(configs))

        return execute


def native_observation(obs: dict, profile: dict, revision: str) -> Observation:
    """Read native tensor observations; missing fields remain absent, never invented."""
    group, asset = obs.get("articulations", {}), profile["asset_name"]
    facts = {}
    for suffix, field, unit in (
        ("storage_volume", "pipette.volume", "uL"),
        ("is_tip_attached", "pipette.tip_attached", ""),
    ):
        value = group.get(f"{asset}__{suffix}")
        if value is not None:
            scalar = value.reshape(-1)[0].item()
            if suffix == "is_tip_attached":
                scalar = bool(scalar)
            facts[field] = Fact(scalar, Evidence.SIMULATED, unit)
    joints = group.get(f"{asset}__joint_pos")
    if joints is not None:
        values = joints[0].tolist()
        for index, value in enumerate(values):
            facts[f"joint.{index}"] = Fact(value, Evidence.SIMULATED, "m", "matterix.joint")
        # Explicit calibration: machine_mm = native_m * scale + offset_mm.
        for axis, transform in profile.get("axis_transform", {}).items():
            facts[f"axis.{axis}"] = Fact(
                values[transform["index"]] * transform["scale"] + transform["offset_mm"],
                Evidence.SIMULATED,
                "mm",
                "ot2.machine",
            )
    return Observation("matterix", facts, revision, datetime.now(timezone.utc).isoformat())


class MatterixRuntime:
    """Own one initialized environment on its main thread for the whole session."""

    def __init__(
        self,
        env,
        state_machine,
        obs,
        profile: dict,
        *,
        timeout: float = 60,
        is_running=lambda: True,
        after_step=lambda: None,
    ):
        if env.num_envs != 1:
            raise ValueError("A real-device mirror requires exactly one Matterix environment")
        self.env, self.sm, self.obs, self.profile = env, state_machine, obs, profile
        self.timeout, self.is_running = timeout, is_running
        self.after_step = after_step
        self.busy, self.fault = False, ""

    def observe(self, revision="initial") -> Observation:
        """Capture current native state without commanding the simulation."""
        return native_observation(self.obs, self.profile, revision)

    async def __call__(self, operation, configs) -> Outcome:
        """Apply semantic outputs, step physics and wait for actual terminal masks."""
        if self.busy or self.fault:
            raise RuntimeError(f"Runtime busy or needs explicit recovery: {self.fault}")
        self.busy = True
        try:
            if operation.action == "dispense" and operation.parameters["empty_after"]:
                volume = self.observe().facts.get("pipette.volume")
                if volume is None or abs(volume.value - operation.parameters["volume_ul"]) > 1e-6:
                    raise ValueError("empty_after requires the entire known simulated volume")
            self.sm.set_action_sequence(list(configs))
            self.sm.reset()  # Reset sequence bookkeeping, NEVER reset the environment here.
            deadline = time.monotonic() + self.timeout
            while True:
                if not self.is_running() or time.monotonic() >= deadline:
                    raise TimeoutError("Matterix closed or operation deadline exceeded")
                action, semantics = self.sm.step(self.obs)
                if isinstance(action, dict):
                    action = {key: value.to(self.env.device) for key, value in action.items()}
                else:
                    action = action.to(self.env.device)
                self.obs, _, terminated, truncated, _ = self.env.step(action, semantic_actions=semantics)
                self.after_step()
                if bool(terminated.any()) or bool(truncated.any()):
                    raise RuntimeError("Environment terminated/auto-reset; session state is no longer continuous")
                if bool(self.sm.action_sequence_failure.any()):
                    self.fault = "Matterix action sequence failed"
                    return Outcome(Status.FAILED, self.observe(operation.operation_id), self.fault)
                if bool(self.sm.action_sequence_success.all()):
                    return Outcome(Status.SUCCEEDED, self.observe(operation.operation_id))
                await asyncio.sleep(0)  # Keep transport responsive; Isaac remains on this thread.
        except BaseException as error:
            self.fault = f"{type(error).__name__}: {error}"
            raise
        finally:
            self.busy = False
