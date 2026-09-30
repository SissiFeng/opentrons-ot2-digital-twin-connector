# Browser application: OT-2 first test (0.6.0)

The browser is only the operator interface. Ubuntu hosts the application and native
Matterix. The computer wired to the OT-2 runs a small gateway which makes outbound
requests to Ubuntu. No Ubuntu → Mac TCP 15051 access is needed in this mode.

The first acceptance is **Home + position readback**, with the left P10 GEN1
8-channel and right P300 GEN2 8-channel nominal assets. No tip/liquid semantics.
Actual Ubuntu/GPU rendering and physical paired execution are field acceptance,
not results established by the CPU/HTTP/browser fixture tests.

## 1. Mac: update and create a local robot connection

Keep the OT-2's real SiLA connector running in its already tested connector mode.
Run in a Mac terminal:

```bash
cd /Users/sissifeng/opentrons-ot2-digital-twin-connector
git pull --ff-only origin feat/bridge-ot2-runtime-convergence
python3 -m venv "$HOME/ot2-gateway-venv"
"$HOME/ot2-gateway-venv/bin/python" -I -m pip --isolated install './packages/ot2-bridge[sila]'
ssh -B en8 -N -o ExitOnForwardFailure=yes -o ConnectTimeout=5 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:15052:127.0.0.1:50051 ot2
```

Leave the SSH terminal running. `en8` is this Mac's previously verified wired
interface; another computer needs its own actual interface. A timeout before SSH
connects must be fixed on the Mac ↔ robot link. The new localhost-only port 15052
can coexist with the earlier Tailscale-bound 15051 forward. No `-g` is needed.

In a second Mac terminal, perform a read-only identity check and save the binding:

```bash
"$HOME/ot2-gateway-venv/bin/python" -I -m ot2_bridge.site_setup device \
  --instrument ot2 --host 127.0.0.1 --port 15052 \
  --output "$HOME/ot2-gateway-config"
```

This reads the real server identity; it does not home the robot. Existing output
is preserved: reuse a valid configuration, or choose a new output directory for a
changed binding. Two files are created: `device-profile.json` and `gateway-token`.
Privately copy this directory once to Ubuntu as `$HOME/ot2-gateway-config`, using
your existing remote-desktop file transfer or another approved private transfer.
The token is a device credential; do not put it in Git, chat, a shared document,
or the browser login box. It is distinct from the existing console password.

## 2. Ubuntu: install/update and configure the native runtime once

In Ubuntu's terminal (no Mac→Ubuntu SSH is required):

```bash
cd "$HOME/ot2-bridge-code"
git pull --ff-only origin feat/bridge-ot2-runtime-convergence
"$HOME/ot2-bridge-test/bridge-venv/bin/python" -I -m pip --isolated install ./packages/ot2-bridge
chmod 600 "$HOME/ot2-gateway-config/gateway-token"
```

If the isolated pinned checkouts and bridge environment do not exist yet, use
`bash packages/ot2-bridge/scripts/setup-ot2-ubuntu.sh` first. It does not install
Isaac. Preserve your already working Isaac environment.

**Activate the same Isaac environment that already runs Matterix**, then run:

```bash
cd "$HOME/ot2-bridge-code"
python packages/ot2-bridge/scripts/configure-browser-app.py ubuntu \
  --root "$HOME/ot2-bridge-test" \
  --device-directory "$HOME/ot2-gateway-config" \
  --listen 100.119.227.39
```

This checks the pinned clean source/assets and captures the activated Python
executable in `~/ot2-bridge-test/site.json`. It never installs Bridge into Isaac.
Use `--matterix-root` / `--assets-root` only for other clean checkouts at the exact
profile revisions. The OT-2 revisions are:

- Matterix: `f38d10d86a91c86ddc0e21db1927b68565d8378f`
- Assets: `98da840f8bb2770ac5ec82a483dca3c4cf635206`

An existing `site.json` is not overwritten. To change the configuration, supply
`--output "$HOME/ot2-bridge-test/site-new.json"`, review it, and launch using that
file. Keep the credential files private. Site setup is performed by the operator
maintaining the installation, not by every browser user.

Stop the old console with Ctrl-C in its terminal. Start the new application:

```bash
"$HOME/ot2-bridge-test/bridge-venv/bin/python" -I -m ot2_bridge.flex_cli console \
  --listen 100.119.227.39 --tailscale \
  --password-file "$HOME/ot2-bridge-test/console-password" \
  --site-config "$HOME/ot2-bridge-test/site.json" \
  --output "$HOME/ot2-bridge-test/browser-runs"
```

