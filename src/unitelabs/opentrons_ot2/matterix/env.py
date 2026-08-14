"""
Import-safe Matterix OT-2 environment factory and readiness validation.

This module is the boundary a DT/Isaac Lab process uses to build the real
Matterix ``MatterixBaseEnv`` for the OT-2 and to validate that the host is
ready to do so.  It deliberately does NOT import ``isaaclab``, ``matterix``,
``matterix_sm``, or ``torch`` at module import time: those live on the Linux
Isaac Lab machine.  ``make_ot2_env`` imports them lazily, after the caller has
launched the Omniverse ``AppLauncher``, mirroring the PoC ``make_real_env``
contract (see ``twin_sim.backend``).

Readiness is checked without launching Omniverse through
:func:`validate_environment`, which reuses the preflight checks and adds a
gymnasium task-registration probe for ``config/matterix_ot2.json``.
"""

from __future__ import annotations

import dataclasses
import importlib
import platform
from pathlib import Path
from typing import Any, Protocol

from ..bridge.registry import ContractRegistry
from ..digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError
from .asset import MatterixAssetConfigurationError, OT2MatterixAssetConfig
from .preflight import Check, build_checks


class MatterixEnvironmentError(RuntimeError):
    """The real Matterix OT-2 environment cannot be constructed or validated."""


@dataclasses.dataclass(frozen=True)
class EnvironmentValidation:
    """Result of a non-launching Matterix OT-2 environment check."""

    ready: bool
    checks: tuple[Check, ...]

    @property
    def missing(self) -> tuple[Check, ...]:
        """Return the failing checks."""
        return tuple(check for check in self.checks if not check.ok)


def validate_environment(
    connector_config_path: str | Path,
    matterix_config_path: str | Path,
) -> EnvironmentValidation:
    """
    Run non-launching readiness checks for the real Matterix OT-2 env.

    Args:
        connector_config_path: The validated OT-2 digital-twin config.
        matterix_config_path: The ``matterix_ot2.json`` identity pins.

    Returns:
        An :class:`EnvironmentValidation` with the preflight checks plus a
        gymnasium task-registration probe.
    """
    checks = list(build_checks(connector_config_path, matterix_config_path))
    try:
        asset = OT2MatterixAssetConfig.from_file(matterix_config_path)
    except MatterixAssetConfigurationError as error:
        checks.append(
            Check(
                "Matterix OT-2 gym task registration",
                False,
                f"{type(error).__name__}: {error}",
                "Provide a valid config/matterix_ot2.json before registering the task.",
            )
        )
        return EnvironmentValidation(ready=False, checks=tuple(checks))

    registered = _gym_task_registered(asset.task_id)
    checks.append(
        Check(
            "Matterix OT-2 gym task registration",
            registered,
            asset.task_id if registered else f"{asset.task_id!r} is not registered",
            "Install the matterix_tasks OT-2 task extension and import it before gym.make().",
        )
    )
    return EnvironmentValidation(ready=all(check.ok for check in checks), checks=tuple(checks))


def _gym_task_registered(task_id: str) -> bool:
    """Probe gymnasium's registry without launching Omniverse."""
    try:
        gymnasium = importlib.import_module("gymnasium")
        importlib.import_module("matterix_tasks")  # import side effect registers tasks
    except (ImportError, AttributeError, ModuleNotFoundError):
        return False
    try:
        gymnasium.spec(task_id)
    except (ValueError, KeyError, TypeError):
        return False
    return True


class MatterixEnv(Protocol):
    """The minimal Matterix env surface used by the StateMachine driver."""

    num_envs: int
    step_dt: float
    device: str

    def reset(self, seed: int | None = None, options: dict[str, Any] | None = None) -> tuple[Any, dict]:
        """Reset the environment."""
        ...

    def step(
        self,
        action: object,
        semantic_actions: list[object] | None = None,
    ) -> tuple[object, object, object, object, dict]:
        """Advance the environment."""
        ...

    def close(self) -> None:
        """Close the environment."""
        ...


def make_ot2_env(
    task_id: str,
    *,
    connector_config_path: str | Path,
    matterix_config_path: str | Path,
    num_envs: int = 1,
    device: str = "cuda",
    use_fabric: bool = True,
) -> MatterixEnv:
    """
    Construct the real Matterix OT-2 env.

    The Omniverse ``AppLauncher`` must already be running before this is
    called (the caller owns the launcher lifecycle, matching the Matterix
    ``scripts/run_workflow.py`` pattern).  Identity pins are validated before
    the env is created so a mismatched connector snapshot fails closed.

    Returns:
        The unwrapped Matterix gymnasium env.  The caller is responsible for
        ``env.reset()`` and ``env.close()``.
    """
    if platform.system() != "Linux":
        msg = "Matterix OT-2 env construction requires a Linux Isaac Lab host"
        raise MatterixEnvironmentError(msg)

    connector = _load_connector(connector_config_path)
    asset = OT2MatterixAssetConfig.from_file(matterix_config_path)
    try:
        asset.validate_connector(connector, ContractRegistry.packaged())
        asset.validate_joint_alignment()
        asset.validate_reference_frame_alignment()
        asset.validate_tip_rack_bindings(connector)
    except MatterixAssetConfigurationError as error:
        raise MatterixEnvironmentError(str(error)) from error

    try:
        gymnasium = importlib.import_module("gymnasium")
        importlib.import_module("matterix_tasks")  # registers Matterix gym tasks
        from isaaclab_tasks.utils.parse_cfg import parse_env_cfg  # type: ignore[import-not-found]
    except (ImportError, ModuleNotFoundError) as error:
        msg = (
            "Matterix OT-2 runtime missing — install matterix_tasks + isaaclab + "
            "gymnasium in the Isaac Lab environment before creating the env"
        )
        raise MatterixEnvironmentError(msg) from error

    try:
        env_cfg = parse_env_cfg(
            task_id,
            device=device,
            num_envs=num_envs,
            use_fabric=use_fabric,
        )
        env = gymnasium.make(task_id, cfg=env_cfg).unwrapped
    except (ImportError, KeyError, ValueError, TypeError) as error:
        msg = f"Unable to create Matterix OT-2 env {task_id!r}: {error}"
        raise MatterixEnvironmentError(msg) from error
    return env  # type: ignore[return-value]


def _load_connector(connector_config_path: str | Path) -> DigitalTwinConfig:
    try:
        return DigitalTwinConfig.from_file(connector_config_path)
    except DigitalTwinConfigurationError as error:
        msg = f"Invalid OT-2 connector config {connector_config_path}: {error}"
        raise MatterixEnvironmentError(msg) from error


__all__ = [
    "Check",
    "EnvironmentValidation",
    "MatterixEnv",
    "MatterixEnvironmentError",
    "make_ot2_env",
    "validate_environment",
]
