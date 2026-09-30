# OT-2 first test from the shared bridge console

For a Mac browser with the backend on Ubuntu and a wired OT-2 on Mac, use [the Tailscale deployment guide](ot2-mac-ui-ubuntu-backend.md).

The console now defaults to **Opentrons OT-2**. The real backend is the **physical instrument's SiLA connector**. Matterix is the digital-twin simulation. The console neither starts a connector simulator nor changes a connector's mode; a service that reports simulation mode cannot execute a real console run.

The first OT-2 console workflow is **Home & position readback**:

1. Send `Home(Axes="XYZABC")` to the actual OT-2 motion feature. Matterix executes its pinned native `move_home` workflow.
2. Read all six firmware axis positions and connector homing flags. Matterix reads its own four native joint positions without resetting the scene.

This verifies the connection, command mapping, completion and observation path. It does not perform tip pickup, liquid transfer or Flex commands. Existing calibrated OT-2 semantic workflows remain available through `ot2-bridge`; they are not automatically reinterpreted as this console profile.

## Open the console

From the bridge repository on the Mac:

```sh
uv run --project packages/ot2-bridge --extra sila lab-bridge console
```

Open `http://127.0.0.1:8088`. Select **Opentrons OT-2** and **SiLA connector · real**. The device selector changes the adapter, workflow, profile, preview and exported plan together. Flex remains available with its own tip-cycle profile.

For a wheel installation:

```sh
python3 -m venv bridge-venv
bridge-venv/bin/pip install '/absolute/path/ot2_bridge-0.5.2-py3-none-any.whl[sila]'
bridge-venv/bin/lab-bridge console
```

## 1. Connect and inspect without motion

Keep the existing real OT-2 SiLA connector running. If Ubuntu can reach it, forward its actual SiLA port to the Mac:

```sh
ssh -N -L 50051:OT2_IP:50051 UBUNTU_USER@UBUNTU_HOST
```

Replace the host/user placeholders and remote port with your deployment. This is the SiLA port, not the robot-server HTTP API port. The baseline transport uses the plaintext connector behind this SSH tunnel. A TLS-only deployment needs a matching transport adapter; do not disable its security.

In the console, **Instrument binding → Forwarded SiLA port** should match your local forward. Click **Check connector**. It only reads:

- SiLA server UUID and `IsSimulating`;
- `GetPosition`: X/Y/Z/A/B/C in firmware machine mm;
- `HomedFlags`: the connector driver's cached per-axis flags, labelled `software_tracked`, not a fresh hardware sensor measurement.

The adapter discovers `ca.accelerationconsortium/robots/MotionControlFeature/v1` from the server's live feature definition. A missing/incompatible feature fails before motion. It never falls back to the Flex adapter or a simulator. Bind the UUID returned from the correct robot. Position and homing observations do not establish that a tip, electrode or liquid is present.

## 2. Test the real OT-2 first

Clear the deck and remove attached tips/tools. Ensure no other controller owns an active run on the instrument. The bridge's local lock excludes another bridge process on this control host, not every external client.

1. Choose **Real backend only**.
2. Click **Review this plan**.
3. Read the two steps and acknowledge physical homing/readback.
4. Click **Start reviewed run**.

**This commands physical movement.** All six axes are homed through the existing connector. No coordinates, pipette model or labware mapping are invented by the bridge.

Pass condition: both operations succeed, all connector homing flags are true and the report contains six finite axis positions. Inspect the instrument as well as the report. There is no requirement to launch Matterix for this test.

## 3. Test Matterix once on Ubuntu

The exported source/assets baseline is:

| Component | Revision |
| --- | --- |
| Matterix-Internal | `f38d10d86a91c86ddc0e21db1927b68565d8378f` |
| Matterix_assets_internal | `98da840f8bb2770ac5ec82a483dca3c4cf635206` |
| Native task / workflow | `Matterix-OT2-DualMulti-Home-v1` / `move_home` |

Use the existing working Isaac/Matterix environment with clean pinned source and asset checkouts, actual USD/LFS files and editable imports pointing to the selected Matterix checkout. The launcher verifies revisions and import locations. It does not install Isaac or obtain private assets.

Review a new plan and click **Download Ubuntu test bundle**. Extract it on Ubuntu:

```sh
export MATTERIX_ROOT=/absolute/path/to/pinned/Matterix-Internal
export ASSETS_ROOT=/absolute/path/to/pinned/Matterix_assets_internal
cd /absolute/path/to/extracted/ot2-test
sh simulate.sh
```

It initializes once, executes native home once, reads the native joints, and writes `sim-result.json`. Pass condition: `status: completed`, two succeeded steps and all four joints within 0.005 m of the native home target. It refuses an existing output file; use a new filename for an explicitly requested rerun:

```sh
sh simulate.sh --output /absolute/path/to/new-sim-result.json
```

## 4. Pair real OT-2 with Matterix

Review/export a fresh plan from the bound physical profile. Extract that exact bundle on Ubuntu and run:

```sh
sh serve-sim.sh
```

Wait for **OT-2 BRIDGE READY**, then forward its service:

```sh
ssh -N -L 8765:127.0.0.1:8765 UBUNTU_USER@UBUNTU_HOST
```

Keep the SiLA forward alive too. Choose **Real + Matterix · paired steps**, acknowledge the exact loaded plan and physical setup, and start. Both sides complete home before the second readback step.

The first-test comparison is **completion only**. Firmware machine coordinates in mm and Matterix joints in m remain separate. A completed paired run is not a calibrated position match, simultaneous timing guarantee or proof of physical safety. The report marks this explicitly. A missing readback, timeout or failed operation holds subsequent dispatch. No automatic retry occurs.

## Stop and records

**Request instrument stop** calls the bound connector's `EmergencyStop` and shows its response. The bridge also stops dispatching subsequent operations. In the current OT-2 connector, Home and EmergencyStop share a motion lock, so Stop can queue behind an in-progress Home. The UI Stop is not an immediate hardware emergency-stop guarantee. Cancelling a request or seeing a held run does not prove physical stop; inspect the robot and use its physical stop control when necessary.

Each attempt writes `plan.json` and `report.json` under the console's output directory. A run ID cannot be reused for another real attempt. Inspect the result and create a fresh review before retrying. The console uses `bridge-runs/` by default; a custom `--output` path is shown in the run record.

## CLI equivalent

```sh
lab-bridge profile --instrument ot2 --output ot2-profile.json
lab-bridge inspect --profile ot2-profile.json
# Put the returned server UUID into ot2-profile.json.
lab-bridge export --profile ot2-profile.json --output ot2-session
lab-bridge run --plan ot2-session/plan.json --mode real-only --hardware --output ot2-result
lab-bridge stop --profile ot2-session/plan.json
```

Development tests may opt into `--connector-simulator` explicitly on the CLI; it is absent from the normal console. A mode mismatch is rejected. Development simulation never stands in for physical acceptance.

Local verification exercised the real OT-2 connector through gRPC using its explicitly selected Smoothie simulator, plus a synthetic Matterix peer for paired transport. Native GPU and physical OT-2 acceptance remain to be performed on site.
