"""One-time site setup. Browser users never supply local executable paths or credentials."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shlex
import sys

from .flex_cli import control, read_profile
from .gateway import private_secret
from .instruments import is_ot2, profile_template, validate_profile


def write_new(path, value, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w") as file:
        file.write(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    device = sub.add_parser("device", help="Read-only physical identity binding on the gateway computer")
    device.add_argument("--instrument", choices=("ot2", "flex"), default="ot2")
    device.add_argument("--profile", help="Required qualified site profile for Flex")
    device.add_argument("--host", default="127.0.0.1")
    device.add_argument("--port", type=int, default=15051)
    device.add_argument("--output", required=True)
    native = sub.add_parser("ubuntu", help="Run from the already working Isaac environment")
    native.add_argument("--root", default=str(Path.home() / "ot2-bridge-test"))
    native.add_argument("--device-directory", required=True)
    native.add_argument("--matterix-root")
    native.add_argument("--assets-root")
    native.add_argument("--listen", default="100.119.227.39")
    native.add_argument("--output", help="New site config path; existing files are preserved")
    args = parser.parse_args()
    if args.command == "device":
        profile = read_profile(args.profile) if args.profile else profile_template(args.instrument)
        if not is_ot2(profile) and not args.profile:
            parser.error("Flex requires --profile with qualified coordinates and pipette geometry")
        profile["real"].update(host=args.host, port=args.port)
        info = asyncio.run(control(profile, "inspect"))
        if info.get("is_simulating") is not False:
            raise ValueError("Physical connector required; no fallback to simulation")
        profile["real"]["server_uuid"] = info["server_uuid"]
        if not is_ot2(profile):
            profile["real"].update(pipette_id=info["pipette"]["id"], pipette_model=info["pipette"]["model"])
        validate_profile(profile, hardware=True)
        root = Path(args.output).absolute()
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        write_new(root / "device-profile.json", json.dumps(profile, indent=2))
        write_new(root / "gateway-token", secrets.token_urlsafe(32) + "\n")
        print(f"Read-only physical binding saved: {root}. No robot motion requested.")
        print("Copy this directory privately to Ubuntu once; do not paste its token into chat or Git.")
    else:
        if importlib.util.find_spec("isaaclab") is None:
            raise ValueError("Activate the Isaac environment that already runs Matterix, then repeat setup")
        root = Path(args.root).absolute()
        device_root = Path(args.device_directory).absolute()
        profile_path = device_root / "device-profile.json"
        profile = validate_profile(json.loads(profile_path.read_text()), hardware=True)
        private_secret(device_root / "gateway-token")
        config = {
            "matterix": {
                "python": sys.executable,
                "matterix_root": str(Path(args.matterix_root or root / "Matterix-Internal").absolute()),
                "assets_root": str(Path(args.assets_root or root / "Matterix_assets_internal").absolute()),
                "headless": True,
                "startup_timeout": 600,
                "env": {
                    key: os.environ[key]
                    for key in (
                        "CONDA_PREFIX",
                        "LD_LIBRARY_PATH",
                        "ISAACSIM_PATH",
                        "CARB_APP_PATH",
                        "EXP_PATH",
                        "PYTHONPATH",
                    )
                    if key in os.environ
                },
            },
            "gateways": [
                {
                    "name": "Opentrons OT-2" if is_ot2(profile) else "Opentrons Flex",
                    "profile_file": str(profile_path),
                    "token_file": str(device_root / "gateway-token"),
                }
            ],
        }
        from .flex_matterix import check_checkout

        check_checkout(config["matterix"]["matterix_root"], profile["matterix_revision"], ("source", "scripts"))
        check_checkout(config["matterix"]["assets_root"], profile["assets_revision"])
        root.mkdir(parents=True, exist_ok=True)
        output = Path(args.output or root / "site.json").absolute()
        write_new(output, json.dumps(config, indent=2))
        password = root / "console-password"
        if not password.exists():
            write_new(password, secrets.token_urlsafe(32) + "\n")
        private_secret(password)
        print(f"Site configuration saved: {output}")
        command = [
            str(root / "bridge-venv/bin/python"),
            "-I",
            "-m",
            "ot2_bridge.flex_cli",
            "console",
            "--listen",
            args.listen,
            "--tailscale",
            "--password-file",
            str(password),
            "--site-config",
            str(output),
            "--output",
            str(root / "browser-runs"),
        ]
        print("Start the application on Ubuntu:\n" + shlex.join(command))
        print("Keep that terminal running. Open the printed URL from an authorized browser (user: bridge).")


if __name__ == "__main__":
    main()
