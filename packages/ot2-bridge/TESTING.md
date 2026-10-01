# Test the OT-2 bridge

The bridge maps and correlates operations and observations. The sample runner owns workflow order and its stop-on-difference policy. Matterix owns simulation; the external client owns physical execution. Neither the comparison result nor a simulated prediction automatically changes the real robot.

## 1. Laptop: one-command end-to-end test

From this repository, with `uv` installed:

```sh
uv run --project packages/ot2-bridge ot2-bridge demo --out /tmp/ot2-bridge-demo
```

This starts a persistent synthetic simulation service on a temporary loopback port, connects a second synthetic peer, and runs home → pickup → move → aspirate → move → dispense → drop. Both peers are labelled synthetic. It tests actual JSON transport, operation pairing, completion, observation provenance and comparisons. It does not run Isaac or contact hardware.

Open `/tmp/ot2-bridge-demo/report.html`. Inspect `trace.jsonl` for the exact separate real-side and simulation outcomes. The output directory must not already contain a trace, to prevent overwriting evidence.

Negative controls:

```sh
uv run --project packages/ot2-bridge ot2-bridge demo --drift 2 --out /tmp/ot2-bridge-drift
uv run --project packages/ot2-bridge ot2-bridge demo --fail-action aspirate --out /tmp/ot2-bridge-failure
```

Expected: drift stops after operation 4 with exit code 3 and `pipette.volume=different`; injected simulation failure stops after operation 4 with exit code 2, while the real-side operation still finishes successfully. Unavailable comparisons are never matches. These stop decisions belong to the example runner, not `Mirror`.

## 2. Native Matterix: GPU smoke test with a synthetic real peer

Requires the existing Linux/Isaac/Matterix installation. Source contract inspected:

- Matterix branch `codex/ot2-tip-rack-collection-97-integration-46`, commit `8d70c1977ed26249b6f5e47644d5a84a44a4d003`.
- Task `Matterix-OT2-PipettingSemantics-v1`, one environment, one left pipette, one rigid tip asset.
- Native `MoveToJointConfigCfg`, `SetOT2TipAttachedCfg`, `AspirateOT2Cfg`, `DispenseOT2Cfg`.

Install `packages/ot2-bridge` into **that environment's Python**, for example `python -m pip install -e /absolute/path/to/connector/packages/ot2-bridge`. Then, from the Matterix checkout:

```sh
./matterix.sh -p /absolute/path/to/connector/packages/ot2-bridge/examples/matterix_server.py \
  --plan /absolute/path/to/connector/packages/ot2-bridge/examples/native/plan.json \
  --profile /absolute/path/to/connector/packages/ot2-bridge/examples/native/profile.json
```

Add `--headless` if desired. The launcher creates/reset the environment once, validates all recipes before listening, and then preserves the environment across operations. It forwards native semantic outputs on every physics step and waits for terminal action masks. It disables the ordinary episode clock; genuine termination, truncation, failure or cancellation invalidates the session. The simulation advances on operation execution, not while waiting for network input.

From the same machine, in another terminal:

```sh
ot2-bridge run --demo-real \
  --plan /absolute/path/to/connector/packages/ot2-bridge/examples/native/plan.json \
  --out /tmp/ot2-bridge-native
```

From a different machine, first forward the loopback endpoint:

```sh
ssh -N -L 8765:127.0.0.1:8765 your-gpu-host
```

Run the same client command locally. The bridge does not expose an unauthenticated public listener or open robot ports.

**The supplied native profile is a simulation smoke fixture.** Its joint targets come from the upstream tip demonstration; its well labels are synthetic and do not describe calibrated plate geometry. Its `move_to_well` recipes deliberately exercise the message/sequence path at a fixed pose. It cannot be used in hardware mode. It does not validate aspiration from actual wells, eight-channel handling or PR58 collection-tip holding joints.

Each server process owns one run. After completion or failure, inspect the evidence and deliberately start a new session; reconnect is not an automatic reset or replay. Interrupting the client does not prove the remote simulation stopped: its server-side deadline owns cleanup. Reconcile before restarting.

## 3. Existing real client or orchestrator

`OT2ClientAdapter` accepts an existing `Ot2SilaClient`, or another object implementing the same semantic methods, plus an async observation callback. It imports neither SiLA nor an orchestrator. A different API can use `CallableAdapter`; no core changes are required.

`ot2.client/1` preserves the upstream client's exact operation semantics:

| Action | Explicit parameters |
|---|---|
| home | axes |
| move_to_well | mount, channels, slot, well, origin, offset, tip_length_mm, speed |
| pick_up_tip | mount, channels, slot, well, offset, tip_length_mm, speed |
| drop_tip | mount, channels, tiprack_diameter_mm, speed |
| aspirate | mount, channels, volume_ul, flow_rate_ul_s |
| dispense | mount, channels, volume_ul, flow_rate_ul_s, empty_after |

