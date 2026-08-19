# Matterix P0 Acceptance Governance

## Decision

The team has not assigned the accountable owners yet. The ownership fields
therefore remain `UNASSIGNED` by design.

This does not block implementation or local validation of the P0 gates. It
does block all of the following:

- changing a revision manifest from `DRAFT` to `RELEASE_CANDIDATE`;
- issuing a final `PASS` acceptance report;
- creating a baseline tag;
- authorizing physical OT-2 motion.

## Required roles

| Role | Current name | Current status | Authority |
|---|---|---|---|
| Cross-repository release owner | `UNASSIGNED` | `UNASSIGNED` | Owns the revision manifest and release verdict |
| Backup release owner | `UNASSIGNED` | `UNASSIGNED` | Covers the release owner and reviews rollback evidence |
| Matterix framework maintainer | `UNASSIGNED` | `UNASSIGNED` | Approves runtime and Python configuration changes |
| Matterix asset maintainer | `UNASSIGNED` | `UNASSIGNED` | Approves asset metadata, hashes, and physical asset definitions |
| Connector maintainer | `UNASSIGNED` | `UNASSIGNED` | Approves connector contracts and evidence schemas |
| Robot safety owner | `UNASSIGNED` | `UNASSIGNED` | Is the only role that may approve physical motion |

Every implementation issue must still record an assignee, exact repository,
base commit, dependencies, command, inputs, thresholds, artifact location, and
target evidence level. Unknown human ownership must be written as
`UNASSIGNED`; it must never be guessed.

## Evidence location

Acceptance evidence belongs under:

```text
artifacts/matterix-acceptance/<manifest_id>/<gate_id>/
```

Each report must include the command, repository commits, asset hashes,
runtime fingerprint, checks, metrics, verdict, and sign-off state. Artifacts
must be content-hashed before a report can become final. Manifest and report
artifact paths are relative to the common workspace directory that contains
the three repositories; pass that directory as `--artifact-root` so the
validator can open every file and recompute its SHA-256.

## P0 acceptance commands

### Gate 1: governance and schema contract

Run from `opentrons-ot2-digital-twin-connector`:

```bash
uv run python scripts/validate_matterix_acceptance.py \
  --manifest config/matterix_revision_manifest.json \
  --report config/matterix_acceptance_report.example.json
```

Required result:

```text
MATTERIX_ACCEPTANCE_SCHEMA_PASS
```

The checked manifest intentionally remains `DRAFT` while any repository is
dirty, any contract identifier is a placeholder, simulation evidence is
missing, or any required owner is unassigned.

### Gate 2: Matterix CPU unit tests

Run from `matterix-internal` with a Python environment containing the packages
in `requirements-test.txt` and the Matterix runtime dependencies:

```bash
MANIFEST_ID=matterix-ot2-isaac3-pr50-2026-08-19-draft
mkdir -p "artifacts/matterix-acceptance/${MANIFEST_ID}/cpu-unit-tests"
python scripts/run_cpu_unit_tests.py \
  --junitxml="artifacts/matterix-acceptance/${MANIFEST_ID}/cpu-unit-tests/junit.xml"
```

Required result:

```text
MATTERIX_CPU_UNIT_TESTS_PASS
```

This is `LOCAL_TESTED` evidence only. It does not replace the separate
Isaac/PhysX GPU smoke gate.

### Gate 3: PR #50 Isaac Lab 3.0 runtime fingerprint and simulation smoke

First capture the host and package fingerprint from `matterix-internal` on the
candidate Ubuntu GPU host:

```bash
MANIFEST_ID=matterix-ot2-isaac3-pr50-2026-08-19-draft
python scripts/check_compatibility.py \
  --profile matterix-isaaclab-3.0-pr50 \
  --strict \
  --output "artifacts/matterix-acceptance/${MANIFEST_ID}/runtime-fingerprint/runtime-compatibility.json"
```

Required result:

```text
MATTERIX_COMPATIBILITY_PASS
```

The artifact must contain `compatible: true`. Missing GPU/runtime components
are failures; the command never falls back to simulation. This command proves
the host/package fingerprint only. It does not import or execute Isaac/PhysX.

Then run the actual one-environment Isaac/PhysX smoke and hash its log:

