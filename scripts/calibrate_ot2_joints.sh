#!/bin/sh
# Run the complete operator-gated OT-2 joint measurement session from a laptop.
# Usage: sh scripts/calibrate_ot2_joints.sh <robot-host> [probe|endpoints] [plan.json]

set -eu

HOST="${1:?Usage: $0 <robot-host> [probe|endpoints] [plan.json]}"
MODE="${2:-probe}"
PLAN="${3:-config/ot2_joint_calibration_plan.json}"

case "$MODE" in
    probe|endpoints) ;;
    *) echo "ERROR: mode must be probe or endpoints"; exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
    echo "ERROR: uv is required on the laptop running this pipeline."
    exit 1
fi

echo "Validating reviewed plan: $PLAN"
uv run ot2-joint-calibrate validate-plan --plan "$PLAN"

echo "Checking connector health on: $HOST"
sh scripts/verify_ot2.sh "$HOST"

echo ""
echo "This pipeline will home X/Y/Z/A and move one axis at a time at <=20 mm/s."
echo "Every probe moves away from the positive limit switch and returns to home."
if [ "$MODE" = "endpoints" ]; then
    echo "Endpoint mode will also traverse every approved min/max in $PLAN."
    echo "Z and A must have reviewed finite approved_min_mm values."
fi
echo ""
echo "Before continuing confirm all three conditions:"
echo "  1. The deck and gantry travel area are clear."
echo "  2. An operator is physically present."
echo "  3. Emergency-stop access is unobstructed."
printf "Type CALIBRATE to begin: "
read -r CONFIRMATION
if [ "$CONFIRMATION" != "CALIBRATE" ]; then
    echo "Cancelled before hardware motion."
    exit 1
fi

if [ "$MODE" = "endpoints" ]; then
    uv run ot2-joint-calibrate run \
        --robot "${HOST}:50051" \
        --plan "$PLAN" \
        --exercise-approved-endpoints \
        --confirm-cleared-deck \
        --confirm-operator-present \
        --confirm-emergency-stop-accessible
else
    uv run ot2-joint-calibrate run \
        --robot "${HOST}:50051" \
        --plan "$PLAN" \
        --confirm-cleared-deck \
        --confirm-operator-present \
        --confirm-emergency-stop-accessible
fi
