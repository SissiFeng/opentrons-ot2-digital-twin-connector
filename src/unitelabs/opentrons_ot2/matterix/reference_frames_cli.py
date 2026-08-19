"""Inspect OT-2 frame alignment or derive one candidate transform from fiducials."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .asset import MatterixAssetConfigurationError, OT2MatterixAssetConfig
from .reference_frames import ReferenceFrameAlignment, ReferenceFrameAlignmentError, derive_rigid_transform


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Validate and summarize the configured transform chain.")
    check.add_argument("--config", default="config/matterix_ot2.json")
    check.add_argument("--json", action="store_true")
    check.add_argument("--strict", action="store_true", help="Exit nonzero until the chain is VERIFIED.")

    derive = subparsers.add_parser("derive", help="Fit one parent-from-child transform from paired fiducials.")
    derive.add_argument("--input", required=True, help="JSON file containing paired source/target points in metres.")
    derive.add_argument("--output", help="Optional path for the candidate JSON evidence artifact.")

    candidate = subparsers.add_parser(
        "build-candidate",
        help="Combine two reviewed transform artifacts into a separate candidate config.",
    )
    candidate.add_argument("--config", default="config/matterix_ot2.json")
    candidate.add_argument("--world-from-base", required=True)
    candidate.add_argument("--base-from-deck", required=True)
    candidate.add_argument("--alignment-id", required=True)
    candidate.add_argument("--output", required=True)
    return parser


def _check(args: argparse.Namespace) -> int:
    try:
        asset = OT2MatterixAssetConfig.from_file(args.config)
    except MatterixAssetConfigurationError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    profile = asset.reference_frame_alignment
    payload = {
        "alignment_id": profile.alignment_id,
        "status": profile.status,
        "verified": profile.verified,
        "world_from_base": profile.world_from_base.as_mapping(),
        "base_from_deck": profile.base_from_deck.as_mapping(),
        "world_from_deck": profile.world_from_deck.as_mapping(),
        "evidence": profile.evidence,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(
            f"alignment_id={profile.alignment_id}\n"
            f"status={profile.status}\n"
            f"world_from_deck={json.dumps(profile.world_from_deck.as_mapping(), sort_keys=True)}\n"
        )
    return 0 if profile.verified or not args.strict else 1


def _load_observations(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"Unable to load frame observations from {path}: {error}"
        raise ReferenceFrameAlignmentError(msg) from error
    if not isinstance(value, dict):
        msg = "Frame observation root must be an object"
        raise ReferenceFrameAlignmentError(msg)
    allowed = {
        "schema_version",
        "parent_frame",
        "child_frame",
        "source_points_m",
        "target_points_m",
        "max_rms_error_m",
        "evidence",
    }
    unknown = set(value) - allowed
    if unknown:
        msg = f"Frame observations contain unknown fields {sorted(unknown)}"
        raise ReferenceFrameAlignmentError(msg)
    if value.get("schema_version") != "1.0":
        msg = "Frame observation schema_version must be '1.0'"
        raise ReferenceFrameAlignmentError(msg)
    if not str(value.get("evidence", "")).strip():
        msg = "Frame observations must describe their measurement evidence"
        raise ReferenceFrameAlignmentError(msg)
    return value


def _derive(args: argparse.Namespace) -> int:
    source = Path(args.input)
    try:
        observations = _load_observations(source)
        transform, metrics = derive_rigid_transform(
            observations["source_points_m"],  # type: ignore[arg-type]
            observations["target_points_m"],  # type: ignore[arg-type]
            parent_frame=str(observations["parent_frame"]),
            child_frame=str(observations["child_frame"]),
            max_rms_error_m=float(observations["max_rms_error_m"]),
        )
    except (KeyError, TypeError, ValueError, ReferenceFrameAlignmentError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    input_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    payload = {
        "schema_version": "1.0",
        "status": "CANDIDATE_REQUIRES_REVIEW",
        "transform": transform.as_mapping(),
        "metrics": metrics,
        "source_evidence": str(observations["evidence"]),
        "input_path": str(source),
        "input_sha256": input_sha256,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded, encoding="utf-8")
        sys.stdout.write(f"candidate={destination}\n")
    else:
        sys.stdout.write(encoded)
    return 0


def _candidate_transform(path: Path, label: str) -> tuple[dict[str, object], str]:
    value = _load_observations_candidate(path, label)
    transform = value.get("transform")
    if not isinstance(transform, dict):
        msg = f"{label} candidate must contain a transform object"
        raise ReferenceFrameAlignmentError(msg)
    return transform, hashlib.sha256(path.read_bytes()).hexdigest()


def _load_observations_candidate(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"Unable to load {label} candidate from {path}: {error}"
        raise ReferenceFrameAlignmentError(msg) from error
    if not isinstance(value, dict) or value.get("status") != "CANDIDATE_REQUIRES_REVIEW":
        msg = f"{label} must be a CANDIDATE_REQUIRES_REVIEW artifact produced by derive"
        raise ReferenceFrameAlignmentError(msg)
    return value


def _build_candidate(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    destination = Path(args.output)
    if destination.resolve() == config_path.resolve():
        sys.stderr.write("error: --output must be separate; the active config is never overwritten\n")
        return 1
    alignment_id = args.alignment_id.strip()
    if not alignment_id or alignment_id.upper().startswith("UNVERIFIED"):
        sys.stderr.write("error: --alignment-id must identify reviewed evidence and cannot start with UNVERIFIED\n")
        return 1
    world_path = Path(args.world_from_base)
    deck_path = Path(args.base_from_deck)
    try:
        OT2MatterixAssetConfig.from_file(config_path)
        config_mapping = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(config_mapping, dict):
            msg = "Matterix config root must be an object"
            raise ReferenceFrameAlignmentError(msg)
        world_from_base, world_digest = _candidate_transform(world_path, "world-from-base")
        base_from_deck, deck_digest = _candidate_transform(deck_path, "base-from-deck")
        profile_mapping = {
            "schema_version": "1.0",
            "alignment_id": alignment_id,
            "status": "VERIFIED",
            "world_from_base": world_from_base,
            "base_from_deck": base_from_deck,
            "evidence": (
                f"REVIEWED_FIDUCIAL_FITS: {world_path} sha256={world_digest}; {deck_path} sha256={deck_digest}"
            ),
        }
        ReferenceFrameAlignment.from_mapping(profile_mapping)
        config_mapping["reference_frame_alignment"] = profile_mapping
        OT2MatterixAssetConfig.from_mapping(config_mapping)
    except (
        MatterixAssetConfigurationError,
        ReferenceFrameAlignmentError,
        OSError,
        json.JSONDecodeError,
    ) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    payload = (json.dumps(config_mapping, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{digest}  {destination.name}\n",
        encoding="utf-8",
    )
    sys.stdout.write(
        f"candidate={destination}\nsha256={digest}\nalignment_id={alignment_id}\nactive_config_unchanged=true\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run frame-alignment inspection or derivation."""
    args = _parser().parse_args(argv)
    if args.command == "check":
        return _check(args)
    if args.command == "derive":
        return _derive(args)
    return _build_candidate(args)


if __name__ == "__main__":
    raise SystemExit(main())
