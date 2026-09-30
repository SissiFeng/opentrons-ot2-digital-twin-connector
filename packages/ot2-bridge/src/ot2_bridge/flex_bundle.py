"""Portable source and run-manifest export, without changing remote machines."""

from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile

from .instruments import validate_plan, is_ot2


def bundle_bytes(plan):
    """Build a relocatable test bundle with fixed launch scripts and no secrets."""
    validate_plan(plan)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plan.json", json.dumps(plan, indent=2))
        root = Path(__file__).parent
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".html", ".css", ".js", ".patch", ".md"):
                archive.write(path, "bridge-src/ot2_bridge/" + path.relative_to(root).as_posix())
        common = """#!/bin/sh
set -eu
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${MATTERIX_ROOT:?Set MATTERIX_ROOT to the pinned Matterix checkout}"
: "${ASSETS_ROOT:?Set ASSETS_ROOT to the pinned Matterix assets checkout}"
export PYTHONPATH="$bundle_dir/bridge-src${PYTHONPATH:+:$PYTHONPATH}"
cd "$MATTERIX_ROOT"
"""
        args = '''./matterix.sh -p -m ot2_bridge.flex_matterix --plan "$bundle_dir/plan.json" --matterix-root "$MATTERIX_ROOT" --assets-root "$ASSETS_ROOT"'''
        if is_ot2(plan["profile"]):
            # The existing activated Isaac environment supplies Python. Avoid the
            # repository's shell launcher, which edits ~/.bashrc on every launch.
            args = '''"${MATTERIX_PYTHON:-python}" -m ot2_bridge.flex_matterix --plan "$bundle_dir/plan.json" --matterix-root "$MATTERIX_ROOT" --assets-root "$ASSETS_ROOT"'''
        scripts = {
            "simulate.sh": common + "exec " + args + ' --output "$bundle_dir/sim-result.json" "$@"\n',
            "serve-sim.sh": common + "exec " + args + ' --serve "$@"\n',
            "bridge.sh": """#!/bin/sh
set -eu
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$bundle_dir/bridge-src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m ot2_bridge.flex_cli "$@"
""",
        }
        for name, script in scripts.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o100755 << 16
            archive.writestr(info, script)
        archive.writestr("requirements-sila.txt", "sila2==0.14.0\ngrpcio==1.80.0\ngrpcio-tools==1.80.0\n")
        if is_ot2(plan["profile"]):
            archive.writestr(
                "README.txt",
                "OT-2 HOME AND READBACK TEST\n\nSee bridge-src/ot2_bridge/guides/ot2-console-first-test.md.\nSet MATTERIX_ROOT and ASSETS_ROOT to the pinned checkouts.\nRun sh simulate.sh for one Matterix execution, or sh serve-sim.sh for console control.\nReal runs connect to the actual OT-2 SiLA connector. No simulator is started automatically.\nHome moves all axes. Clear the deck and remove attached tips/tools before acknowledging.\nThis workflow tests home and position readback; it does not test tip pickup or liquid transfer.\n",
            )
        else:
            archive.writestr(
                "README.txt",
                """FLEX SINGLE-TIP TEST BUNDLE

See bridge-src/ot2_bridge/guides/flex-first-test.md for the full procedure and connector patch requirement.\n\n1. This bundle contains the exact reviewed plan and bridge source. It never installs or starts a robot service.
2. Ubuntu: use the pinned Matterix and assets revisions from plan.json in your working Isaac environment.
3. Sim only: MATTERIX_ROOT=/path/Matterix ASSETS_ROOT=/path/assets sh simulate.sh
   Runs once and writes sim-result.json. Existing output is not overwritten.
4. Paired run: use sh serve-sim.sh instead. Wait for FLEX BRIDGE READY.
5. Mac: python3 -m venv .venv; .venv/bin/pip install -r requirements-sila.txt
   Use .venv/bin/python with bridge-src on PYTHONPATH, or activate .venv and use sh bridge.sh.
6. SSH forward 8765 to the Ubuntu Matterix service and 50051 to the Flex connector.
7. sh bridge.sh inspect --profile plan.json
   Reports connector UUID, simulation flag, pipette identity and tip sensor. No motion.
8. Hardware: fill a profile created by sh bridge.sh profile --instrument flex, including calibrated points and identities.
   Re-export a new plan after any change; both peers must load that exact plan.
   sh bridge.sh home --profile plan.json --hardware  (explicit physical homing)
   sh bridge.sh run --plan plan.json --mode shadow --hardware --output run-result
   For an explicit development connector simulator use --connector-simulator instead of --hardware.
9. Stop: sh bridge.sh stop --profile plan.json
   Requests connector EmergencyStop; always inspect its acknowledgement and the instrument.

No default coordinates qualify hardware. Real moves use deck mm; simulated joints are never sent to hardware.
Concurrent mode waits for both peers at each semantic step. Sim-only success is not physical acceptance.
Only the left single-channel pickup/release workflow is enabled in this release.
""",
            )
    return stream.getvalue()
