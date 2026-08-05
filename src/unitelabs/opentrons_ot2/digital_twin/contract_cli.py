"""Generate or verify the exact high-level connector contract."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from unitelabs.cdk import Connector, SiLAServerConfig

from .. import OpentronsOt2Config, _register_digital_twin_features
from ..io import OT2MotionController
from .contract import ContractMismatchError, ContractSnapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/ot2_dt_config.json", help="Digital-twin configuration path")
    parser.add_argument("--write", help="Write the generated contract artifact to this path")
    parser.add_argument("--expect", help="Require equality with a stored contract artifact")
    parser.add_argument("--expect-id", help="Require this exact contract_id")
    return parser


async def _run(args: argparse.Namespace) -> ContractSnapshot:
    config = OpentronsOt2Config(
        use_simulator=True,
        digital_twin_config_path=args.config,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    motion = await OT2MotionController.build(simulate=True)
    try:
        connector = Connector(config)
        _, snapshot = await _register_digital_twin_features(
            connector,
            motion,
            config,
            (),
            verify_packaged_contract=False,
        )
        if args.expect:
            expected = ContractSnapshot.from_file(args.expect)
            if snapshot != expected:
                msg = f"Generated contract {snapshot.contract_id} differs from stored contract {expected.contract_id}"
                raise ContractMismatchError(msg)
        if args.expect_id:
            snapshot.require(args.expect_id)
        if args.write:
            snapshot.write(Path(args.write))
        return snapshot
    finally:
        await motion.disconnect()


def main() -> None:
    """Run the contract generator and fail closed on any incompatibility."""
    args = _parser().parse_args()
    snapshot = asyncio.run(_run(args))
    sys.stdout.write(json.dumps(snapshot.to_mapping(), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
