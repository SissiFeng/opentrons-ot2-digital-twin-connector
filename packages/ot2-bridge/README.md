# OT-2 / Flex bridge

For the **browser-operated Ubuntu application** (automatic native Matterix startup,
native view, outbound device gateway), start with the [0.6.0 OT-2 test guide](src/ot2_bridge/guides/browser-ot2-test.md).
No Ubuntu-to-Mac SiLA port access is required in this mode. GPU and physical
acceptance must still be performed in the lab.

For the current Mac-browser / Ubuntu-backend / wired-OT-2 setup, follow [this field guide](src/ot2_bridge/guides/ot2-mac-ui-ubuntu-backend.md). This path tests home and readback with dual eight-channel visual assets; no tip/liquid semantics are added.


## Ready-to-run testing

```sh
uv run --project packages/ot2-bridge ot2-bridge demo --out /tmp/ot2-bridge-demo
```

Run from the repository root. See [TESTING.md](TESTING.md) for the native Matterix
launcher, real-client integration, source requirements and verification boundaries.
The `ot2.client/1` adapter preserves upstream client semantics; the older
`ot2.operation/1` contract below remains supported for existing integrations.

A small, independently installable package for mirroring an orchestrator-owned
real operation into a Matterix simulation. Python >=3.10; no runtime dependencies.
It does not import SiLA, Prefect, Opentrons, Isaac Lab or Matterix at startup.

```sh
python -m pip install ./packages/ot2-bridge
python packages/ot2-bridge/examples/concurrent_mock.py
```

The example is explicitly a mock on both sides. It proves concurrent pairing,
not robot motion or Matterix/PhysX execution.

## Boundary

```text
Orchestrator (workflow order, retries, recovery, run IDs)
    │
    ├─ real adapter → existing SDK / SiLA / HTTP / custom driver
    │
    └─ Operation → Matterix adapter → existing persistent simulation runtime
                         │
               paired outcomes and comparisons → orchestrator
```

Adapters implement `prepare(operation) -> async completion callable`.
`prepare` validates and maps without side effects. The callable executes once
and returns an `Outcome` after the operation and post-state observation finish.
Unsupported mappings fail before either side dispatches in `Mirror.run`.

The bridge owns correlation, concurrent task lifetime and explicit field
comparison. It does not own a workflow graph, device state, geometry, tip
inventory, planning, retries, sim-to-real promotion, or hardware recovery.
Each adapter owns backend capability checks, transport, completion semantics,
state normalization, and any backend-specific cancellation protocol.

## Two integration styles

**Wrapping an existing real call:**

```python
mirror = Mirror(sim_adapter, sim_timeout=30, tolerances={"LEFT.volume": 0.5})
result = await mirror.run(operation, real_adapter, report=reports.append)
result.real.outcome.require_success()  # keep real failure visible to the workflow
# Workflow policy decides how to react to result.sim and result.comparisons.
```

`CallableAdapter` can wrap a plain Python API or an orchestrator's existing
operation callback. There is no requirement to route real hardware through a
SiLA connector. A callback must be asynchronous/nonblocking; synchronous APIs
need backend-owned workers with explicit stop/completion semantics.

**Real execution stays entirely outside the bridge:**

```python
pending = mirror.start(operation)   # immediately before real dispatch
# The external orchestrator performs real execution and normalizes its outcome.
result = await pending.finish(operation, real_execution)
```

`real_execution` is `Execution(Outcome(...), started, finished)`. Completion must
carry the exact same operation identity and parameters. Source clocks need not
be synchronized; comparisons are at semantic operation completion, not physical
time alignment. A remote transport must marshal these values in its own adapter;
the core is not a network service. The optional `remote` module supplies a
loopback simulation endpoint. On external cancellation call
`await pending.abandon()` to release the sim handle; this never controls real.
Cancelling `finish` leaves the sim and handle available for retry or abandonment.
Do not drop an unfinished handle without abandoning it.

A single `Mirror` permits only one pending pair per device. It rejects overlaps
instead of queuing/reordering them. Different devices may overlap only if their
backend runtimes are independent. One shared Matterix StateMachine must be bound
to one device/session or protected by its runtime owner. The bridge neither
resets the environment nor reseeds its state between operations.

## Operation contract

`Operation` has `schema`, `run_id`, `operation_id`, `device_id`, `action` and
immutable JSON-valued `parameters`. IDs and configuration bindings come from the
caller. The session does not persist IDs or guarantee exactly-once execution
across restarts. Do not automatically retry uncertain side effects.

The initial `ot2.operation/1` mappings use the following fields. These are plain
Python/transport payloads, not SiLA FDL identifiers. Unit suffixes are explicit.

| Action | Parameters |
|---|---|
| `home` | none; all axes in the connector's existing Home contract |
| `move_to` | mount, channels, x/y/z (mm), speed_mm_s, reference |
| `move_to_well` | mount, channels, labware_id, well, height, speed_mm_s, reference |
| `pick_up_tip` | mount, channels, labware_id, well |
| `drop_tip` | mount, channels; connector-configured trash |
| `aspirate`, `dispense` | mount, channels, volume_ul, flow_rate_ul_s; **current pose** |

