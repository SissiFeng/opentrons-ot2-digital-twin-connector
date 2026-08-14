"""Joint evidence fusion and candidate promotion tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.matterix.alignment import JOINT_AXES, JointAlignmentError
from unitelabs.opentrons_ot2.matterix.alignment_cli import main
from unitelabs.opentrons_ot2.matterix.alignment_evidence import build_verified_alignment_candidate

from tests.matterix.helpers import verified_alignment, verified_alignment_mapping


def _evidence() -> tuple[dict[str, object], dict[str, object]]:
    alignment = verified_alignment()
    hardware_axes = {}
    simulation_axes = {}
    for axis in JOINT_AXES:
        item = alignment.axes[axis]
        real_delta = -10.0
        sim_delta = real_delta * alignment.real_input_scale / item.sign
        hardware_axes[axis] = {
            "reference_mm": item.real_reference,
            "observed_delta_mm": real_delta,
            "probe_pass": True,
            "endpoints": {
                "observed_min_mm": item.real_min,
                "observed_max_mm": item.real_max,
                "pass": True,
            },
        }
        simulation_axes[axis] = {
            "reference_m": item.sim_reference,
            "observed_delta_m": sim_delta,
            "min_m": item.sim_min,
            "max_m": item.sim_max,
            "probe_pass": True,
            "limits_pass": True,
        }
    return (
        {"schema_version": "1.0", "status": "COMPLETE", "axis_results": hardware_axes},
        {"schema_version": "1.0", "status": "COMPLETE", "axes": simulation_axes},
    )


def test_complete_paired_evidence_builds_verified_profile() -> None:
    hardware, simulation = _evidence()
    candidate = build_verified_alignment_candidate(
        verified_alignment(),
        hardware,
        simulation,
        alignment_id="OT2-TEST-PAIRED",
        hardware_evidence_label="hardware.json",
        simulation_evidence_label="simulation.json",
    )
    assert candidate.verified is True
    assert candidate.real_to_sim("Y", 200.0) == pytest.approx(-0.02)
    assert "hardware.json" in candidate.axes["X"].evidence


def test_missing_endpoint_replay_fails_closed() -> None:
    hardware, simulation = _evidence()
    hardware["axis_results"]["Z"]["endpoints"] = None
    with pytest.raises(JointAlignmentError, match="endpoints must be an object"):
        build_verified_alignment_candidate(
            verified_alignment(),
            hardware,
            simulation,
            alignment_id="OT2-TEST-PAIRED",
            hardware_evidence_label="hardware.json",
            simulation_evidence_label="simulation.json",
        )


def test_candidate_cli_never_overwrites_active_config(tmp_path: Path, capsys) -> None:
    config = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    config["joint_alignment"] = verified_alignment_mapping()
    config_path = tmp_path / "matterix.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    hardware, simulation = _evidence()
    hardware_path = tmp_path / "hardware.json"
    simulation_path = tmp_path / "simulation.json"
    hardware_path.write_text(json.dumps(hardware), encoding="utf-8")
    simulation_path.write_text(json.dumps(simulation), encoding="utf-8")
    original = config_path.read_bytes()
    assert (
        main(
            [
                "build-candidate",
                "--config",
                str(config_path),
                "--hardware-evidence",
                str(hardware_path),
                "--simulation-evidence",
                str(simulation_path),
                "--alignment-id",
                "OT2-TEST-PAIRED",
                "--output",
                str(config_path),
            ]
        )
        == 1
    )
    assert config_path.read_bytes() == original
    assert "never overwritten" in capsys.readouterr().err


def test_candidate_cli_writes_separate_validated_config(tmp_path: Path) -> None:
    config = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    config["joint_alignment"] = copy.deepcopy(verified_alignment_mapping())
    config_path = tmp_path / "matterix.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    hardware, simulation = _evidence()
    hardware_path = tmp_path / "hardware.json"
    simulation_path = tmp_path / "simulation.json"
    hardware_path.write_text(json.dumps(hardware), encoding="utf-8")
    simulation_path.write_text(json.dumps(simulation), encoding="utf-8")
    destination = tmp_path / "candidate.json"
    assert (
        main(
            [
                "build-candidate",
                "--config",
                str(config_path),
                "--hardware-evidence",
                str(hardware_path),
                "--simulation-evidence",
                str(simulation_path),
                "--alignment-id",
                "OT2-TEST-PAIRED",
                "--output",
                str(destination),
            ]
        )
        == 0
    )
    assert json.loads(destination.read_text(encoding="utf-8"))["joint_alignment"]["alignment_id"] == ("OT2-TEST-PAIRED")
    assert destination.with_suffix(".json.sha256").is_file()
