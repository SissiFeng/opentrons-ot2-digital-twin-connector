# Deployment, gateway migration and recovery (0.7.0)

Run application/setup commands on Ubuntu directly in its terminal. No SSH from
Mac to Ubuntu is required. Browser users only need approved network access and
the application login. Gateway hosts currently support Ubuntu/macOS; a Windows
browser works, but native Windows gateway locking is not supported.

This guide uses an existing `site.json`, private `console-password` and device
profile from [the OT-2 setup guide](browser-ot2-test.md). Keep the working Isaac
environment separate from the Bridge virtual environment. Commands do not install
or change the robot's connector.

## Upgrade an existing installation

Wait for active work to end. Inspect the physical device and stop the old gateway
and Hub. Back up the entire Hub output directory while stopped, including
`application.sqlite3` if present, and both gateway ledger directories. Never delete
ledgers or change `--output` to bypass an uncertain outcome.

In each host's repository checkout, pull the reviewed branch:

```bash
git pull --ff-only origin feat/bridge-ot2-runtime-convergence
```

Ubuntu (from the repository directory):

```bash
"$HOME/ot2-bridge-test/bridge-venv/bin/python" -I -m pip --isolated install ./packages/ot2-bridge
"$HOME/ot2-bridge-test/bridge-venv/bin/python" -I -c 'import importlib.metadata; print(importlib.metadata.version("ot2-bridge"))'
```

Gateway (from its repository directory):

```bash
"$HOME/ot2-gateway-venv/bin/python" -I -m pip --isolated install './packages/ot2-bridge[sila]'
```

Expect version `0.7.0`. Do not install the bridge into Isaac. Do not assume a public
PyPI package with this name is this repository's release. New hosts can create a
venv or use `pipx install './packages/ot2-bridge[sila]'` from the reviewed checkout.
The installed commands are `lab-bridge`, `lab-bridge-gateway`, `lab-bridge-setup`.

## Choose one Hub entry mode

### Direct Tailscale HTTP (existing network)

Use the Ubuntu node's currently verified Tailscale IPv4. This example preserves
the address used during commissioning; recheck `tailscale ip -4` if it changes.

```bash
"$HOME/ot2-bridge-test/bridge-venv/bin/lab-bridge" console \
  --listen 100.119.227.39 --tailscale \
  --password-file "$HOME/ot2-bridge-test/console-password" \
  --site-config "$HOME/ot2-bridge-test/site.json" \
  --output "$HOME/ot2-bridge-test/browser-runs"
```

Browser and separate gateway use `http://100.119.227.39:8088`.
To use MagicDNS, add `--public-url http://EXACT_HUB_NAME.ts.net:8088`, replacing
the hostname with the actual Hub name. It must resolve to the listening address.
Only the printed/configured authorities are accepted. The private tailnet encrypts
transport; browser and gateway credentials remain distinct.

### Same-host gateway with direct Tailscale entry

If Ubuntu is wired directly to OT-2, append `--local-gateway-port 8089` to the
previous console command. Start the gateway on Ubuntu with:

```bash
"$HOME/ot2-gateway-venv/bin/lab-bridge-gateway" \
  --hub http://127.0.0.1:8089 \
  --profile "$HOME/ot2-gateway-config/device-profile.json" \
  --token-file "$HOME/ot2-gateway-config/gateway-token"
```

Port 8089 only listens on loopback and only serves gateway routes. It does not
expose the browser UI/API, and still requires a gateway token. The SSH forward
must run on Ubuntu in this topology; the profile points to that local forward.
There is no separate gateway Tailscale node or inbound gateway grant.

### HTTPS with Tailscale Serve

A site administrator must first enable the appropriate HTTPS/Serve capability
and approve user/gateway access to the external port, normally 443. Set the real
Hub MagicDNS name; do not literally use the example placeholder.

```bash
BRIDGE_PUBLIC_URL='https://EXACT_HUB_NAME.ts.net'
"$HOME/ot2-bridge-test/bridge-venv/bin/lab-bridge" console \
  --listen 127.0.0.1 --port 8088 \
  --public-url "$BRIDGE_PUBLIC_URL" --trusted-proxy-loopback \
  --password-file "$HOME/ot2-bridge-test/console-password" \
  --site-config "$HOME/ot2-bridge-test/site.json" \
  --output "$HOME/ot2-bridge-test/browser-runs"
```

In another Ubuntu terminal, after checking the existing Serve configuration:

```bash
tailscale serve status
tailscale serve --bg --https=443 http://127.0.0.1:8088
```

Coordinate the port/path with the administrator if Serve already hosts another
service; do not overwrite an unrelated service. Use **Serve**, not public Funnel.
The browser and separate gateway use the exact HTTPS URL. A colocated gateway
uses `http://127.0.0.1:8088` directly. Do not add `--tailscale` to the loopback
backend command: Serve handles the remote network entry, and the application
requires its own credentials. Forwarded headers do not grant authentication.

