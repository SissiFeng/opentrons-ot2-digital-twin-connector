# Mac browser, Ubuntu bridge + Matterix, wired OT-2 via Mac

This procedure runs **home and readback only**. It adds no tip or liquid semantics.
The nominal visual asset is left `p10_multi_v1.6` (8 channels), right
`p300_multi_v2.0` (8 channels). Models are operator-reported; the current SiLA
motion feature does not independently identify them. The asset is not calibrated
for real tool positions or collision checking.

```text
Mac browser ----------------> Ubuntu Bridge backend (Tailscale :8088)
                                   |                    |
                                   |                    +--> Matterix (localhost :8765)
                                   v
Mac Tailscale :15051 --> SSH over wired link --> OT-2 SiLA :50051
```

No SSH server on Ubuntu is required. The two machines already use Tailscale.
The reported Ubuntu address is `100.119.227.39`; the Mac address observed on
2026-09-30 is `100.122.149.108`. Recheck with `tailscale ip -4` if a device changes.
Port 3389 is remote desktop; 22 is SSH; neither is the SiLA or bridge port.

## 1. Mac terminal: keep the OT-2 SiLA forward running

First confirm `ssh ot2` still works and the physical connector listens on 50051.
Use its actual port if different. No connector installation or mode switch is
performed here.

```sh
ssh -N -g -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 100.122.149.108:15051:127.0.0.1:50051 ot2
```

Leave this terminal open. Bind only the Mac Tailscale IP, not `0.0.0.0` or its
Wi-Fi IP. SiLA uses the existing encrypted Tailscale network between Ubuntu and
Mac, then SSH between Mac and OT-2. Access to that forwarded port grants access
to the connector; the tailnet policy should permit the Ubuntu peer only.
If `ssh ot2` times out, fix the wired connection/SSH alias first; Bridge cannot
reach the robot through a failed SSH connection. No automatic retry of motion
is performed after reconnecting.

## 2. Ubuntu remote-desktop terminal: obtain code and prepare

Use a new checkout so your working Matterix directory stays intact. These are
private repositories: use your existing GitHub authentication. `git`, Git LFS,
Python 3.10+ and Python venv support are required. This installs the bridge into
its own venv; it does not install or alter Isaac.

```sh
git clone --branch feat/bridge-ot2-runtime-convergence --single-branch \
  https://github.com/SissiFeng/opentrons-ot2-digital-twin-connector.git \
  "$HOME/ot2-bridge-code"
cd "$HOME/ot2-bridge-code"
bash packages/ot2-bridge/scripts/setup-ot2-ubuntu.sh
```

If this dedicated bridge checkout already exists, run `git pull --ff-only` there
before the setup script. The script creates `$HOME/ot2-bridge-test`, checks exact
source and asset revisions and refuses to reset an existing different or dirty
checkout. Set `OT2_TEST_ROOT` to a new directory if you need another test copy.

## 3. Ubuntu terminal A: start the backend

```sh
cat "$HOME/ot2-bridge-test/console-password"
"$HOME/ot2-bridge-test/bridge-venv/bin/lab-bridge" console \
  --listen 100.119.227.39 --tailscale \
  --password-file "$HOME/ot2-bridge-test/console-password" \
  --output "$HOME/ot2-bridge-test/runs"
```

The password is generated locally, is not committed, and must remain private.
The console only accepts this Tailscale interface and Tailscale peers. Password
authentication protects the page and all API routes; Host, Origin and session
token checks remain enabled. HTTP here travels inside the encrypted Tailscale
connection. The console refuses ordinary LAN/public exposure.

## 4. Mac browser: bind and review

Open **http://100.119.227.39:8088/**. Log in with user **bridge** and the password
displayed in the Ubuntu terminal.

1. Select **OT-2**. Open **Instrument binding**.
2. Set **SiLA host / Mac Tailscale IP** to `100.122.149.108` and the port to `15051`.
3. Click **Check connector**. It reads UUID, real/simulation mode, positions and
   cached homed flags. It does not move the instrument.
4. Click **Review this plan**. The exact bundle is saved on Ubuntu. Copy the
   **Start Matterix on Ubuntu** command shown in the page.

Do not set SiLA host to `127.0.0.1` in this topology: the backend is on Ubuntu,
while the forward is on Mac. The motion feature and physical mode are checked;
the bridge never switches to a connector simulator.

## 5. Ubuntu terminal B: start Matterix

Activate the **same Isaac/Matterix environment that already runs your simulation**
(use your existing activation command; the setup script does not create it).
Then:

```sh
source "$HOME/ot2-bridge-test/runtime.env"
python -c 'import isaaclab; print("Isaac Lab import available")'
```

Paste the exact `cd ... && sh serve-sim.sh` command from the Mac page. It uses
this terminal's Python and imports the pinned Matterix sources without changing
editable package installations or `.bashrc`. Wait for **OT-2 BRIDGE READY**.
The Isaac window should display the dual eight-channel model. This remains a
native GPU test to be performed on Ubuntu; the Mac CPU checks do not prove it.

## 6. Mac browser: perform the staged test

- **Matterix only** tests the native home/readback path without real motion.
- **Real backend only** tests the actual connector independently.
- **Real + Matterix · paired steps** runs the same home/readback plan on both.

For a physical run, clear the deck and remove attached tips/tools, then review
and acknowledge that one run in the page. Both backends keep their own units,
frames and timings. No real joint coordinates are generated from the USD.
Success here means command completion and finite readback, not calibrated
position agreement. A new run needs a fresh review and a new Matterix service
loaded with that exact plan; stop the old service with Ctrl+C before starting
the newly printed command. Never reuse a completed plan for another real run.

Reports are on Ubuntu in `$HOME/ot2-bridge-test/runs/<run-id>/report.json`.
The UI Stop can queue behind OT-2 Home because of the connector's motion lock;
it is not an immediate physical emergency stop.

## Connection checks

From Ubuntu, this only tests that the Mac forward is reachable:

```sh
python3 - <<'PY'
import socket
with socket.create_connection(("100.122.149.108", 15051), timeout=5):
    print("Mac forwarded port reachable; use Check connector to verify SiLA")
PY
```

If the browser cannot connect, verify `tailscale ip -4` on Ubuntu, the console's
printed URL, and its host firewall/Tailscale policy for TCP 8088. If the connector
check fails, inspect the Mac SSH terminal and confirm the actual OT-2 SiLA port.
The final physical and Isaac outcomes must be read from the current run reports.
