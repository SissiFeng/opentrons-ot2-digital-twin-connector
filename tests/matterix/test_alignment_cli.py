"""Operator-facing OT-2 joint alignment inspection commands."""

from __future__ import annotations

import json

import pytest

from unitelabs.opentrons_ot2.matterix.alignment_cli import main


def test_check_reports_checked_profile_as_unverified(capsys) -> None:
    assert main(["check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is False
    assert [row["axis"] for row in report["axes"]] == list("XYZABC")
    assert report["axes"][0]["status"] == "UNVERIFIED"


def test_check_strict_fails_until_physical_measurements_exist(capsys) -> None:
    assert main(["check", "--strict", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verified"] is False


def test_derive_reports_direction_and_offset(capsys) -> None:
    code = main(
        [
            "derive",
            "--real-reference",
            "100",
            "--sim-reference",
            "0.05",
            "--real-delta",
            "10",
            "--sim-delta",
            "-0.01",
            "--json",
        ]
    )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sign"] == -1
    assert result["offset_m"] == pytest.approx(0.15)


def test_derive_rejects_scale_mismatch(capsys) -> None:
    code = main(
        [
            "derive",
            "--real-reference",
            "100",
            "--sim-reference",
            "0.05",
            "--real-delta",
            "10",
            "--sim-delta",
            "0.02",
        ]
    )
    assert code == 1
    assert "magnitude mismatch" in capsys.readouterr().err
