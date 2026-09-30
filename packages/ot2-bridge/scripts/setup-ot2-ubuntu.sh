#!/usr/bin/env bash
# Create isolated test checkouts and a bridge-only venv. No robot commands.
set -euo pipefail
umask 077
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
package_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
test_root=${OT2_TEST_ROOT:-"$HOME/ot2-bridge-test"}
mkdir -p "$test_root"
test_root=$(CDPATH= cd -- "$test_root" && pwd)
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
git lfs version

checkout() {
    local url=$1 destination=$2 revision=$3
    if [ ! -e "$destination" ]; then
        GIT_LFS_SKIP_SMUDGE=1 git clone --branch codex/ot2-dual-multichannel --single-branch "$url" "$destination"
        git -C "$destination" checkout --detach "$revision"
    fi
    test "$(git -C "$destination" rev-parse HEAD)" = "$revision" || {
        echo "Different checkout at $destination. Choose a new OT2_TEST_ROOT; no existing files were reset." >&2
        return 1
    }
    test -z "$(git -C "$destination" status --porcelain --untracked-files=normal)" || {
        echo "Local changes at $destination. Preserve them and choose a new OT2_TEST_ROOT." >&2
        return 1
    }
}

checkout https://github.com/ac-rad/Matterix-Internal.git "$test_root/Matterix-Internal" f38d10d86a91c86ddc0e21db1927b68565d8378f
checkout https://github.com/ac-rad/Matterix_assets_internal.git "$test_root/Matterix_assets_internal" 98da840f8bb2770ac5ec82a483dca3c4cf635206
git -C "$test_root/Matterix_assets_internal" lfs pull

if [ ! -e "$test_root/bridge-venv" ]; then
    python3 -m venv "$test_root/bridge-venv"
fi
"$test_root/bridge-venv/bin/python" -m pip install "$package_dir[sila]"

python3 - "$test_root" <<'PY'
import os
from pathlib import Path
import secrets
import shlex
import sys

root = Path(sys.argv[1])
password = root / "console-password"
if not password.exists():
    with password.open("x") as output:
        output.write(secrets.token_urlsafe(32) + "\n")
    password.chmod(0o600)
env = root / "runtime.env"
content = "\n".join(f"export {key}={shlex.quote(str(value))}" for key, value in {
    "MATTERIX_ROOT": root / "Matterix-Internal",
    "ASSETS_ROOT": root / "Matterix_assets_internal",
}.items()) + "\n"
if env.exists() and env.read_text() != content:
    raise SystemExit(f"Preserved existing {env}; choose a new OT2_TEST_ROOT")
if not env.exists():
    env.write_text(content)
print(f"Prepared {root}")
print("This did not install Isaac or command the OT-2.")
print("Start the Ubuntu console:")
print(f"{shlex.quote(str(root / 'bridge-venv/bin/lab-bridge'))} console --listen 100.119.227.39 --tailscale --password-file {shlex.quote(str(password))} --output {shlex.quote(str(root / 'runs'))}")
print(f"Browser user: bridge. Read the password locally with: cat {shlex.quote(str(password))}")
print(f"Matterix terminal: activate your existing Isaac environment, then source {shlex.quote(str(env))}")
PY
