"""Operator-facing frame alignment CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.matterix.reference_frames_cli import main


def test_check_reports_example_unverified(capsys) -> None:
    assert main(["check", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified"] is False
    assert report["world_from_deck"]["child_frame"] == "ot2_deck"
    assert main(["check", "--strict"]) == 1


def test_derive_writes_reviewable_candidate(tmp_path: Path) -> None:
    observations = {
        "schema_version": "1.0",
        "parent_frame": "matterix_world",
        "child_frame": "ot2_deck",
        "source_points_m": [[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0], [0, 0, 0.1]],
        "target_points_m": [[1, 2, 3], [1.1, 2, 3], [1, 2.1, 3], [1, 2, 3.1]],
        "max_rms_error_m": 1e-9,
        "evidence": "TEST_ONLY surveyed fiducials",
    }
    source = tmp_path / "observations.json"
    destination = tmp_path / "candidate.json"
    source.write_text(json.dumps(observations), encoding="utf-8")
    assert main(["derive", "--input", str(source), "--output", str(destination)]) == 0
    result = json.loads(destination.read_text(encoding="utf-8"))
    assert result["status"] == "CANDIDATE_REQUIRES_REVIEW"
    assert result["transform"]["translation_m"] == pytest.approx([1.0, 2.0, 3.0])


def _write_transform_candidate(
    tmp_path: Path,
    *,
    name: str,
    parent_frame: str,
    child_frame: str,
    translation: list[float],
) -> Path:
    source_points = [[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0], [0, 0, 0.1]]
    target_points = [[point[index] + translation[index] for index in range(3)] for point in source_points]
    observations = tmp_path / f"{name}-observations.json"
    candidate = tmp_path / f"{name}-candidate.json"
    observations.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "parent_frame": parent_frame,
                "child_frame": child_frame,
                "source_points_m": source_points,
                "target_points_m": target_points,
                "max_rms_error_m": 1e-9,
                "evidence": f"TEST_ONLY {name} fiducials",
            }
        ),
        encoding="utf-8",
    )
    assert main(["derive", "--input", str(observations), "--output", str(candidate)]) == 0
    return candidate


def test_build_candidate_joins_reviewed_chain_without_overwriting_active_config(tmp_path: Path, capsys) -> None:
    config = tmp_path / "matterix.json"
    config.write_bytes(Path("config/matterix_ot2.json").read_bytes())
    before = config.read_bytes()
    world = _write_transform_candidate(
        tmp_path,
        name="world-base",
        parent_frame="matterix_world",
        child_frame="ot2_base",
        translation=[1.0, 2.0, 3.0],
    )
    deck = _write_transform_candidate(
        tmp_path,
        name="base-deck",
        parent_frame="ot2_base",
        child_frame="ot2_deck",
        translation=[0.1, 0.2, 0.3],
    )
    capsys.readouterr()
    output = tmp_path / "candidate-config.json"
    code = main(
        [
            "build-candidate",
            "--config",
            str(config),
            "--world-from-base",
            str(world),
            "--base-from-deck",
            str(deck),
            "--alignment-id",
            "TEST-FRAME-ALIGNMENT-1",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert config.read_bytes() == before
    assert output.with_suffix(".json.sha256").is_file()
    result = json.loads(output.read_text(encoding="utf-8"))["reference_frame_alignment"]
    assert result["status"] == "VERIFIED"
    assert result["alignment_id"] == "TEST-FRAME-ALIGNMENT-1"
    assert "active_config_unchanged=true" in capsys.readouterr().out
