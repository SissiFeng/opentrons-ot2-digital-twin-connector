# Flex bridge: first Ubuntu and hardware test

This release provides a local English run console, an editable instrument profile, a schematic workflow preview, a portable Matterix launcher, a Flex SiLA adapter, and structured run reports. The enabled workflow is **left, single-channel `tip_pickup_and_release`** from Matterix PR #59. It takes `Tips__A1`, carries it to the rear waste, releases it, and returns to park. The first release does not execute mixing, lid manipulation, right/multichannel pipetting or arbitrary Python workflows. OT-2 is also available in the shared console with its own home/readback first-test workflow; see ot2-console-first-test.md.

## What has and has not been exercised

- The bridge mappings and console API can be tested on a Mac without Isaac or hardware.
- SiLA-wire integration uses the actual Flex connector and Opentrons 8.8.1 Flex hardware simulator. The paired integration test uses a clearly labelled synthetic Matterix peer.
- Matterix execution requires the user's working Ubuntu GPU/Isaac environment. This Mac has not established GPU acceptance or physical Flex acceptance.
- A successful command/simulator run does not establish collision freedom, correct physical rack seating, liquid accuracy or electrode retention.

## Source combination

| Component | Revision |
| --- | --- |
| Matterix-Internal PR #59 | `18a96b78ee989fbf4e480e9db6a588c148c6adbe` |
| Matterix_assets_internal PR #11 | `c57f12a04079a5f99405e1fcb818a3b7ccc52e5b` |
| AC Flex connector main | `2639016ee9f234949aaf596c2ab2b93694eb0e0b` plus the included pipette discovery patch |

The Matterix launcher checks the source revisions and rejects local runtime edits. Use clean sibling worktrees if your current working checkout differs. Run `git lfs pull` in the assets worktree if assets are LFS-managed, and verify actual USD files are present. The existing working Isaac/Matterix installation and asset access are prerequisites; the bridge does not install Isaac or download private assets.

**Connector fix required for this baseline:** Opentrons' public `attached_instruments` uses `opentrons.types.Mount`, while the connector's `GetAttachedPipettes` looked up `OT3Mount`. This can report an installed pipette as absent. The one-line compatibility patch reads the public key first and retains the internal-key compatibility path. It does not change robot control.

The patch is at `bridge-src/ot2_bridge/patches/flex-pipette-mount.patch` in the exported bundle. Apply it in the connector source checkout with `git apply --check /path/to/flex-pipette-mount.patch`, then `git apply /path/to/flex-pipette-mount.patch`, and deploy using the connector's existing procedure. Skip application if that exact fix is already present. The local `/Users/sissifeng/opentrons-flex` source is already fixed; deployment to the robot has not been performed here. Do not replace a working connector configuration with the bridge profile.

## 1. Open the console on Mac

From this repository:

```sh
uv run --project packages/ot2-bridge --extra sila lab-bridge console
```

Open `http://127.0.0.1:8088`. No hardware action happens on startup or on plan review. Dependencies beyond the standard library are isolated in the optional `sila` extra. Matterix does not need this extra.

The packaged alternative is:

```sh
python3 -m venv bridge-venv
bridge-venv/bin/pip install '/path/to/ot2_bridge-0.4.0-py3-none-any.whl[sila]'
bridge-venv/bin/lab-bridge console
```

Select Flex. The initial profile has **blank physical coordinates and identities**. It can be reviewed and exported for Matterix-only execution immediately. The central deck is schematic; it does not assert that a physical rack occupies its drawn slot.

## 2. Reproduce Matterix once on Ubuntu

Click **Review this plan**, then **Download Ubuntu test bundle**. Transfer and extract the ZIP on Ubuntu. It includes bridge source, the frozen plan and fixed shell launchers.

Activate the same Isaac/Matterix environment you already used for PR #59. Point it at the pinned clean source and asset trees. If using sibling source worktrees, install their editable Matterix packages in that environment first; an older editable import is explicitly rejected.

```sh
export MATTERIX_ROOT=/absolute/path/to/pinned/Matterix-Internal
export ASSETS_ROOT=/absolute/path/to/pinned/Matterix_assets_internal
cd /absolute/path/to/extracted/flex-test
sh simulate.sh
```

This executes the original 11 native actions partitioned into three semantic operations. It initializes the scene once and **does not reset between operations or repeat the workflow**. It writes `sim-result.json`; success requires `status: completed`, three successful steps, and simulated tip values `true`, `false`, `false`. The launcher refuses an existing report path before running again. To explicitly run a fresh simulation with a different report filename:

```sh
sh simulate.sh --output /absolute/path/to/new-sim-result.json
```

Close this simulation before starting a remote session with the same device model.

## 3. Connect without commanding hardware

The bridge expects SiLA through a local SSH forward, default port 50051. For example, if the connector is reachable from Ubuntu:

```sh
ssh -N -L 50051:FLEX_IP:50051 UBUNTU_USER@UBUNTU_HOST
```

