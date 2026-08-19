# Sissi Feng OT-2 Digital-Twin Connector

An independent SiLA 2 connector for Opentrons OT-2 robots, external
`robot.*` workflows, and Matterix digital-twin integration.

This repository has its own Git history and package identity:
`sissifeng-opentrons-ot2-dt-connector`. It does not inherit a remote or commit
graph from the source snapshot. See
[REPOSITORY_PROVENANCE.md](REPOSITORY_PROVENANCE.md) for source provenance.

## What is implemented

The connector exposes two deliberately separated surfaces:

| Surface | Owner | Purpose |
|---|---|---|
| High-level digital-twin features | OT-2 connector | Versioned deck/config identity, state, motion, atomic tip lifecycle, pipette inventory, and volume operations |
| Diagnostic/raw features | OT-2 connector | Existing motion, GPIO, calibration, and module diagnostics |
| `robot.*` Bridge | Orchestrator adapter | Preserves external workflow JSON, resolves the pinned SiLA contract, gates execution, and writes audit JSONL |
| Matterix boundary | Digital twin | Produces OT-2 semantic action configs, installs complete workflow sequences through `StateMachine.set_action_sequence`, and compares predicted versus connector state |

The high-level SiLA features are:

- `DeviceInformationProvider` 1.0
- `DeckConfigurationProvider` 1.0
- `RobotStateProvider` 1.0
- `MotionController` 2.0
- `PipetteController` 1.0
- `TipController` 1.0
- `LiquidHandlingController` 1.0

The serialized FDL, endpoint modes, parameter identifiers, defined errors, and
configuration schema are pinned by
[`ot2_dt_contract.json`](src/unitelabs/opentrons_ot2/contracts/ot2_dt_contract.json).
The current contract ID is:

```text
bd710e8b7d66f1f739330945c13b6401450682a0a6a7675c02478cf8f44bbffc
```

Connector startup fails if the runtime FDL differs from this artifact.

## Safety and authority boundaries

- Atomic tip operations remain connector-owned under one hardware lock:
  move, pickup or release, retract, then update state.
- The OT-2 has no Flex-style physical tip-presence sensor. Successful tip
  operations therefore report `SOFTWARE_TRACKED`; failures and cancellations
  become `UNKNOWN` / `UNRECONCILED`.
- Side effects are never retried automatically by the Bridge.
- The Bridge performs read-only identity, calibration, pipette, and state
  preflight before any side effect.
- Every Bridge side effect records the request hash plus pre/post connector
  state in append-only JSONL.
- Simulation is explicit. Hardware initialization errors never silently switch
  to simulation.
- The checked example configuration is intentionally
  `calibration_confirmed=false`; it is not authorization for physical motion.

## Configuration

There are three distinct configuration files:

| File | Purpose |
|---|---|
| [`config/ot2_config.json`](config/ot2_config.json) | Connector process, SiLA server, hardware/simulator mode |
| [`config/ot2_dt_config.json`](config/ot2_dt_config.json) | Versioned deck, labware, pipette, calibration, trash, and durable state path |
| [`config/ot2_simulator_config.json`](config/ot2_simulator_config.json) | Explicit local process simulator |
| [`config/ot2_dt_simulator_config.json`](config/ot2_dt_simulator_config.json) | Simulator-only confirmed calibration and `/tmp` state |
| [`config/matterix_ot2.json`](config/matterix_ot2.json) | Matterix task, USD identity, joint/frame alignment, individual tip addressing, action factory, and connector identity pins |

Before physical operation, create a local reviewed digital-twin configuration
with measured geometry and definition hashes, set a real `calibration_id`, and
set `calibration_confirmed=true` only after physical inspection. Do not commit
secrets; process-level secrets belong in environment variables or deployment
configuration.

Validate the exact connector contract:

```sh
uv run ot2-contract-check \
  --config config/ot2_dt_config.json \
  --expect src/unitelabs/opentrons_ot2/contracts/ot2_dt_contract.json
```

## Local development and simulation

