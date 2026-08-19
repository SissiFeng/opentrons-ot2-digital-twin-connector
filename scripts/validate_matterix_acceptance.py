#!/usr/bin/env python3

"""Validate Matterix revision manifests and acceptance reports."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPOSITORY_ROOT / "src" / "unitelabs" / "opentrons_ot2" / "contracts"
REVISION_SCHEMA = CONTRACT_ROOT / "matterix_revision_manifest.schema.json"
REPORT_SCHEMA = CONTRACT_ROOT / "matterix_acceptance_report.schema.json"
EXPECTED_REPOSITORIES = {
    "opentrons-ot2-digital-twin-connector",
    "matterix-internal",
    "matterix-assets-internal",
}
PLACEHOLDER_PREFIXES = ("UNCONFIRMED", "UNVERIFIED", "REPLACE_")
REQUIRED_OWNER_ROLES = {
    "cross_repository_release_owner",
    "backup_owner",
    "framework_maintainer",
    "asset_maintainer",
    "connector_maintainer",
    "robot_safety_owner",
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_schema(instance: dict[str, Any], schema_path: Path) -> list[str]:
    schema = _read_json(schema_path)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [error.message for error in sorted(validator.iter_errors(instance), key=lambda item: list(item.path))]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_artifact(
    artifact: dict[str, Any], artifact_root: Path | None, label: str, errors: list[str]
) -> Path | None:
    if artifact_root is None:
        errors.append(f"{label} requires --artifact-root for content verification")
        return None
    resolved_root = artifact_root.resolve()
    artifact_path = (resolved_root / artifact["path"]).resolve()
    try:
        artifact_path.relative_to(resolved_root)
    except ValueError:
        errors.append(f"{label} path escapes the artifact root")
        return None
    if not artifact_path.is_file():
        errors.append(f"{label} file does not exist: {artifact['path']}")
        return None
    actual_hash = _sha256(artifact_path)
    if actual_hash != artifact["sha256"]:
        errors.append(f"{label} sha256 does not match the file content")
        return None
    return artifact_path


def _normalized_pattern(value: str) -> str:
    return value.replace("x", "*")


def validate_manifest(
    manifest: dict[str, Any],
    compatibility_matrix: dict[str, Any] | None = None,
    compatibility_matrix_sha256: str | None = None,
    artifact_root: Path | None = None,
) -> list[str]:
    """Validate schema and cross-field release invariants."""
    errors = _validate_schema(manifest, REVISION_SCHEMA)
    if errors:
        return errors

    repository_names = [repository["name"] for repository in manifest["repositories"]]
    if set(repository_names) != EXPECTED_REPOSITORIES or len(repository_names) != len(set(repository_names)):
        errors.append("repositories must contain each required repository exactly once")

    role_counts = {
        role: sum(asset["role"] == role for asset in manifest["assets"])
        for role in ("ROBOT_ARTICULATION", "CONSUMABLE_96_TIP_COMPOSITE")
    }
    if any(count != 1 for count in role_counts.values()):
        errors.append("assets must contain each required asset role exactly once")
    assets_by_role = {asset["role"]: asset for asset in manifest["assets"]}
    robot = assets_by_role.get("ROBOT_ARTICULATION")
    tip_rack = assets_by_role.get("CONSUMABLE_96_TIP_COMPOSITE")
    if robot is None or tip_rack is None:
        errors.append("assets must include one robot articulation and one 96-tip composite")
    else:
        if robot["sha256"] == tip_rack["sha256"]:
            errors.append("the robot articulation hash must not be the 96-tip composite hash")
        if tip_rack.get("expected_child_count") != 96:
            errors.append("the consumable composite must declare expected_child_count=96")
        if manifest["contracts"]["robot_asset_sha256"] != robot["sha256"]:
            errors.append("contracts.robot_asset_sha256 must match the ROBOT_ARTICULATION asset")

    profile: dict[str, Any] | None = None
    profile_release_authorized: bool | None = None
    if compatibility_matrix is not None:
        declared_matrix_hash = manifest["runtime"]["compatibility_matrix"]["sha256"]
        if compatibility_matrix_sha256 != declared_matrix_hash:
            errors.append("the compatibility matrix content does not match its declared sha256")
        profiles = {profile["profile_id"]: profile for profile in compatibility_matrix.get("profiles", [])}
        profile = profiles.get(manifest["runtime"]["profile_id"])
        if profile is None:
            errors.append("the runtime profile is missing from the compatibility matrix")
        else:
            profile_release_authorized = bool(profile.get("release_authorized"))
            if manifest["runtime"]["release_authorized"] != profile_release_authorized:
                errors.append("runtime.release_authorized must match the compatibility matrix profile")
            declared_patterns = {
                "python": profile["python_version_pattern"],
                "isaac_lab": profile["packages"]["isaaclab"],
                "isaac_sim": profile["packages"]["isaacsim"],
                "torch": profile["packages"]["torch"],
            }
            for field, matrix_pattern in declared_patterns.items():
                if _normalized_pattern(manifest["runtime"][field]) != _normalized_pattern(matrix_pattern):
                    errors.append(f"runtime.{field} must match the selected compatibility profile")

    if manifest["status"] in {"RELEASE_CANDIDATE", "ACCEPTED"}:
        if any(repository["dirty"] for repository in manifest["repositories"]):
            errors.append("release candidates and accepted manifests require clean repositories")
        if any(
            owner["status"] != "APPROVED" or owner["name"] == "UNASSIGNED" for owner in manifest["ownership"].values()
        ):
            errors.append("release candidates and accepted manifests require approved ownership roles")
        if manifest["runtime"]["evidence_status"] != "SIM_RUNTIME_VERIFIED":
            errors.append("release candidates and accepted manifests require SIM_RUNTIME_VERIFIED runtime evidence")
        if compatibility_matrix is None:
            errors.append("release candidates and accepted manifests require the compatibility matrix input")
        elif profile_release_authorized is not True:
            errors.append("the compatibility matrix profile is not release-authorized")
        if not manifest["runtime"]["release_authorized"]:
            errors.append("release candidates and accepted manifests require runtime.release_authorized=true")
        for artifact_field in ("fingerprint_artifact", "smoke_artifact"):
            if artifact_field not in manifest["runtime"]:
                errors.append(f"release candidates and accepted manifests require runtime.{artifact_field}")
        if "fingerprint_artifact" in manifest["runtime"]:
            fingerprint_path = _verify_artifact(
                manifest["runtime"]["fingerprint_artifact"],
                artifact_root,
                "runtime.fingerprint_artifact",
                errors,
            )
            if fingerprint_path is not None:
                try:
                    fingerprint = _read_json(fingerprint_path)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    errors.append(f"runtime.fingerprint_artifact is not valid JSON: {exc}")
                else:
                    if fingerprint.get("compatible") is not True:
                        errors.append("runtime.fingerprint_artifact must contain compatible=true")
                    if fingerprint.get("profile_id") != manifest["runtime"]["profile_id"]:
                        errors.append("runtime.fingerprint_artifact profile_id does not match the manifest")
                    repository_fingerprint = fingerprint.get("fingerprint", {}).get("repository", {})
                    matterix_repository = next(
                        repository
                        for repository in manifest["repositories"]
                        if repository["name"] == "matterix-internal"
                    )
                    if repository_fingerprint.get("commit") != matterix_repository["commit"]:
                        errors.append("runtime.fingerprint_artifact Matterix commit does not match the manifest")
                    if repository_fingerprint.get("dirty") is not False:
                        errors.append("runtime.fingerprint_artifact must record a clean Matterix checkout")
                    if profile is not None:
                        actual = fingerprint.get("fingerprint", {})
                        expected_values = {
                            "python": (actual.get("python"), profile["python_version_pattern"]),
                            "isaaclab": (
                                actual.get("packages", {}).get("isaaclab"),
                                profile["packages"]["isaaclab"],
                            ),
                            "isaacsim": (
                                actual.get("packages", {}).get("isaacsim"),
                                profile["packages"]["isaacsim"],
                            ),
                            "torch": (
                                actual.get("packages", {}).get("torch"),
                                profile["packages"]["torch"],
                            ),
                        }
                        for field, (actual_value, expected_pattern) in expected_values.items():
                            if not isinstance(actual_value, str) or not fnmatch.fnmatchcase(
                                actual_value, expected_pattern
                            ):
                                errors.append(
                                    f"runtime.fingerprint_artifact {field} does not match the compatibility profile"
                                )
        if "smoke_artifact" in manifest["runtime"]:
            smoke_path = _verify_artifact(
                manifest["runtime"]["smoke_artifact"],
                artifact_root,
                "runtime.smoke_artifact",
                errors,
            )
            if smoke_path is not None:
                try:
                    smoke_log = smoke_path.read_text(encoding="utf-8")
                except UnicodeDecodeError as exc:
                    errors.append(f"runtime.smoke_artifact is not valid UTF-8: {exc}")
                else:
                    matterix_commit = next(
                        repository["commit"]
                        for repository in manifest["repositories"]
                        if repository["name"] == "matterix-internal"
                    )
                    if "OT2_SMOKE_PASS" not in smoke_log:
                        errors.append("runtime.smoke_artifact does not contain OT2_SMOKE_PASS")
                    if f"MATTERIX_COMMIT={matterix_commit}" not in smoke_log:
                        errors.append("runtime.smoke_artifact Matterix commit does not match the manifest")
        for field in ("calibration_id", "joint_alignment_id", "tip_binding_id"):
            value = manifest["contracts"][field]
            if value.startswith(PLACEHOLDER_PREFIXES):
                errors.append(f"release candidates and accepted manifests cannot use placeholder {field}")
    return errors


def validate_report(report: dict[str, Any], manifest: dict[str, Any], artifact_root: Path | None = None) -> list[str]:
    """Validate an acceptance report against its revision manifest."""
    errors = _validate_schema(report, REPORT_SCHEMA)
    if errors:
        return errors
    if report["manifest_id"] != manifest["manifest_id"]:
        errors.append("report.manifest_id must match the revision manifest")
    if report["verdict"] == "PASS":
        if any(check["status"] != "PASS" for check in report["checks"]):
            errors.append("a PASS report requires every check to pass")
        if any(sign_off["status"] != "APPROVED" for sign_off in report["sign_offs"]):
            errors.append("a PASS report requires every sign-off to be approved")
        if not report["artifacts"]:
            errors.append("a PASS report requires at least one content-hashed artifact")
        for index, artifact in enumerate(report["artifacts"]):
            _verify_artifact(artifact, artifact_root, f"report.artifacts[{index}]", errors)
        sign_off_roles = [sign_off["role"] for sign_off in report["sign_offs"]]
        if len(sign_off_roles) != len(set(sign_off_roles)):
            errors.append("a PASS report cannot contain duplicate sign-off roles")
        for sign_off in report["sign_offs"]:
            owner = manifest["ownership"][sign_off["role"]]
            if sign_off["name"] != owner["name"] or owner["status"] != "APPROVED":
                errors.append(f"PASS sign-off {sign_off['role']} must match an approved manifest owner")
        if report["report_scope"] == "FINAL":
            if manifest["status"] not in {"RELEASE_CANDIDATE", "ACCEPTED"}:
                errors.append("a final PASS report requires a release-candidate or accepted manifest")
            if set(sign_off_roles) != REQUIRED_OWNER_ROLES:
                errors.append("a final PASS report requires every governance role exactly once")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--compatibility-matrix",
        type=Path,
        help="Verify the manifest against the pinned Matterix compatibility matrix",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Resolve and re-hash manifest and report artifact paths below this directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = _read_json(args.manifest)
    compatibility_matrix = None
    compatibility_matrix_sha256 = None
    if args.compatibility_matrix:
        compatibility_matrix = _read_json(args.compatibility_matrix)
        compatibility_matrix_sha256 = _sha256(args.compatibility_matrix)
    errors = [
        f"manifest: {error}"
        for error in validate_manifest(
            manifest,
            compatibility_matrix,
            compatibility_matrix_sha256,
            args.artifact_root,
        )
    ]
    if args.report:
        report = _read_json(args.report)
        errors.extend(f"report: {error}" for error in validate_report(report, manifest, args.artifact_root))
    if errors:
        for error in errors:
            print(f"MATTERIX_ACCEPTANCE_ERROR: {error}", file=sys.stderr)
        return 1
    print(f"MATTERIX_ACCEPTANCE_SCHEMA_PASS manifest_id={manifest['manifest_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
