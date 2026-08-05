"""Narrow Matterix StateMachine integration point for an OT-2 action extension."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol

from .actions import OT2ActionCfg


class MatterixRuntimeUnavailable(RuntimeError):
    """A real Matterix OT-2 asset or action implementation is unavailable."""


class MatterixActionFactory(Protocol):
    """Convert connector-owned semantic configs to installed Matterix configs."""

    def build_ot2_action_cfg(self, action: OT2ActionCfg) -> object:
        """Build one Matterix compositional-action configuration."""
        ...


class MatterixStateMachine(Protocol):
    """Matterix workflow-level state-machine boundary."""

    def set_action_sequence(self, configs: list[object]) -> None:
        """Install the complete compositional action sequence."""
        ...


@dataclass(frozen=True)
class _ModuleActionFactory:
    module: ModuleType

    def build_ot2_action_cfg(self, action: OT2ActionCfg) -> object:
        return self.module.build_ot2_action_cfg(action)


def load_action_factory(module_name: str) -> MatterixActionFactory:
    """Load the separately installed OT-2 Matterix action extension."""
    if not module_name:
        msg = "Matterix OT-2 action_factory_module must not be empty"
        raise MatterixRuntimeUnavailable(msg)
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        msg = f"Unable to import Matterix OT-2 action factory {module_name!r}: {error}"
        raise MatterixRuntimeUnavailable(msg) from error
    builder = getattr(module, "build_ot2_action_cfg", None)
    if not callable(builder):
        msg = f"Matterix action factory {module_name!r} must expose callable build_ot2_action_cfg(action)"
        raise MatterixRuntimeUnavailable(msg)
    return _ModuleActionFactory(module)


def install_action_sequence(
    state_machine: MatterixStateMachine,
    actions: tuple[OT2ActionCfg, ...],
    factory: MatterixActionFactory,
) -> tuple[object, ...]:
    """Translate and install a complete workflow through ``set_action_sequence``."""
    if not actions:
        msg = "Matterix OT-2 workflow contains no side-effecting action configs"
        raise MatterixRuntimeUnavailable(msg)
    runtime_configs = tuple(factory.build_ot2_action_cfg(action) for action in actions)
    if any(config is None for config in runtime_configs):
        msg = "Matterix OT-2 action factory returned an empty action configuration"
        raise MatterixRuntimeUnavailable(msg)
    state_machine.set_action_sequence(list(runtime_configs))
    return runtime_configs