Keep it running. Do not start a per-run `serve-sim.sh`: the application now owns
native process startup and cleanup. The existing browser username is `bridge`;
use the password from your private console-password file.

## 3. Mac: start the outbound gateway

After Ubuntu's application is running:

```bash
"$HOME/ot2-gateway-venv/bin/python" -I -m ot2_bridge.gateway_agent \
  --hub http://100.119.227.39:8088 \
  --profile "$HOME/ot2-gateway-config/device-profile.json" \
  --token-file "$HOME/ot2-gateway-config/gateway-token"
```

Expected: `Gateway online: ot2-lab ... No motion requested.` Keep it running.
This uses the Mac→Ubuntu direction already used by your browser, subject to the
site's access policy. It opens no inbound Mac gateway port. Anyone authorized to
reach/login to the Ubuntu application can use the same browser flow; they do not
need a robot connection or Python on their own computer.

## 4. Browser: new acceptance sequence

Open `http://100.119.227.39:8088/` and refresh once after the update.

1. Select **Opentrons OT-2** and the configured device. Verify **Gateway online**.
   There should be no host/port binding form in managed application mode.
2. **Check connector**. Expect a physical identity/state response; no motion.
3. **Review this plan** → **Simulate in Matterix** → acknowledge → start.
   The page shows scene startup, the native runtime log, actual Matterix frames
   with capture time, separate observations and a completed/held report.
4. After inspecting the rehearsal, clear the real deck and remove attached
   tips/tools. Review a new plan, select **Run device**, explicitly acknowledge
   one physical Home/readback run, and start. Observe the actual robot locally.
5. After the independent runs pass, review a new plan and choose **Run both**.
   Matterix must become ready before device execution begins. Each paired step
   completes on both sides before the next step. Elapsed times remain independent.

A simulation and the later physical run have different run IDs with the same
profile hash and workflow. Sim success never starts the device automatically.
Reloading the browser during an active run reconnects to the job; it does not
replay it. The application currently allows one active run at a time.

For every run, retain `browser-runs/<run-id>/plan.json`, `report.json` and, when
simulation was selected, `matterix.log`, `native-ready.json`, and `viewer/`.
Native frames are captured up to twice per second; the browser refreshes about
once per second. This is not a time-synchronized video record. `Last native frame · not live` means the process stopped or the
frame is stale. Missing imagery is explicitly unavailable. Firmware coordinates
(mm) and native joint coordinates (m) are not a calibrated physical comparison.

## Failure handling

- **Gateway offline:** check the SSH terminal, outbound agent terminal and
  Ubuntu URL. No run is automatically resumed or retried after connection loss.
- **Missing gateway response / unknown result:** preserve the report and inspect
  the physical machine. The hub/gateway hold further operations. Reconcile with
  the local operator before restarting both application and gateway and reviewing
  a new run. Restarting is not evidence that the previous motion stopped.
- **Matterix failed/exited:** open **Native runtime log**. Check the captured
  Python executable, pinned clean checkouts, assets, GPU/Isaac environment and
  renderer. Native failures never switch to a synthetic simulation.
- **Stop:** prevents later application steps and sends the actual device Stop
  request when a device run is active. OT-2's connector may queue Stop behind Home;
  use the robot's local emergency procedure for an immediate physical stop.
- **One machine works, another browser cannot reach the site:** that browser still
  needs approved network reachability and login. This application does not change
  Tailscale grants, host firewall rules, or site security policy.

## Flex and other orchestrators

Flex uses the same gateway path with its qualified profile: provide `--profile`
to `site_setup device` with the calibrated deck points and correct pipette.
Add its profile/token as another entry in site.json `gateways`. Distinct pinned
native checkouts can be set in `matterix_by_device`, keyed by `device_id`; each
value has the same fields as `matterix` (`python`, `matterix_root`, `assets_root`,
`headless`, `startup_timeout`, `env`). The per-device value takes precedence over
the default. Flex source/assets pins remain those in its existing profile.

The hub has no SiLA dependency. A device-side provider can be selected once using
`gateway_agent --provider your_package:factory`. That local factory receives the
configured profile and supplies async `inspect`, `begin(plan)`,
`snapshot(revision)`, `execute(Operation)`, `stop`, and `close`. It returns the
existing Observation/Outcome contracts and owns physical identity validation.
The gateway still enforces exact configured profile, ordered reviewed operations,
local exclusion and no replay. Current OT-2/Flex profile fields retain the legacy
SiLA binding shape; a replacement provider may map/ignore transport-specific
fields, but must validate its own real device identity before any motion.
Provider import paths are local setup only, never accepted from a browser.
