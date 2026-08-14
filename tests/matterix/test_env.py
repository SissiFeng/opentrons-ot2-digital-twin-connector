"""Import-safe Matterix OT-2 env validation (no Isaac Lab / Omniverse)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.matterix.env import (
    EnvironmentValidation,
    MatterixEnvironmentError,
    make_ot2_env,
    validate_environment,
)

from tests.digital_twin.helpers import config_mapping


def test_validate_environment_reports_not_ready_without_runtime() -> None:
    validation = validate_environment("config/ot2_dt_config.json", "config/matterix_ot2.json")
    assert isinstance(validation, EnvironmentValidation)
    assert validation.ready is False
    names = {check.name for check in validation.checks}
    assert "Matterix OT-2 USD asset" in names
    assert "Matterix OT-2 gym task registration" in names
    assert "Linux platform" in names


def test_validate_environment_reports_config_ok_even_when_runtime_missing() -> None:
    validation = validate_environment("config/ot2_dt_config.json", "config/matterix_ot2.json")
    by_name = {check.name: check for check in validation.checks}
    assert by_name["OT-2 connector configuration"].ok is True
    assert by_name["Confirmed physical calibration"].ok is False  # example config is not hardware-approved
    assert by_name["matterix_ot2_actions"].ok is False


def test_make_ot2_env_fails_closed_on_macos(tmp_path: Path) -> None:
    connector_path = tmp_path / "ot2_dt_config.json"
    connector_path.write_text(json.dumps(config_mapping(tmp_path / "state.json")), encoding="utf-8")
    with pytest.raises(MatterixEnvironmentError, match="Linux"):
        make_ot2_env(
            "Matterix-OT2-LiquidHandler-v1",
            connector_config_path=connector_path,
            matterix_config_path="config/matterix_ot2.json",
        )


def test_validate_environment_accepts_isolated_config(tmp_path: Path) -> None:
    connector_path = tmp_path / "ot2_dt_config.json"
    connector_path.write_text(json.dumps(config_mapping(tmp_path / "state.json")), encoding="utf-8")
    validation = validate_environment(connector_path, "config/matterix_ot2.json")
    by_name = {check.name: check for check in validation.checks}
    assert by_name["OT-2 connector configuration"].ok is True
