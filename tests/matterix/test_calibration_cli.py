"""Operator CLI tests for the real OT-2 calibration pipeline."""

from __future__ import annotations

import json

from unitelabs.opentrons_ot2.matterix.calibration_cli import main


def test_validate_checked_in_plan(capsys) -> None:
    """The reviewed template is parseable but does not move hardware."""
    assert main(["validate-plan", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert set(payload["plan"]["axes"]) == {"X", "Y", "Z", "A"}


def test_run_requires_all_three_explicit_safety_confirmations() -> None:
    """Argparse rejects a hardware run without operator safety confirmations."""
    try:
        main(["run", "--robot", "robot.local:50051"])
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("Expected argparse to reject missing confirmations")
