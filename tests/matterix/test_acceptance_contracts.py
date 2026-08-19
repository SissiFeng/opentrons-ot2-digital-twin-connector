"""Tests for cross-repository Matterix acceptance contracts."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).parents[2]
VALIDATOR = REPOSITORY_ROOT / "scripts" / "validate_matterix_acceptance.py"
MANIFEST = REPOSITORY_ROOT / "config" / "matterix_revision_manifest.json"
REPORT = REPOSITORY_ROOT / "config" / "matterix_acceptance_report.example.json"


def _run_validator(
    manifest: Path,
    report: Path | None = None,
    compatibility_matrix: Path | None = None,
    artifact_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(VALIDATOR), "--manifest", str(manifest)]
    if report is not None:
        command.extend(("--report", str(report)))
    if compatibility_matrix is not None:
        command.extend(("--compatibility-matrix", str(compatibility_matrix)))
    if artifact_root is not None:
        command.extend(("--artifact-root", str(artifact_root)))
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_checked_examples_validate() -> None:
    result = _run_validator(MANIFEST, REPORT)

    assert result.returncode == 0, result.stderr
    assert "MATTERIX_ACCEPTANCE_SCHEMA_PASS" in result.stdout


def test_robot_hash_cannot_be_replaced_by_tip_rack_hash(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assets_by_role = {asset["role"]: asset for asset in manifest["assets"]}
    tip_hash = assets_by_role["CONSUMABLE_96_TIP_COMPOSITE"]["sha256"]
    assets_by_role["ROBOT_ARTICULATION"]["sha256"] = tip_hash
    manifest["contracts"]["robot_asset_sha256"] = tip_hash
    changed_manifest = tmp_path / "manifest.json"
    changed_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_validator(changed_manifest)

    assert result.returncode == 1
    assert "robot articulation hash must not be the 96-tip composite hash" in result.stderr


def test_release_candidate_rejects_dirty_or_unassigned_inputs(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["status"] = "RELEASE_CANDIDATE"
    changed_manifest = tmp_path / "manifest.json"
    changed_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_validator(changed_manifest)

    assert result.returncode == 1
    assert "require clean repositories" in result.stderr
    assert "require approved ownership roles" in result.stderr
    assert "require SIM_RUNTIME_VERIFIED" in result.stderr
    assert "require the compatibility matrix input" in result.stderr
    assert "require runtime.release_authorized=true" in result.stderr
    assert "require runtime.fingerprint_artifact" in result.stderr
    assert "require runtime.smoke_artifact" in result.stderr


def test_release_candidate_rejects_unassigned_names_with_approved_status(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["status"] = "RELEASE_CANDIDATE"
    for repository in manifest["repositories"]:
        repository["dirty"] = False
    manifest["runtime"]["evidence_status"] = "SIM_RUNTIME_VERIFIED"
    manifest["contracts"]["calibration_id"] = "calibration-reviewed"
    manifest["contracts"]["joint_alignment_id"] = "joint-alignment-reviewed"
    manifest["contracts"]["tip_binding_id"] = "tip-binding-reviewed"
    for owner in manifest["ownership"].values():
        owner["status"] = "APPROVED"
    changed_manifest = tmp_path / "manifest.json"
    changed_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_validator(changed_manifest)

    assert result.returncode == 1
    assert "require approved ownership roles" in result.stderr


def test_pass_report_requires_manifest_owners_and_hashed_artifacts(tmp_path: Path) -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["verdict"] = "PASS"
    report["sign_offs"] = [
        {
            "role": "connector_maintainer",
            "name": "Invented Person",
            "status": "APPROVED",
        }
    ]
    changed_report = tmp_path / "report.json"
    changed_report.write_text(json.dumps(report), encoding="utf-8")

    result = _run_validator(MANIFEST, changed_report)

    assert result.returncode == 1
    assert "at least one content-hashed artifact" in result.stderr
    assert "must match an approved manifest owner" in result.stderr


def test_pass_report_rehashes_artifact_content(tmp_path: Path) -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    report["verdict"] = "PASS"
    report["artifacts"] = [{"path": "missing.json", "sha256": "0" * 64}]
    report["sign_offs"] = [
        {
            "role": "connector_maintainer",
            "name": "UNASSIGNED",
            "status": "APPROVED",
        }
    ]
    changed_report = tmp_path / "report.json"
    changed_report.write_text(json.dumps(report), encoding="utf-8")

    result = _run_validator(MANIFEST, changed_report, artifact_root=tmp_path)

    assert result.returncode == 1
    assert "report.artifacts[0] file does not exist" in result.stderr


def test_release_candidate_verifies_runtime_artifact_contents(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["status"] = "RELEASE_CANDIDATE"
    for repository in manifest["repositories"]:
        repository["dirty"] = False
    for role, owner in manifest["ownership"].items():
        owner.update({"name": f"Assigned {role}", "status": "APPROVED"})
    manifest["runtime"]["evidence_status"] = "SIM_RUNTIME_VERIFIED"
    manifest["runtime"]["release_authorized"] = True
    manifest["contracts"]["calibration_id"] = "calibration-reviewed"
    manifest["contracts"]["joint_alignment_id"] = "joint-alignment-reviewed"
    manifest["contracts"]["tip_binding_id"] = "tip-binding-reviewed"

    matrix = {
        "profiles": [
            {
                "profile_id": "matterix-isaaclab-3.0-pr50",
                "release_authorized": True,
                "python_version_pattern": "3.12.*",
                "packages": {
                    "isaaclab": "3.0.0b2*",
                    "isaacsim": "6.0.1*",
                    "torch": "2.10.*+cu128",
                },
            }
        ]
    }
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    manifest["runtime"]["compatibility_matrix"]["sha256"] = _sha256(matrix_path)

    fingerprint = {
        "compatible": True,
        "profile_id": "matterix-isaaclab-3.0-pr50",
        "fingerprint": {
            "python": "3.12.3",
            "packages": {
                "isaaclab": "3.0.0b2.post1",
                "isaacsim": "6.0.1.0",
                "torch": "2.10.0+cu128",
            },
            "repository": {"commit": "0" * 40, "dirty": False},
        },
    }
    fingerprint_path = tmp_path / "fingerprint.json"
    fingerprint_path.write_text(json.dumps(fingerprint), encoding="utf-8")
    smoke_path = tmp_path / "smoke.log"
    smoke_path.write_text(f"MATTERIX_COMMIT={'0' * 40}\nOT2_SMOKE_PASS\n", encoding="utf-8")
    manifest["runtime"]["fingerprint_artifact"] = {
        "path": fingerprint_path.name,
        "sha256": _sha256(fingerprint_path),
    }
    manifest["runtime"]["smoke_artifact"] = {
        "path": smoke_path.name,
        "sha256": _sha256(smoke_path),
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_validator(
        manifest_path,
        compatibility_matrix=matrix_path,
        artifact_root=tmp_path,
    )

    assert result.returncode == 1
    assert "fingerprint_artifact Matterix commit does not match" in result.stderr
    assert "smoke_artifact Matterix commit does not match" in result.stderr