The gateway validates certificates against the host's trust store. There is no
`--insecure` switch. The backend accepts only its local authority and the exact
configured public authority/origin, including a local proxy that rewrites Host.
Actual tailnet policy, DNS and Serve certificates require onsite acceptance.

## Device connection and installation credentials

Keep a manually managed loopback SSH forward on the computer wired to the robot:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ConnectTimeout=5 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -L 127.0.0.1:15052:127.0.0.1:50051 ot2
```

`ot2` must be that host's tested SSH alias. Where link-local routing requires an
interface binding, use the locally verified interface (the commissioning Mac
used `-B en8`; do not copy this name to another host). SSH is not automatically
started or discovered by the gateway. Keep its terminal running; there is no
fallback to a simulator on connection failure.

Initial installations retain gateway ID `default` and the existing token. To
replace a gateway, wait for completion, inspect the device, stop the old gateway
and its SSH process, and retain its ledgers. On Ubuntu run:

```bash
"$HOME/ot2-bridge-test/bridge-venv/bin/lab-bridge-setup" gateway-credential \
  --site-config "$HOME/ot2-bridge-test/site.json" \
  --device-id ot2-lab --gateway-id lab-ot2-02 \
  --token-output "$HOME/ot2-gateway-lab-02.token" \
  --replace --confirm-old-gateway-stopped
```

This explicitly revokes all earlier application tokens for this device. The new
private file is never printed; transfer it securely to the new gateway. A sibling
private credentials registry (or configured `credentials_file`) holds only token
hashes. The running Hub reloads it for authorization/polls/catalog requests; an
old session is fenced when rotation is observed. This is not cancellation of an
already-delivered physical action. Keep old commands terminal or reconciled before
handoff. Do not delete the registry to re-enable a legacy token.

New gateway:

```bash
"$HOME/ot2-gateway-venv/bin/lab-bridge-gateway" \
  --hub http://100.119.227.39:8088 \
  --gateway-id lab-ot2-02 \
  --profile "$HOME/ot2-gateway-config/device-profile.json" \
  --token-file "$HOME/ot2-gateway-lab-02.token"
```

Substitute the configured HTTPS URL or same-host loopback URL as appropriate.
Local host/port may differ from the Hub's profile. Device UUID, instrument/model,
calibration and all other qualified profile fields must still match. Changing a
physical device/model requires a separately reviewed configuration.

A personal Mac can remain a user-owned Tailscale node during temporary gateway
use. Do not retag it just to run this process. Dedicated lab gateways can use
persistent tagged nodes, provisioned by the administrator. Preserve existing
Ubuntu administrative/RDP access when changing that node's identity.

## Rehearsal linkage

Review a plan, select Simulate and start. Once it completes, review again: the UI
shows the matching completed simulation run. Run device / Run both records that
`rehearsal_run_id` after the operator explicitly authorizes the new run.

To enforce this order for an installation, set `"require_rehearsal": true` at the
top level of site.json while the Hub is stopped (or pass `--require-rehearsal`
during initial `lab-bridge-setup ubuntu`). False is the default for independent
commissioning. The server rejects missing, failed, or mismatched references when
required; a successful rehearsal is not proof of current physical safety.

The full per-run plan hash differs between sim and real. Match
`workflow_sha256` + `qualified_profile_sha256`; retain distinct run IDs and the
full plan/profile binding for audit and each native handshake.

## Held runs and restart

1. Preserve the Hub output and gateway ledgers. Stop does not prove the instrument
   has stopped; inspect the actual machine. If Hub crashed, also inspect any old
   Matterix process on Ubuntu. No native process or hardware execution is resumed.
2. If the gateway disconnected, wait for old commands to end and restart the
   gateway. Registration is allowed for inspection while execution remains held.
3. For a physical hold, click **Check connector** and verify the bound identity.
   A pure simulation hold does not require an online physical gateway.
4. In **Inspection required before another run**, record what you checked and
   resolved, confirm the device/native process and old gateway are idle/stopped,
   then click **Record reconciliation**.
5. Review a new run. Previous reports remain held/failed; reconciliation never
   rewrites them to success or replays their operations.

Retain `browser-runs/application.sqlite3`, all run folders,
`~/.local/state/matterix-gateway` (or `--ledger`), and
`~/.local/state/matterix-bridge` (or `MATTERIX_BRIDGE_STATE`). Locks are local;
an independent external orchestrator must participate in the installation's
exclusive device-control procedure. Running a second Hub with a different output
directory or restoring an old database is not a supported recovery procedure.

## Acceptance

Verify authenticated browser access, rejected unauthenticated/wrong-origin requests,
and read-only connector identity first. Then test native sim, explicitly authorized
real Home/readback, and paired execution. Test migration and process restart only
while no physical motion is active. Software fixtures cover these failure paths;
real Tailscale/Serve, Ubuntu GPU and physical acceptance remain separate.
