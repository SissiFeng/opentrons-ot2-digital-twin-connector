"""Inspect and derive OT-2 joint-level real-to-simulation alignment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .alignment import AxisAlignment, JointAlignmentError, derive_sign_and_offset
from .alignment_evidence import alignment_as_mapping, build_verified_alignment_candidate
from .asset import MatterixAssetConfigurationError, OT2MatterixAssetConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Validate and summarize the six-axis alignment table.")
    check.add_argument("--config", default="config/matterix_ot2.json")
    check.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    check.add_argument("--strict", action="store_true", help="Exit nonzero until X/Y/Z/A are verified.")

    derive = subparsers.add_parser("derive", help="Derive sign and offset from one paired safe movement.")
    derive.add_argument("--real-reference", type=float, required=True, help="Real OT-2 reference reading in mm.")
    derive.add_argument("--sim-reference", type=float, required=True, help="Simulation reference reading in m.")
    derive.add_argument("--real-delta", type=float, required=True, help="Observed real movement delta in mm.")
    derive.add_argument("--sim-delta", type=float, required=True, help="Observed simulation movement delta in m.")
    derive.add_argument("--json", action="store_true", help="Emit a machine-readable result.")

    promote = subparsers.add_parser(
        "build-candidate",
        help="Combine complete real and simulation evidence into a separate candidate config.",
    )
    promote.add_argument("--config", default="config/matterix_ot2.json")
    promote.add_argument("--hardware-evidence", required=True)
    promote.add_argument("--simulation-evidence", required=True)
    promote.add_argument("--alignment-id", required=True)
    promote.add_argument("--output", required=True)
    return parser


def _axis_mapping(item: AxisAlignment) -> dict[str, object]:
    return {
        "axis": item.axis,
        "mode": item.mode,
        "status": item.status,
        "sim_joint": item.sim_joint,
        "real_reference_mm": item.real_reference,
        "sim_reference_m": item.sim_reference,
        "offset_m": item.offset,
        "sign": item.sign,
        "real_min_mm": item.real_min,
        "real_max_mm": item.real_max,
        "sim_min_m": item.sim_min,
        "sim_max_m": item.sim_max,
        "evidence": item.evidence,
        "pass": item.verified if item.mode == "JOINT" else item.status == "NOT_MODELED",
    }


def _check(args: argparse.Namespace) -> int:
    try:
        asset = OT2MatterixAssetConfig.from_file(Path(args.config))
    except MatterixAssetConfigurationError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    alignment = asset.joint_alignment
    rows = [_axis_mapping(item) for item in alignment.axes.values()]
    payload = {
        "alignment_id": alignment.alignment_id,
        "equation": alignment.equation,
        "verified": alignment.verified,
        "axes": rows,
    }
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        headings = (
            "axis",
            "mode",
            "status",
            "sign",
            "offset_m",
            "real_min_mm",
            "real_max_mm",
            "sim_min_m",
            "sim_max_m",
        )
        sys.stdout.write("\t".join(headings) + "\n")
        for row in rows:
            sys.stdout.write("\t".join(str(row[name]) for name in headings) + "\n")
        state = "VERIFIED" if alignment.verified else "UNVERIFIED - physical measurements still required"
        sys.stdout.write(f"Alignment: {state}\n")
    return 0 if alignment.verified or not args.strict else 1


def _derive(args: argparse.Namespace) -> int:
    try:
        sign, offset = derive_sign_and_offset(
            real_reference=args.real_reference,
            sim_reference=args.sim_reference,
            real_delta=args.real_delta,
            sim_delta=args.sim_delta,
        )
    except JointAlignmentError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    payload = {"sign": sign, "offset_m": offset, "equation": "q_real_m = sign * q_sim_m + offset_m"}
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"sign={sign}\noffset_m={offset:.9g}\n{payload['equation']}\n")
    return 0


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"Unable to load {label} from {path}: {error}"
        raise JointAlignmentError(msg) from error
    if not isinstance(value, dict):
        msg = f"{label} root must be an object"
        raise JointAlignmentError(msg)
    return value


def _build_candidate(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    hardware_path = Path(args.hardware_evidence)
    simulation_path = Path(args.simulation_evidence)
    destination = Path(args.output)
    if destination.resolve() == config_path.resolve():
        sys.stderr.write("error: --output must be a separate candidate file; the active config is never overwritten\n")
        return 1
    try:
        asset = OT2MatterixAssetConfig.from_file(config_path)
        config_mapping = _json_object(config_path, "Matterix config")
        hardware = _json_object(hardware_path, "hardware evidence")
        simulation = _json_object(simulation_path, "simulation evidence")
        candidate = build_verified_alignment_candidate(
            asset.joint_alignment,
            hardware,
            simulation,
            alignment_id=args.alignment_id,
            hardware_evidence_label=str(hardware_path),
            simulation_evidence_label=str(simulation_path),
        )
        config_mapping["joint_alignment"] = alignment_as_mapping(candidate)
        OT2MatterixAssetConfig.from_mapping(config_mapping)
    except (JointAlignmentError, MatterixAssetConfigurationError) as error:
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
        f"candidate={destination}\nsha256={digest}\nalignment_id={candidate.alignment_id}\n"
        "active_config_unchanged=true\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the joint-alignment inspection utility."""
    args = _parser().parse_args(argv)
    if args.command == "check":
        return _check(args)
    if args.command == "derive":
        return _derive(args)
    return _build_candidate(args)


if __name__ == "__main__":
    raise SystemExit(main())
