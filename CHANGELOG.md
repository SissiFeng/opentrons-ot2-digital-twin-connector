# Changelog

All notable changes to the independent
`sissifeng-opentrons-ot2-dt-connector` package are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the package uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-07

### Added

- Versioned OT-2 joint-level sim-to-real alignment for the four physical USD
  joints, with strict reference, sign, endpoint, evidence, and range validation
  plus an operator inspection/derivation CLI.
- Evidence-joined joint candidate generation: complete real and simulation
  probe/endpoint artifacts produce a separate validated config and SHA-256
  sidecar without overwriting the active configuration.
- Explicit world/base/deck rigid transforms, non-collinear fiducial fitting,
  reversible point conversion, and reviewed two-transform candidate generation.
- Exact OT-2 well-to-nested-child bindings for the PR7 rack composition (96
  independently selectable tips plus the rack body), canonical manifest hashing,
  runtime drift rejection, and a non-overwriting acceptance CLI.
- Native OT-2 pipette pickup/return translation: safe vertical, XY, contact,
  explicit selected-child attach/detach, physics release, and retract. The
  generated task includes a visible `pick_and_return_tip` workflow.
- A hardware acceptance procedure that separates connector/source defaults
  from measured physical evidence and documents the current X/Y range mismatch.
- An operator-gated `ot2-joint-calibrate` pipeline and one-command wrapper that
  collect real X/Y/Z/A identity, Home, direction, return, and optionally approved
  endpoint evidence without taking direct ownership of the robot serial port.
- PoC arbiter semantics: `RunMode` (SIM_ONLY / REAL_ONLY /
  SIM_FIRST_THEN_REAL / SHADOW) with `OT2ExecutionArbiter`, structured
  `DryRunResult`, `DivergenceAlert`, and per-step lockstep shadow
  comparison of connector state.
- Pure physical-feasibility dry-run gate (`bridge/dry_run.py`): resolves
  every MoveToWell / PickUpTip target from the pinned configuration,
  checks deck bounds, and validates aspirate/dispense volumes against the
  instrument envelope before any real side effect.
- Observability for motion commands: `home`, `move_to`, and `move_to_well`
  now publish per-phase EXECUTING progress and cancellable lifecycle
  semantics (`CancelledError` + hardware halt), matching the SiLA
  intermediate-response guidance.
- Import-safe Matterix OT-2 environment boundary (`matterix/env.py`):
  `validate_environment()` (non-launching readiness + gym task-registration
  probe) and `make_ot2_env()` (lazy Isaac Lab/Matterix imports after the
  caller launches the Omniverse `AppLauncher`).
- `ot2-matterix-env` CLI: `check` (JSON/plain readiness report, `--strict`
  exit code) and `generate <dir>` (writes a Matterix task-extension scaffold:
  `matterix_ot2_env.py`, `__init__.py`, README).
- OT-2 → Matterix action translation now resolves symbolic well references
  (`MoveToWellCfg`, `PickUpTipCfg`) and the configured trash position through
  the pinned `DigitalTwinConfig` instead of assuming raw coordinates.
- Joint-space OT-2 action translation: gantry moves emit aligned
  `MatterixMoveToJointConfig` targets for X/Y plus the active-mount Z/A; Home
  emits the verified real reference pose. B/C are semantic-only because the
  legacy USD has no plunger joints, so aspirate/dispense update liquid semantics
  without inventing action dimensions.
- `ot2-matterix-env generate` now also writes `matterix_ot2_actions.py`
  implementing `build_ot2_action_cfg(action)` against the joint-space
  translation, and the generated env module registers the task with an
  optional `matterix_assets.robots.ot2` articulation.
- Unit coverage for the arbiter modes, shadow divergence detection, the
  dry-run gate, action translation, env validation, joint/frame evidence,
  nested manifest acceptance, and the env CLI.

### Changed

- Contract regenerated with `OperationPhase.EXECUTING`; new contract ID
  `bd710e8b7d66f1f739330945c13b6401450682a0a6a7675c02478cf8f44bbffc`.
- `config/matterix_ot2.json` and the config/contract test pinned to the
  new contract ID.
- Shadow mode now compares state immediately after each side-effecting
  step (lockstep) instead of comparing final state after the whole
  workflow.

## [0.1.0] - 2026-08-05

### Added

- Independent Git and package identity for Sissi Feng's OT-2 connector.
- Hash-pinned SiLA FDL/configuration contract and startup drift gate.
- Versioned device, deck, robot-state, motion, pipette, tip, and
  liquid-handling features.
- Durable evidence-qualified state with `SOFTWARE_TRACKED` and `UNRECONCILED`
  tip semantics.
- Connector-owned atomic tip pickup and release under the hardware lock.
- External `robot.*` workflow parser, contract-derived gRPC transport,
  fail-closed preflight, and append-only side-effect audit.
- Matterix OT-2 semantic action planner, workflow-level StateMachine boundary,
  asset/config pins, shadow comparison, and import-safe readiness CLI.
- Local real-gRPC simulator test covering an audited tip-transfer lifecycle.

### Changed

- Package name changed to `sissifeng-opentrons-ot2-dt-connector`.
- Liquid-operation inputs remain strictly positive while tracked response
  volume permits a valid `0 µL` result after dispense.
- Duplicate same-type module discovery now fails closed.
- Hardware GPIO imports are deferred in explicit simulator mode without
  introducing fallback-to-simulation behavior.

### Safety

- The checked example calibration remains unconfirmed and cannot authorize
  physical motion.
- Real Matterix OT-2 execution remains gated on a verified USD asset, Linux
  Isaac Lab environment, physical calibration, accepted X/Y/Z/A and frame
  alignment, exact nested manifest, and a mount/channel-compatible PR47
  attachment route.
