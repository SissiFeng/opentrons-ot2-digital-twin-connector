"""Translate contract-resolved bridge commands into OT-2 Matterix action plans."""

from __future__ import annotations

import dataclasses

from ..bridge.adapter import OT2WorkflowAdapter
from ..bridge.models import OT2Command
from ..bridge.workflow import ParsedWorkflow
from ..features._digital_twin_types import Position
from .actions import (
    AspirateCfg,
    DispenseCfg,
    DropTipCfg,
    HomeCfg,
    MoveToCfg,
    MoveToWellCfg,
    OT2ActionCfg,
    PickUpTipCfg,
    ReconcileTipCfg,
)


@dataclasses.dataclass(frozen=True)
class StepActionSequence:
    """Matterix actions tied to one immutable connector request."""

    step_id: str
    action: str
    request_hash: str
    actions: tuple[OT2ActionCfg, ...]


@dataclasses.dataclass(frozen=True)
class MatterixWorkflowPlan:
    """Workflow-level sequence handed to the Matterix StateMachine boundary."""

    workflow_name: str
    workflow_version: str
    contract_id: str
    config_id: str
    steps: tuple[StepActionSequence, ...]

    @property
    def actions(self) -> tuple[OT2ActionCfg, ...]:
        """Flatten all compositional action configs in source order."""
        return tuple(action for step in self.steps for action in step.actions)


@dataclasses.dataclass(frozen=True)
class OT2MatterixPlanner:
    """Use the bridge adapter as the single source of workflow semantics."""

    adapter: OT2WorkflowAdapter

    def plan(self, workflow: ParsedWorkflow) -> MatterixWorkflowPlan:
        """Translate only connector-owned side effects into twin actions."""
        steps = []
        for source in workflow.steps:
            command = self.adapter.adapt(source)
            if command is None or not command.side_effecting:
                continue
            steps.append(
                StepActionSequence(
                    step_id=source.step_id,
                    action=source.action,
                    request_hash=command.request_hash,
                    actions=(_to_action(command),),
                )
            )
        return MatterixWorkflowPlan(
            workflow_name=workflow.name,
            workflow_version=workflow.version,
            contract_id=self.adapter.registry.snapshot.contract_id,
            config_id=self.adapter.config.config_id,
            steps=tuple(steps),
        )


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value))


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"Bridge command field {field!r} is not numeric"
        raise ValueError(msg)
    return float(value)


def _to_action(command: OT2Command) -> OT2ActionCfg:
    parameters = command.parameters
    mount = _enum_text(parameters.get("mount", ""))
    if command.feature_identifier == "MotionController" and command.endpoint == "Home":
        return HomeCfg()
    if command.feature_identifier == "MotionController" and command.endpoint == "MoveTo":
        position = parameters["position"]
        if not isinstance(position, Position):
            msg = "Bridge MoveTo command position is not a Position structure"
            raise ValueError(msg)
        return MoveToCfg(
            mount=mount,
            x=_number(position.x, "position.x"),
            y=_number(position.y, "position.y"),
            z=_number(position.z, "position.z"),
            speed=_number(parameters["speed"], "speed"),
            reference=_enum_text(parameters["reference"]),
        )
    if command.feature_identifier == "MotionController" and command.endpoint == "MoveToWell":
        return MoveToWellCfg(
            mount=mount,
            labware_id=str(parameters["labware_id"]),
            well=str(parameters["well"]),
            height=_enum_text(parameters["height"]),
            speed=_number(parameters["speed"], "speed"),
            reference=_enum_text(parameters["reference"]),
        )
    if command.feature_identifier == "TipController" and command.endpoint == "PickUpTip":
        return PickUpTipCfg(
            mount=mount,
            labware_id=str(parameters["labware_id"]),
            well=str(parameters["well"]),
        )
    if command.feature_identifier == "TipController" and command.endpoint == "DropTip":
        return DropTipCfg(mount=mount)
    if command.feature_identifier == "TipController" and command.endpoint == "ReconcileTip":
        return ReconcileTipCfg(mount=mount, present=bool(parameters["present"]))
    if command.feature_identifier == "LiquidHandlingController" and command.endpoint == "Aspirate":
        return AspirateCfg(
            mount=mount,
            volume=_number(parameters["volume"], "volume"),
            flow_rate=_number(parameters["flow_rate"], "flow_rate"),
        )
    if command.feature_identifier == "LiquidHandlingController" and command.endpoint == "Dispense":
        return DispenseCfg(
            mount=mount,
            volume=_number(parameters["volume"], "volume"),
            flow_rate=_number(parameters["flow_rate"], "flow_rate"),
        )
    msg = f"No Matterix OT-2 action mapping for {command.feature_identifier}.{command.endpoint}"
    raise ValueError(msg)