```bash
MANIFEST_ID=matterix-ot2-isaac3-pr50-2026-08-19-draft
mkdir -p "artifacts/matterix-acceptance/${MANIFEST_ID}/runtime-smoke"
set -o pipefail
{
  printf 'MATTERIX_COMMIT=%s\n' "$(git rev-parse HEAD)"
  ./matterix.sh -p scripts/smoke_ot2.py \
    --task Matterix-OT2-LiquidHandler-v1 \
    --num_envs 1 \
    --steps 20 \
    --headless
} 2>&1 | tee \
  "artifacts/matterix-acceptance/${MANIFEST_ID}/runtime-smoke/ot2-smoke.log"
sha256sum \
  "artifacts/matterix-acceptance/${MANIFEST_ID}/runtime-smoke/ot2-smoke.log"
```

Required smoke marker:

```text
OT2_SMOKE_PASS
```

`SIM_RUNTIME_VERIFIED` requires both hashed artifacts. The fingerprint alone
is never runtime-compatibility or physics evidence. PR #50's reported GPU
results are relevant prior evidence, but they do not replace these hashed
commands on the exact pushed revision. PR #51's 96-tip runtime acceptance is a
separate promotion dependency.

### Gate 4: cross-repository revision contract

Run from `opentrons-ot2-digital-twin-connector` with the adjacent Matterix
checkout:

```bash
uv run python scripts/validate_matterix_acceptance.py \
  --manifest config/matterix_revision_manifest.json \
  --report config/matterix_acceptance_report.example.json \
  --compatibility-matrix ../matterix-internal/docs/compatibility_matrix.json \
  --artifact-root ..
```

Then verify that the draft manifest pins:

- exactly three repository commits;
- the OT-2 robot articulation hash;
- the separate 96-tip composite hash;
- the connector contract ID;
- runtime, calibration, alignment, and tip-binding identities;
- the compatibility-matrix digest;
- hashed strict-fingerprint and Isaac/PhysX smoke artifacts before promotion.

Runtime artifact paths in the manifest must therefore include their repository
prefix, for example
`matterix-internal/artifacts/matterix-acceptance/<manifest_id>/runtime-smoke/ot2-smoke.log`.
For release candidates, the validator re-hashes the files, requires
`compatible: true` and the matching Matterix commit in the fingerprint, and
requires both `OT2_SMOKE_PASS` and the matching commit in the smoke log.

For a release candidate, the pinned compatibility profile must contain
`release_authorized: true`. Merely writing `SIM_RUNTIME_VERIFIED` into the
manifest cannot satisfy this gate.

The verified 96-tip composite entrypoint is:

```text
objects/racks/tip_rack/tiprack_with_tip_inst.usda
sha256=a30999b3e30b887759bdf5d0864088a2c31320a42c051922f4a8bde60f50e4bb
```

It must never be used as the OT-2 robot `asset_sha256`.

### Gate 5: asset repository validation

Run from `matterix-assets-internal` after retrieving Git LFS objects:

```bash
MANIFEST_ID=matterix-ot2-isaac3-pr50-2026-08-19-draft
git lfs pull
git lfs fsck
python scripts/validate_assets.py \
  --report "artifacts/matterix-acceptance/${MANIFEST_ID}/assets/asset-validation.json"
```

Required result:

```text
Git LFS fsck OK
MATTERIX_ASSET_VALIDATION_PASS
```

The validator checks LFS tracking and payload retrieval, metadata IDs and
provenance, entrypoint SHA-256 values, and the recursive local USD reference
closure of every metadata-listed entrypoint. Bare Isaac material-library MDL
names are treated as runtime search-path references, not repository files.

## Current local evidence

| Gate | Current result | Evidence boundary |
|---|---|---|
| Governance/schema | Passes as `DRAFT` | Owners and release approval remain unassigned |
| CPU unit tests | 58 passed locally and on final commit `7c3f404` in GitHub Actions run 32291447290 | CPU evidence only; no Isaac/PhysX claim |
| Runtime fingerprint and smoke | PR #50 reports GPU runtime success; the new fingerprint correctly rejects macOS | Hashed fingerprint, OT-2 smoke, and PR #51 exact tip-rack runtime acceptance remain pending |
| Revision manifest | Schema-valid draft with pinned matrix digest | Dirty repositories, unauthorized profile, missing or unverifiable runtime artifacts, and placeholder IDs prevent promotion |
| Asset validation | 67 LFS assets, 2 metadata files, 198 references; local and GitHub Actions run 32291104742 passed | Static/LFS evidence only, not physics acceptance |

## Promotion rule

A baseline tag may be created only when all five P0 gates pass on clean
worktrees, the compatibility profile is release-authorized, the hashed
fingerprint and Isaac/PhysX smoke artifacts are attached to the manifest, the
exact evidence bundle is attached to the final report, and all required owners
are assigned and approved. Physical OT-2 execution additionally requires the
robot safety owner and operator checklist; a software release owner cannot
grant that authority.
