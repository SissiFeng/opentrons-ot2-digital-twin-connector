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
2092afe298601f3a946b0746cc17f52bd9ea504d19e9d34a4c6f3eb4cf24899c
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
| [`config/matterix_ot2.json`](config/matterix_ot2.json) | Matterix task, USD asset hash, joint mapping, action factory, and connector identity pins |

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

See `unitelabs.opentrons_ot2.bridge` for the adapter, transport, executor, and
audit models.

## Matterix status

The connector includes the correct workflow-level integration boundary:

```text
external workflow
  -> contract-resolved OT-2 commands
  -> OT-2 semantic action configs
  -> installed OT-2 Matterix action factory
  -> matterix_sm.StateMachine.set_action_sequence(...)
```

The current Matterix source inventory does not contain an OT-2 USD asset, an
OT-2 gym task, or OT-2 compositional actions. The repository therefore does
not substitute the existing Franka/beaker task or claim a real OT-2 Matterix
run. `config/matterix_ot2.json` intentionally contains an asset-hash
placeholder and a required external action-factory module.

Check the environment without launching Omniverse:

```sh
uv run ot2-matterix-preflight --json
uv run ot2-matterix-preflight --strict
```

Strict readiness requires Linux, Isaac Lab, `matterix_sm`, `matterix_tasks`,
Gymnasium, confirmed physical calibration, a hash-pinned OT-2 USD asset, and an
installed module exposing `build_ot2_action_cfg(action)`.

The import-safe planner and shadow comparison are fully unit-tested on macOS.
Real Isaac Lab execution must be validated on a supported Linux Matterix
machine once the OT-2 asset/action extension exists.

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