Mount is `LEFT` or `RIGHT`. Well/position reference, deck frame, geometry,
calibration and tip bindings must be agreed by the two adapters before a run.
A coordinate alone is not a labware identity. New semantics need a corresponding
explicit builder/schema version; unknown fields must not be silently ignored.

This differs from upstream `shared.ot2.OT2Pipette.aspirate(wells, ...)`, which also
moves to a well and retracts. To adapt that API, its adapter must mirror the same
whole sequence or the source SDK must expose the individual semantic boundaries.
Likewise `dispense(..., mix_after=True)` is a sequence, not one dispense action.
Never wrap a whole source operation while simulating only its liquid component.

## Outcomes and evidence

Real and sim outcomes remain separate: `succeeded`, `failed`, `cancelled`, or
`unknown`. An exception, disconnect, timeout or cancelled await defaults to
`unknown`, because side effects may have occurred. An adapter may return a
precise failed/cancelled outcome only when its backend confirms it. Success does
not imply an observation exists. No `proceed` decision is produced.

Facts retain `hardware_reported`, `software_tracked`, `simulated` or `unknown`
evidence, plus unit and frame. The caller explicitly selects comparison fields
and absolute numeric tolerances. Missing/unknown facts, unsuccessful execution,
or different units/frames produce `unavailable`, never a match. No fields selected
means no comparisons, not an assertion of agreement. Timestamp/revision fields
preserve provenance; adapters must capture fresh post-state for this operation.

Both backends are scheduled concurrently, not guaranteed to start at the same
physical instant. Pair completion waits for both. One peer's failure never
cancels the other. The sim timeout is **cooperative**: the submit callback must
honor cancellation promptly; a blocking or cancellation-resistant driver cannot
be stopped by asyncio. Late sim completion cannot report success. The real
adapter owns its transport timeout. For GPU execution in another process, that
process owns stepping, deadlines and stop acknowledgement.

Caller cancellation of `run` cancels both awaits, settles their cooperative
cleanup, calls the optional synchronous/non-throwing report sink with partial
results, and propagates standard `CancelledError`. This does not assert physical
stoppage. Repeated forced cancellation/process death may prevent reporting;
durable audit and restart reconciliation remain external responsibilities.

## Matterix and SiLA adapters

`MatterixAdapter(device_id, builders, submit)` consumes native config builders.
`submit(operation, configs)` must install the complete sequence with
`StateMachine.set_action_sequence`, drive the existing environment until actual
completion/failure, then return native observations. Returning on submission is
incorrect. Initial state/asset identity is established by that runtime's owner.

`native_liquid_builders(asset_name)` lazily loads PR46 `AspirateOT2Cfg` and
`DispenseOT2Cfg`. It delegates transfer duration/completion to those native
compositional actions. It currently rejects all but LEFT/single-channel and
current-pose liquid semantics. The optional `NativeOT2Adapter` accepts exact deployment-authored pickup/drop/motion recipes; this package does not guess PR58 holding-joint
release/restoration or 8-channel attachment.

For this repository's high-level SiLA connector, use
`unitelabs.opentrons_ot2.bridge.shadow_adapter.SiLAShadowAdapter` with an already
preflighted executor and explicit observation normalizer. It reuses the existing
contract, hardware locks and audit writer. This is not an adapter for the
upstream repository's low-level motion service; that service can be wrapped with
`CallableAdapter` after matching semantic boundaries.

## Validation boundary

Unit tests cover concurrent starts, timeout/failure isolation, cancellation,
external completion identity, device exclusion, observation comparability,
stdlib-only imports and native-config mapping using constructor doubles.
Connector tests verify SiLA mapping against the packaged contract. Neither these
tests nor the mock example establish GPU execution, eight-channel attachment,
physical accuracy or a deployed upstream Prefect workflow.

## Predictive validation

`ot2-bridge demo --mode validate-first` runs each DT operation before dispatching
its real counterpart. `Prevalidator` binds explicit check results to the exact
operation, starting-state/configuration revisions and a local expiry. The caller
consumes a receipt once, owns the dispatch lock and decides whether to proceed.
See [predictive testing](TESTING.md#5-predictive-mode-validate-the-next-step-before-real-dispatch)
for negative controls, native integration and the one-step scope.

## Flex run console (0.3)

`uv run --project packages/ot2-bridge --extra sila lab-bridge console` opens the local control service at `http://127.0.0.1:8088` when invoked from the repository root. The Flex console previews the pinned PR59 left single-tip workflow, exports a portable Ubuntu Matterix bundle, and supports sim-only, real-only and paired execution. Hardware coordinates and identities are required explicitly. See [the Flex first-connection guide](../../docs/testing/ubuntu-flex-first-connection.md), including the required connector pipette-discovery fix. No GPU or physical acceptance is implied by offline tests.

## OT-2 and physical connector selection (0.4)

The console now defaults to OT-2, with a home/readback first-connection workflow. Flex retains its separate tip-cycle adapter. The UI selects the physical SiLA connector; Matterix is the simulation side. Development connector simulation is CLI-only. See `src/ot2_bridge/guides/ot2-console-first-test.md`. Use `lab-bridge profile --instrument ot2` or `--instrument flex` to create the appropriate profile. Paired OT-2 home/readback records completion and native observations separately; it does not assert calibrated coordinate equivalence.