Mounts are uppercase `LEFT`/`RIGHT` on the wire; the adapter maps them to upstream lowercase. Geometry is in millimetres, speed in mm/s, volume in µL, flow in µL/s. No fields are silently dropped. Pickup includes the client's press/retract behavior; drop acts at the current location; liquid operations act at the current pose. `empty_after` is preserved.

The native adapter currently supports **LEFT, one channel**. The client adapter can describe either mount and 1/8 channels, but that does not confer those capabilities on Matterix.

For native motion and tip operations, create exact parameter-matched recipes for the deployed geometry. A recipe includes all semantic inputs, an ordered sequence of four-joint targets (metres), and explicit rigid-tip attachment steps. `axis_transform` maps each selected native joint into a reported machine axis via `machine_mm = native_m * scale + offset_mm`; without calibration, joint facts stay in the native frame and are not compared to machine axes. It is the deployment's responsibility to supply physically valid paths, timing and transforms. Configuration hashes establish identity, not physical correctness.

The real observation callback must read the real state owner. `position_observation()` labels firmware-reported positions and never uses commanded return targets. The client has no authoritative current-volume ledger: missing tip/liquid facts stay absent. A caller-owned ledger may supply `Evidence.TRACKED` facts; simulated volume must never be labelled measured.

## 4. Hardware runner

The CLI supports the upstream client at `AccelerationConsortium/opentrons-sila-clients` commit `271b3fa36d9fc2cd6bbf7f5494bc2ae441672381`. Install it in the **caller** environment, not the OT-2 or GPU environment. Its `sila-toolkit` dependency comes from the private `AccelerationConsortium/uoroboros` repository. The current account received HTTP 404 for that dependency during verification; use an environment where this client is already installed or obtain repository access. There is no fallback or bundled substitute.

Create a deployment plan from the documented operation contract with:

- `binding.mode`: `hardware`.
- `binding.device_id`, `binding.run_id`: the physical-device identity and caller-owned run ID.
- `binding.qualified_mapping_revision`: your reviewed geometry/tip mapping revision.
- `binding.profile_sha256`: `ot2_bridge.wire.fingerprint(profile_json)`.
- `binding.client_config_sha256`: `fingerprint(client_config_json)`.
- `binding.matterix_revision`: the native source revision being tested.
- `mounts`: the deployed channel count, currently `{"LEFT": 1}` for native Matterix.
- `initial_sim`: expected initial native values; the bridge checks these without continuously copying real state into simulation.
- `tolerances`: selected comparable facts, with explicit absolute tolerances in their units.
- `require_comparable`: `true` when missing selected observations must stop this test runner.
- `operations`: unique operation IDs, matching the binding's run and device.

Use the upstream `Ot2Config` JSON for host, port, deck, pipettes and configuration; calibration remains the client's live calibration provider. Include a real move to a valid disposal position before `drop_tip`. Validate geometry, calibration and starting state for that physical setup before running; the smoke profile is not a hardware calibration.

Start the Matterix server with that same plan/profile, then:

```sh
ot2-bridge inspect --plan /absolute/path/to/hardware-plan.json
ot2-bridge run --hardware \
  --plan /absolute/path/to/hardware-plan.json \
  --client-config /absolute/path/to/ot2-client.json \
  --out /absolute/path/to/new-run-directory
```

Hardware mode rejects a connector reporting simulation mode. It reads fresh firmware axes after each completed operation; software-tracked liquid/tip facts require a caller-provided observer through the Python adapter. The core does not implement retries, recovery, workflow scheduling or automatic physical cancellation.

## Evidence and limitations

The independent core has zero mandatory dependencies. The network shim, native launcher, sample runner and offline report are optional integration utilities. `Mirror.start/finish` also support externally owned execution without routing physical commands through the bridge.

Automated coverage exercises the full loopback transfer, drift, failure isolation, exact source-call mapping, unknown readback, initialization mismatch, replay rejection, native stepping and cancellation. Native runtime tests use controlled state-machine/environment doubles. This Mac has no Isaac runtime; **GPU and physical OT-2 acceptance have not been performed**. The hardware client's private dependency also prevents an installed-client integration test here.

There is no continuous telemetry subscription, physically synchronized clock, authoritative combined state database, calibrated liquid measurement, or automatic recovery. Monitoring and comparison occur at semantic operation boundaries. The trace and report enable inspection; replay into physical hardware is intentionally absent.

## 5. Predictive mode: validate the next step before real dispatch

```sh
uv run --project packages/ot2-bridge ot2-bridge demo --mode validate-first --out /tmp/bridge-predictive
uv run --project packages/ot2-bridge ot2-bridge demo --mode validate-first --fail-action aspirate --out /tmp/bridge-held
uv run --project packages/ot2-bridge ot2-bridge demo --mode validate-first --drift 400 --out /tmp/bridge-check-failed
```

