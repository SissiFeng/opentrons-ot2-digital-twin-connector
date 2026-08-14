"""Run the automated real OT-2 joint calibration pipeline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .calibration_pipeline import (
    HardwareCalibrationError,
    HardwareCalibrationPlan,
    report_summary,
    run_remote_pipeline,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-plan", help="Validate the reviewed plan without connecting to hardware.")
    validate.add_argument("--plan", default="config/ot2_joint_calibration_plan.json")
    validate.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run", help="Home and measure X/Y/Z/A through the real connector.")
    run.add_argument("--robot", required=True, metavar="HOST:PORT")
    run.add_argument("--plan", default="config/ot2_joint_calibration_plan.json")
    run.add_argument("--output-dir", default="artifacts/ot2-joint-calibration")
    run.add_argument("--exercise-approved-endpoints", action="store_true")
    run.add_argument("--confirm-cleared-deck", action="store_true", required=True)
    run.add_argument("--confirm-operator-present", action="store_true", required=True)
    run.add_argument("--confirm-emergency-stop-accessible", action="store_true", required=True)
    return parser


def _validate_plan(args: argparse.Namespace) -> int:
    plan = HardwareCalibrationPlan.from_file(args.plan)
    payload = {"valid": True, "plan_sha256": plan.sha256, "plan": plan.as_mapping()}
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(f"Plan valid: {plan.sha256}\n")
    return 0


def _run(args: argparse.Namespace) -> int:
    plan = HardwareCalibrationPlan.from_file(args.plan)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = Path(args.output_dir) / f"ot2-joint-calibration-{timestamp}.json"
    try:
        evidence = asyncio.run(
            run_remote_pipeline(
                args.robot,
                plan,
                evidence_path,
                exercise_endpoints=args.exercise_approved_endpoints,
            )
        )
    except Exception as error:  # noqa: BLE001 - report the durable evidence path for any transport failure
        sys.stderr.write(f"Calibration failed: {error}\nEvidence: {evidence_path}\n")
        return 1
    sys.stdout.write(report_summary(evidence))
    sys.stdout.write(f"evidence={evidence_path}\n")
    sys.stdout.write(f"sha256={evidence_path}.sha256\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run plan validation or the physical calibration state machine."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-plan":
            return _validate_plan(args)
        return _run(args)
    except HardwareCalibrationError as error:
        sys.stderr.write(f"error: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