Prerequisite: [uv](https://docs.astral.sh/uv/).

```sh
uv sync --all-extras
uv run connector start \
  --app unitelabs.opentrons_ot2:create_app \
  --config-path config/ot2_simulator_config.json -vvv
```

The checked simulator config uses `use_simulator=true`,
`with_robot_server=false`, and the relative digital-twin config path.

Run validation:

```sh
uv run ruff format --check src tests
uv run ruff check src tests
uv run pytest -q
uv build
```

The real local gRPC integration test exercises:

```text
preflight
  -> Home
  -> ReconcileTip
  -> atomic PickUpTip
  -> Aspirate
  -> Dispense to 0 µL
  -> atomic DropTip
  -> post-state and audit verification
```

This is simulator evidence, not physical hardware evidence.

## External workflow Bridge

[`ot2_tip_transfer.json`](examples/workflows/ot2_tip_transfer.json) demonstrates
the unchanged phase-based orchestrator shape. Setup steps such as
`robot.load_labware` remain declarative: the connector's pinned configuration
is authoritative. Non-robot steps remain owned by the mixed-instrument
orchestrator.

The Bridge:

1. parses phases and parallel-thread metadata without renaming actions;
2. resolves pipette aliases and symbolic labware against the pinned config;
3. derives gRPC package, service, endpoint mode, and parameter identifiers from
   the packaged contract;
4. verifies connector/config/calibration/serial/pipette identity;
5. executes once and records pre/post state.

Execution modes (`unitelabs.opentrons_ot2.bridge.arbiter`):

| Mode | Behavior |
|---|---|
| `SIM_ONLY` | preflight + replay on the simulator only |
| `REAL_ONLY` | preflight + run on real hardware; simulator never consulted |
| `SIM_FIRST_THEN_REAL` | preflight, simulator dry-run gate, then real execution |
| `SHADOW` | run simulator and real in lockstep; report state divergence per step |

The dry-run gate (`unitelabs.opentrons_ot2.bridge.dry_run`) is a pure,
hardware-free feasibility check: it resolves every MoveToWell / PickUpTip
target from the pinned configuration, checks deck bounds, and validates
aspirate/dispense volumes against the instrument envelope.

See `unitelabs.opentrons_ot2.bridge` for the adapter, transport, executor,
arbiter, and audit models.

## Matterix status

The connector-side P1/P2/P3 integration code is implemented, but the checked
configuration remains deliberately non-executable until its real and Isaac Sim
evidence is reviewed:

| Area | Code status | Evidence still required |
|---|---|---|
| X/Y/Z/A zero, sign, and limits | Complete; paired hardware/simulation evidence builds a separate candidate config | Real-machine probe and Isaac Sim reference/direction/endpoint replay |
| Nested rigid compatibility | Consumes PR47's selected-child `IsTipAttached` contract; no Franka fixture is used | Runtime child manifest from the exact PR47/PR7 composite |
| Individual tip pickup/return | Explicit well → child mapping, safe vertical/XY/contact route, attach/detach, and return/retract are generated | Native OT-2 WebRTC run after all gates pass |
| World/base/deck frames | Rigid-transform fitting, composition, inverse conversion, and candidate generation are complete | Surveyed non-collinear fiducials and operator review |

### Joint-level sim-to-real alignment

Matterix motion now uses a versioned per-axis contract for the four physical
OT-2 USD joints:

```text
q_real_m = sign * q_sim_m + offset_m
```

The contract separately validates the reference position, direction, and both
motion endpoints. The checked example remains deliberately `UNVERIFIED`, so
connector-driven Matterix joint commands fail closed until real measurements
and matching USD limits are supplied. B/C are explicitly semantic-only because
the legacy OT-2 USD has no plunger joints.

Inspect the current table with:

```sh
uv run ot2-joint-alignment check --json
uv run ot2-joint-alignment check --strict
```

Run the complete operator-gated physical measurement session from the laptop:

```sh
sh scripts/calibrate_ot2_joints.sh <robot-host>
```

The pipeline validates the reviewed plan, checks connector health, refuses a
simulator, homes only X/Y/Z/A, probes each axis at reduced speed, returns every
axis to its reference, and writes checkpointed JSON plus a SHA-256 sidecar under
`artifacts/ot2-joint-calibration/`. Use `endpoints` mode only after setting
reviewed finite Z/A lower bounds in
`config/ot2_joint_calibration_plan.json`.

See [the measurement and acceptance procedure](docs/OT2_JOINT_ALIGNMENT.md).

Complete real and simulation evidence is joined without overwriting the active
configuration:

```sh
uv run ot2-joint-alignment build-candidate \
  --hardware-evidence <hardware.json> \
  --simulation-evidence <simulation.json> \
  --alignment-id <reviewed-id> \
  --output <candidate-config.json>
```

### Reference frames and individual tips

Fit the two audited transform segments, then join the reviewed artifacts into a
separate candidate config:

```sh
uv run ot2-frame-alignment derive --input <world-base-fiducials.json> --output <world-base.json>
uv run ot2-frame-alignment derive --input <base-deck-fiducials.json> --output <base-deck.json>
uv run ot2-frame-alignment build-candidate \
  --world-from-base <world-base.json> \
  --base-from-deck <base-deck.json> \
  --alignment-id <reviewed-id> \
  --output <candidate-config.json>
```

The PR7 layout exposes `pipette_tip_mesh_00` through
`pipette_tip_mesh_95`; current PR47 materializes those 96 tips as nested
children and spawns the empty static rack separately. The connector stores all
96 well mappings explicitly and never derives a child name at runtime. Accept a
runtime enumeration only when all 96 nested tip children match:

```sh
uv run ot2-tip-rack-binding check --strict --json
uv run ot2-tip-rack-binding build-candidate \
  --manifest <runtime-manifest.json> \
  --labware-id tips_300 \
  --binding-id <reviewed-id> \
  --output <candidate-config.json>
```

See [the complete integration and acceptance boundary](docs/OT2_MATTERIX_INTEGRATION.md).

The connector includes the correct workflow-level integration boundary:

```text
external workflow
  -> contract-resolved OT-2 commands
  -> OT-2 semantic action configs
  -> installed OT-2 Matterix action factory
  -> matterix_sm.StateMachine.set_action_sequence(...)
```

The generated task uses the external OT-2 asset/config and the nested tip-rack
semantics from the Matterix integration branches. This repository does not
vendor those payloads and does not treat the earlier Franka qualification as
native OT-2 proof. `config/matterix_ot2.json` therefore retains an asset-hash
placeholder and `UNVERIFIED` evidence states.

Check the environment without launching Omniverse:

```sh
uv run ot2-matterix-preflight --json
uv run ot2-matterix-preflight --strict
uv run ot2-matterix-env check --json
uv run ot2-matterix-env check --strict
```

Strict readiness requires Linux, Isaac Lab, `matterix_sm`, `matterix_tasks`,
Gymnasium, confirmed physical calibration, verified X/Y/Z/A alignment, reviewed
world/base/deck transforms, an exact accepted nested-child manifest, a
hash-pinned OT-2 USD, and an installed `build_ot2_action_cfg(action)` module.

To create the DT-side Matterix env once the OT-2 task extension exists:

1. Generate the task-extension scaffold:
   `uv run ot2-matterix-env generate <dir>` (writes `matterix_ot2_env.py`,
   `__init__.py`, and a README into `<dir>`).
2. Install `<dir>` into the Matterix Isaac Lab environment so its
   `gym.register(...)` runs (import `matterix_tasks` first, or the package
   itself).
3. After `AppLauncher` has started Omniverse, build the env with
   `make_ot2_env(task_id, connector_config_path=..., matterix_config_path=...)`
   (see `unitelabs.opentrons_ot2.matterix.env`) and drive it with the
   `matterix_sm.StateMachine` loop from the PoC `twin_sim.real_runner`.

The generated `pick_and_return_tip` workflow uses the OT-2 pipette: move to a
safe vertical position, translate above the selected well, descend, attach the
explicit nested tip child, retract, return to the same well, detach to physics,
and retract. It does not use a gripper or teleport the tip.

The current PR47 semantic contract selects one nested child at a time. The
checked connector example describes a RIGHT eight-channel pipette while the
source-inspected PR47 sensor route is LEFT/single-child. The integration fails
closed on that mismatch. Native multi-channel pickup requires an upstream
multi-child attachment contract; this repository does not pretend one-child
attachment validates an eight-channel pipette.

### External Matterix integration deliverables

The external branches must provide the exact OT-2 articulation, the PR47
nested-rigid semantics, the PR7 rack layout, its separately spawned static rack,
the 96-tip nested collection, the joint action primitive, and the registered gym
task. The four physical USD joints are
`PrismaticJointMiddleBar`, `PrismaticJointPipetteHolder`,
`PrismaticJointLeftPipette`, and `PrismaticJointRightPipette`. B/C remain liquid
semantics because the USD does not contain plunger joints.

Until their hashes, manifests, and alignment evidence are accepted,
`ot2-matterix-env check --strict` reports not ready by design.

## Dual-server OT-2 deployment

With `with_robot_server=true`, one process exposes:

| Server | Default endpoint | Purpose |
|---|---|---|
| SiLA 2 gRPC | `:50051` | Connector and Bridge clients |
| Opentrons robot-server HTTP | `:31950` via nginx/UDS | Opentrons App and REST clients |

Both share one `HardwareControlAPI` and lock. The stock robot-server systemd
unit is disabled so only this process owns the serial/GPIO hardware.

The deployment artifact contains:

- the self-contained ARM connector binary;
- `ot2_config.json`;
- `ot2_dt_config.json`.

For a reviewed physical configuration, place
`config/ot2_dt_config.local.json` locally before deployment. It is copied to
`/var/lib/opentrons-ot2-dt/config.json`; durable state remains at
`/var/lib/opentrons-ot2-dt/state.json`.

```sh
sh scripts/setup_ot2.sh <robot-host>
sh scripts/verify_ot2.sh <robot-host>
```

The default release source is
`sissifeng/opentrons-ot2-digital-twin-connector`. Override it with
`OT2_CONNECTOR_GITHUB_REPOSITORY=owner/repository` if the remote uses another
name. See [scripts/README.md](scripts/README.md) for details.

## Evidence levels

- Unit tests: contract, validation, state, adapter, audit, Matterix planning,
  and shadow semantics.
- Local gRPC simulator: actual serialized protobuf and observable-command
  flow.
- CI/build: platform matrix and frozen ARM artifact checks after a remote is
  configured.
- Not yet established: physical OT-2 HITL and real Matterix/IsaacLab OT-2
  execution.