The first run completes seven operations sequentially: DT → validation → real.
The second and third runs exit5 at operation4; **the real aspiration is never
dispatched**. The third run deliberately returns successful DT execution but
fails the configured capacity check, demonstrating that execution success is
not sufficient. These are synthetic controls, not physical-volume evidence.

Use `--mode validate-first` with `run` for the native launcher or hardware entry
point too. Both terminals must use the same updated plan. The provided native
plan includes a sample `pipette_capacity` check. Real hardware also needs
comparable real/DT starting-state facts; the default firmware observer does not
supply tip or liquid state, so the example liquid/tip tolerances will correctly
hold hardware dispatch. Supply an appropriate caller-owned observer or select
qualified comparable fields; do not fabricate missing facts.

The runner requires `validation_checks`, each with `name`, `field`, `unit`,
`frame`, and either numeric `min`/`max` or scalar `equals`. Only known simulated
facts with matching units/frame are eligible. Missing checks, unknown results,
DT failure, timeout, expiry or changed state/configuration hold dispatch. The
sample capacity rule does not certify collisions, trajectories, tip engagement
or liquid transfer accuracy. Such checks need actual model evidence supplied
by the deployment evaluator; endpoint bounds alone cannot certify a whole path.

Before prediction, the runner compares selected current real facts against the
last DT observation. It then captures a starting-state/configuration fingerprint,
executes the DT once, and reads real state again before consuming a single-use
receipt. `validation_validity_s` defaults to5 seconds from DT completion.
Predicted and real states remain separate; the same prediction is used for the
post-real comparison, without advancing the DT twice. If validation is held or
real execution diverges, stop and explicitly reconcile/reinitialize the session.
There is no automatic rollback, retry or reseeding.

Python integrations can use `Prevalidator.validate(operation, context)` and
`claim(receipt, operation, current_context)` separately. The core never sends a
real command. The caller supplies explicit `required_checks`, an evaluator
returning named `Check` verdicts, and `ValidationContext` revisions covering all
relevant state, model, calibration and check configuration. A receipt belongs
to that validator instance and is consumed once; it is not a transferable or
persisted authorization token. The caller must hold its device/configuration
lock from the initial snapshot through claim and real dispatch. Fingerprints
alone do not detect a value changing away and back, unobserved physical changes,
or races with other controllers. The CLI assumes it is the sole dispatcher.

`trace.jsonl` records checks, hold reasons, context revisions and host-monotonic
DT/real timings. A held step has `real: null`; it is not a hardware failure.
`validation_lead_s` measures from receipt's DT completion to real call start on
the caller host. It includes the context/readback gate and is not firmware motion
latency or a cross-host clock measurement.

This is **one-step validate-first**, not rolling multi-step lookahead or emergency
stop. It trades extra wait time for a pre-dispatch check. Physical stop of an
already-running action still belongs to the orchestrator/device driver. Rolling
lookahead will require separate predicted checkpoints and invalidation of every
prediction downstream of a changed real state.

For the sample scalar checks, every checked field must also appear in the
starting-state `tolerances`. Add other model dependencies through
`validation_start_fields` (for example tip attachment, position, or source-well
volume), with corresponding tolerances. All selected facts must be known and
comparable before validation begins. Supplying an incomplete dependency list is
not a model qualification; the deployment must declare every input its checks
rely on. The generic evaluator API remains available for richer path-dependent
checks.


## Deployment and recovery acceptance (0.7.0)

`tests/test_deployment_recovery.py` exercises persistent Hub holds after process
SIGKILL, legacy interrupted-run import, local owner exclusion, explicit
reconciliation, token rotation/session fencing, transport-independent profile
binding, successful rehearsal matching, strict proxy origins, gateway-only
loopback routing, proxy/redirect rejection, and real local TLS handshakes with
untrusted-CA and wrong-hostname rejection. All devices/renderers in these tests
are CPU fixtures; no robot command or Isaac process is involved.

The browser fixture also completed: connector inspection, recorded reconciliation,
simulation, reviewed linked device run, and refresh restoring the completed run's
observations. It had no physical device and no native Matterix scene.

Validation on 2026-10-01: package suite **193 passed, 1 skipped** (optional external
Flex connector wire fixture absent). Final gateway/recovery subset **29 passed**.
Changed Python files pass the repository's full Ruff rules; all package files pass
F/E9 checks and JavaScript syntax passes. The package-wide full-rule scan also
reports nine existing lint findings in unchanged files, outside this change.
Wheel/sdist build and clean-wheel CLI/startup/journal/packaged-guide smoke checks
pass. The dedicated Bridge application workflow runs CPU acceptance on Ubuntu
Python 3.10 and 3.12. These statements do not establish tailnet/Serve, GPU, or
physical field acceptance; use the deployment and OT-2 guides for those checks.
