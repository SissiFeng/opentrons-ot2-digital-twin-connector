# OT-2 Scripts

Everything here does one of three things: **set up Tailscale**, **install the connector**,
**verify the robot is up**, or run the operator-gated joint calibration. Every script is meant to be run from the repo root
(`sh scripts/<name>.sh <host>`), never inlined by hand on the robot — see AGENTS.md's
"Hardware Driver Rules" / canonical-scripts convention.

The connector is deployed as a single self-contained binary (PyInstaller) — no venv, no
`pip`, no Python installation required on the robot at all. There is no venv-based
deployment path; if you're looking for one, it was removed on purpose.

**Requirements on the machine you run these from:** a POSIX shell (`sh`), an SSH client
(`ssh`/`scp`) with key-based auth to the robot already working, `curl`, and `tar` — all
of which already ship by default on macOS, Linux, and Windows 10+. No GitHub CLI (`gh`),
auth token, or Python needed — `setup_ot2.sh` downloads the binary as a `.tar.gz` from a
public release asset, which doesn't require authentication.

For a private connector repository, install and authenticate GitHub CLI
(`gh auth login`). `setup_ot2.sh` uses `gh release download` when available,
and falls back to unauthenticated `curl` for public repositories.

## The 3-step flow

### 1. Set up Tailscale — `setup_tailscale.sh` (one-time, per physical robot)

```sh
TS_AUTHKEY=tskey-auth-... sh scripts/setup_tailscale.sh <host>
```

Only needed once, when a robot has never had Tailscale on it (or a reflash wiped `/data`).
Uploads the Tailscale binary, writes the auth key, then calls `install_tailscale_service.sh`.
Skip this step for a robot that's already reachable over Tailscale.

### 2. Install the connector — `setup_ot2.sh` (every deploy)

```sh
sh scripts/setup_ot2.sh <host>
```

This is the script you run to ship the latest `main` to a robot. It:

1. Checks the robot's architecture and Python version over SSH first, and refuses to
   continue if there's no matching build for that OT-2 generation — so an incompatible
   binary is never pushed. (The check itself doesn't require Python on the robot for our
   app — it's just how the two OT-2 generations/binary variants are told apart.)
2. Downloads the matching connector binary from the rolling `ot2-latest`
   GitHub Release (authenticated `gh` for private repositories, otherwise
   `curl`; published automatically on every push to `main`).
3. Calls `../deploy_executable.sh` (copies the binary + process config to
   `/var/sila2_ot2`, and the digital-twin config to
   `/var/lib/opentrons-ot2-dt/config.json`) and
   `install_connector_service.sh` (writes/enables/restarts the `sila2-connector`
   systemd service).
4. Calls `verify_ot2.sh`.

The artifact's `ot2_dt_config.json` is deliberately unconfirmed. To deploy
reviewed physical geometry and calibration, create the untracked local file
`config/ot2_dt_config.local.json`; deployment selects it automatically. The
durable state file is not overwritten.

The release repository defaults to
`sissifeng/opentrons-ot2-digital-twin-connector`. Set
`OT2_CONNECTOR_GITHUB_REPOSITORY=owner/repository` to use another remote.

### 3. Verify everything is up — `verify_ot2.sh`

```sh
sh scripts/verify_ot2.sh <host>
```

Checks both services: `start-tailscale` (+ `tailscale status`) and `sila2-connector`
(+ its last 20 log lines). Run standalone any time you just want a health check.

## Everything else in this directory

These are internals called by the three scripts above — you shouldn't normally need to
run them directly:

| Script | Called by | Purpose |
|--------|-----------|---------|
| `install_tailscale_service.sh` | `setup_tailscale.sh` | Write/enable/restart the `start-tailscale` systemd unit |
| `start_tailscale.sh` | the `start-tailscale` systemd unit | Actually starts `tailscaled` and runs `tailscale up` |
| `install_connector_service.sh` | `setup_ot2.sh` | Write/enable/restart the `sila2-connector` systemd unit; disables the stock Opentrons services that would otherwise hold the GPIO lines |
| `start_connector.sh` | manual, on the robot | `ssh root@<host> "sh /data/start_connector.sh [start\|stop\|restart\|status]"` — manual service management, e.g. after a crash |

`../deploy_executable.sh` (repo root, not in `scripts/`) is the lower-level binary
installer that `setup_ot2.sh` calls into — copies the connector binary + config to
`/var/sila2_ot2` over SSH/SCP.

## Not part of the 3-step flow

| Script | Purpose |
|--------|---------|
| `switch_mode.sh` | Toggle the robot between the stock Opentrons app mode and the SiLA2 connector mode; persists across reboot. |
| `calibrate_ot2_joints.sh` | From the laptop, validate the plan, verify the connector, and run the checkpointed X/Y/Z/A physical measurement pipeline. |