Replace the uppercase host/user placeholders with your setup. This does not start or redeploy the connector. For a connector with TLS instead of the baseline plaintext endpoint, do not point this client at it: configure the appropriate adapter/transport first rather than disabling server security.

Select Flex and **SiLA connector · real**, then **Check connector**. The console reads the server UUID, pipette identity, actual simulation flag, machine state and tip-presence sensor. It fills identity fields in the profile but does not infer physical coordinates or switch the server's mode.

Expected baseline: left single-channel pipette, no attached tip, no active Protocol Engine run, no machine error. If the scan says no pipette despite one being installed, verify the connector discovery patch above. An active Protocol Engine run owns the machine; finish/stop it through its owner before selecting direct SiLA control. Do not run another client concurrently. The bridge has local process exclusion, but cannot lock out controllers on another host.

## 4. Bind your physical setup

Expand **Instrument binding**. Supply:

- The rack identity and A1, matching the physical rack and compatible tip.
- Calibration record, actual tip length in mm, and a reviewed travel speed.
- Rack pickup X/Y/Z, waste release X/Y/Z, and park X/Y at a shared clear travel height, all in the connector's **absolute LEFT deck mm** frame.
- Pickup and drop clearance points directly above their target locations. The form derives them from your travel height.

Pickup coordinates target the nozzle before attachment. After attachment, the controller accounts for the tip's length in its motion frame; the waste and clearance targets must be qualified for that tool state. The bridge never copies Matterix joint values, CAD dimensions or rack locations into these fields. Obtain the points from your calibrated connector/protocol setup; this first version does not include a jog/calibration tool.

There is no example hardware coordinate set to copy. The connector performs its own bounds, sensor, recovery and control-authority checks. The bridge does not replace those checks. **Home instrument** is a separate explicit action after the identity binding is complete; it requires the bound pipette and no attached tip. The operator must verify the deck is clear for homing.

Save the profile, then review and export a **new** plan. Any profile edit requires re-exporting to Ubuntu.

## 5. Run real and Matterix together

On Ubuntu, extract the new bundle and start the persistent Matterix service:

```sh
export MATTERIX_ROOT=/absolute/path/to/pinned/Matterix-Internal
export ASSETS_ROOT=/absolute/path/to/pinned/Matterix_assets_internal
sh serve-sim.sh
```

Wait for `FLEX BRIDGE READY`. It initializes the model once, loads the exact plan, and waits. Then forward the simulation service to the Mac in a second terminal (the SiLA forward must also remain available):

```sh
ssh -N -L 8765:127.0.0.1:8765 UBUNTU_USER@UBUNTU_HOST
```

In the console select **Real + Matterix · paired steps**, verify the reviewed configuration, and acknowledge the run. Click **Start reviewed run**. Both peers execute each semantic operation, and the application waits for both before dispatching the next. It compares tip presence at operation boundaries, preserving separate hardware/simulated evidence and timings. It does not compare uncalibrated positions or claim millisecond synchronization.

On a mismatch, timeout or unknown outcome, the application holds subsequent steps. There is no automatic retry. **Request instrument stop** sends the connector's EmergencyStop and reports its response and machine state. A disconnected client, held run or cancelled await is not proof that hardware stopped. Use the instrument's physical emergency control when needed.

Run records are under the console's `bridge-runs/<run-id>/` directory. A local attempt record prevents replaying a previously attempted real-backend run ID. After inspecting the device, restore a fresh tip at A1 and an empty pipette, review a new run, restart the Matterix session with its new bundle, and explicitly retry. Do not delete attempt records as a substitute for inspection.

## CLI equivalent

```sh
lab-bridge profile --instrument flex --output flex-profile.json
# Edit the profile, then read connector identity:
lab-bridge inspect --profile flex-profile.json
# Fill the returned identities and qualified geometry before export.
lab-bridge export --profile flex-profile.json --output flex-session
lab-bridge home --profile flex-session/plan.json --hardware
lab-bridge run --plan flex-session/plan.json --mode shadow --hardware --output flex-result
lab-bridge stop --profile flex-session/plan.json
```

`--mode real-only` omits Matterix. `--mode sim-only` uses the remote Matterix service without connecting to hardware. In development only, with an explicitly configured connector simulator use `--connector-simulator`, never `--hardware`; a mode mismatch is rejected.

## Extending the backend

`flex_runner.run_plan()` accepts an injected real adapter implementing `prepare(operation)` and `snapshot()`, plus a staged simulation transport. It does not import SiLA, Prefect or any other orchestrator. `FlexAdapter` performs semantic-to-connector mapping; `FlexSiLATransport` handles FDL discovery, protobuf and bounded gRPC. The console selects only implemented backends. Adding a Protocol Engine or other orchestrator adapter must preserve its execution ownership and provide the same observation evidence contract.

Application-only policy lives in `flex_runner`, `flex_console`, `flex_cli`, and `flex_session`. The bridge core owns no rack inventory, scheduler, recovery decision or merged authoritative device state. This Flex release exposes paired steps, not asynchronous shadow catch-up or rolling predictive execution. The existing OT-2 validate-first implementation is unchanged.
