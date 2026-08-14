# OT-2 Joint-Level Sim-to-Real Alignment

This procedure aligns the four physical joints represented by the Matterix OT-2
USD with the real robot. It does not claim that the world, deck, or base frames
are aligned.

The executable contract is:

```text
q_real_m = sign * q_sim_m + offset_m
sign in {-1, +1}
```

Real OT-2 connector readings are in millimetres and are converted to metres
before the equation is applied.

## Axis boundary

| Real axis | Matterix representation | Required alignment |
|---|---|---|
| X | `PrismaticJointMiddleBar` | reference, sign, range |
| Y | `PrismaticJointPipetteHolder` | reference, sign, range |
| Z | `PrismaticJointLeftPipette` | reference, sign, range |
| A | `PrismaticJointRightPipette` | reference, sign, range |
| B | aspiration/dispensing semantics | no USD joint |
| C | aspiration/dispensing semantics | no USD joint |

B and C remain real plunger axes in the connector, but the legacy Orbit USD
does not model them as articulation joints. The twin represents their liquid
effect through bounded Matterix semantics. They must not be addressed as action
dimensions 4 and 5.

## Current evidence boundary

`config/matterix_ot2.json` records source defaults from the connector and the
USD, but leaves X/Y/Z/A `UNVERIFIED`. The connector refuses to translate real
motion into Matterix joint commands until all four are verified.

The current source data already reveals range mismatches:

- real/default X: up to 418 mm; current USD X: `-0.12...0.20 m` (320 mm span);
- real/default Y: up to 370 mm; current USD Y: `-0.18...0.18 m` (360 mm span);
- real Z/A home/default maximum: 218 mm; a fixed approved lower operating
  endpoint still needs to be selected and measured for each mount setup.

An offset cannot repair a span mismatch. Update the USD joint limits and
Matterix action clips to the approved real operating range before marking the
profile verified.

## Measurement sequence

Use a cleared deck, an operator at the robot, reduced speed, and one axis at a
time. Do not infer success from simulator motion alone.

The real-machine portion is automated. From the repository root on the laptop,
run:

```sh
sh scripts/calibrate_ot2_joints.sh <robot-host>
```

The script validates the plan, checks the connector, asks for one explicit
safety confirmation, and then performs steps 1, 2, 4, and the real-side range
capture below. It checkpoints after identity, Home, and every axis, so a failed
session still leaves a JSON evidence artifact and SHA-256 sidecar. It never
falls back to simulation.

After reviewing finite Z/A lower endpoints in
`config/ot2_joint_calibration_plan.json`, the full approved-range replay is:

```sh
sh scripts/calibrate_ot2_joints.sh <robot-host> endpoints
```

Endpoint mode validates all four approved ranges before the first Home. It does
not discover mechanical stops automatically.

1. Pin the robot serial, pipette configuration, connector config ID, OT-2 USD
   hash, and software revisions in the evidence record.
2. Home `XYZA` through the connector. Record the returned X/Y/Z/A values as
   `real_reference`.
3. Reset the Matterix environment at its intended aligned home pose. Record its
   X/Y/Z/A joint tensor as `sim_reference`.
4. From those safe reference poses, command a small movement away from the
   positive limit switch on one real axis and the intended corresponding
   simulation joint. Record both signed deltas.
5. Derive sign and offset. For example:

   ```sh
   uv run ot2-joint-alignment derive \
     --real-reference 353 --sim-reference 0.18 \
     --real-delta -10 --sim-delta -0.01
   ```

   A magnitude mismatch is treated as an asset-scale error, not rounded away.
6. Establish `real_min/max` as the explicitly approved operating interval. For
   Z/A this must account for the installed pipette and deck collision envelope;
   do not drive blindly to a mechanical stop just to obtain a number.
7. Author matching `sim_min/max` in the USD and Matterix action clip. Both real
   endpoints must map to the two simulation endpoints under the same equation.
8. Capture the matching simulation evidence artifact. It must declare
   `schema_version=1.0`, `status=COMPLETE`, contain exactly X/Y/Z/A, and record
   `reference_m`, `observed_delta_m`, `min_m`, `max_m`, `probe_pass=true`, and
   `limits_pass=true` per axis.
9. Build a separate full candidate configuration; do not edit or overwrite the
   active config directly:

   ```sh
   uv run ot2-joint-alignment build-candidate \
     --hardware-evidence <hardware.json> \
     --simulation-evidence <simulation.json> \
     --alignment-id <reviewed-id> \
     --output <candidate-config.json>
   ```

   The command requires complete endpoint evidence on both sides, derives each
   sign/offset, validates both endpoint equalities, embeds evidence hashes, and
   writes a SHA-256 sidecar.
10. Review the candidate, then run:

   ```sh
   uv run ot2-joint-alignment check --strict --json
   uv run ot2-matterix-preflight --strict
   ```

11. On Ubuntu/Isaac Sim, replay reference, interior, and both endpoint values
    per joint. Confirm numeric position, visible physical direction, and limit
    rejection before enabling connector-driven workflows.

## Acceptance table

For each X/Y/Z/A joint, retain this evidence:

```text
axis
sim_joint
real_reference_mm
sim_reference_m
sign
offset_m
real_min_mm
real_max_mm
sim_min_m
sim_max_m
robot_serial
pipette_setup
usd_sha256
measurement_run_id
PASS/FAIL
```

Passing requires reference equality, direction equality, both endpoint
equalities, and rejection immediately outside both approved ranges.
