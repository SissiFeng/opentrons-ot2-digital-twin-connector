"""Inspect or build an accepted OT-2 nested tip-rack binding candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from ..digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError
from .asset import MatterixAssetConfigurationError, OT2MatterixAssetConfig
from .tip_rack import TipRackBindingError, binding_for_labware, nested_manifest_sha256

_MANIFEST_SCHEMA_VERSION = "1.0"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Summarize configured well-to-child bindings.")
    check.add_argument("--config", default="config/matterix_ot2.json")
    check.add_argument("--connector-config", default="config/ot2_dt_config.json")
    check.add_argument("--json", action="store_true")
    check.add_argument("--strict", action="store_true", help="Exit nonzero unless every binding is executable.")

    candidate = subparsers.add_parser(
        "build-candidate",
        help="Validate a runtime child manifest and write a separate accepted candidate config.",
    )
    candidate.add_argument("--config", default="config/matterix_ot2.json")
    candidate.add_argument("--connector-config", default="config/ot2_dt_config.json")
    candidate.add_argument("--manifest", required=True)
    candidate.add_argument("--labware-id", required=True)
    candidate.add_argument("--binding-id", required=True)
    candidate.add_argument("--output", required=True)
    return parser


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        msg = f"Unable to load {label} from {path}: {error}"
        raise TipRackBindingError(msg) from error
    if not isinstance(value, dict):
        msg = f"{label} root must be an object"
        raise TipRackBindingError(msg)
    return value


def _runtime_manifest(path: Path) -> tuple[str, list[str], str]:
    value = _json_object(path, "runtime nested manifest")
    allowed = {"schema_version", "asset_name", "child_ids", "evidence"}
    unknown = set(value) - allowed
    if unknown:
        msg = f"Runtime nested manifest contains unknown fields {sorted(unknown)}"
        raise TipRackBindingError(msg)
    if value.get("schema_version") != _MANIFEST_SCHEMA_VERSION:
        msg = f"Runtime nested manifest schema_version must be {_MANIFEST_SCHEMA_VERSION!r}"
        raise TipRackBindingError(msg)
    asset_name = str(value.get("asset_name", "")).strip()
    evidence = str(value.get("evidence", "")).strip()
    raw_child_ids = value.get("child_ids")
    if not isinstance(raw_child_ids, list) or not asset_name or not evidence:
        msg = "Runtime nested manifest requires asset_name, evidence, and a child_ids array"
        raise TipRackBindingError(msg)
    child_ids = [str(child_id).strip() for child_id in raw_child_ids]
    nested_manifest_sha256(asset_name, child_ids)
    return asset_name, child_ids, evidence


def _check(args: argparse.Namespace) -> int:
    try:
        connector = DigitalTwinConfig.from_file(args.connector_config)
        asset = OT2MatterixAssetConfig.from_file(args.config)
    except (DigitalTwinConfigurationError, MatterixAssetConfigurationError) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1
    rows = []
    ready = True
    for binding in asset.tip_rack_bindings:
        try:
            binding.validate_connector(connector)
        except TipRackBindingError as error:
            executable = False
            detail = str(error)
            ready = False
        else:
            executable = True
            detail = "accepted one-to-one single-channel binding"
        rows.append(
            {
                "labware_id": binding.labware_id,
                "asset_name": binding.asset_name,
                "mount": binding.mount,
                "well_count": len(binding.child_ids),
                "nested_child_count": len(binding.child_ids) + len(binding.non_tip_child_ids),
                "status": binding.status,
                "binding_id": binding.binding_id,
                "manifest_sha256": binding.manifest_sha256,
                "executable": executable,
                "detail": detail,
            }
        )
    payload = {"ready": ready, "bindings": rows}
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        for row in rows:
            state = "READY" if row["executable"] else "NOT_READY"
            sys.stdout.write(
                f"[{state}] {row['labware_id']} -> {row['asset_name']} "
                f"({row['nested_child_count']} rigid children): {row['detail']}\n"
            )
    return 0 if ready or not args.strict else 1


def _build_candidate(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    destination = Path(args.output)
    if destination.resolve() == config_path.resolve():
        sys.stderr.write("error: --output must be separate; the active config is never overwritten\n")
        return 1
    binding_id = args.binding_id.strip()
    if not binding_id or binding_id.upper().startswith("UNVERIFIED"):
        sys.stderr.write("error: --binding-id must identify reviewed evidence and cannot start with UNVERIFIED\n")
        return 1
    manifest_path = Path(args.manifest)
    try:
        connector = DigitalTwinConfig.from_file(args.connector_config)
        asset = OT2MatterixAssetConfig.from_file(config_path)
        config_mapping = _json_object(config_path, "Matterix config")
        manifest_asset_name, child_ids, evidence = _runtime_manifest(manifest_path)
        binding = binding_for_labware(asset.tip_rack_bindings, args.labware_id)
        if binding.asset_name != manifest_asset_name:
            msg = (
                f"Runtime manifest asset_name {manifest_asset_name!r} does not match configured {binding.asset_name!r}"
            )
            raise TipRackBindingError(msg)
        binding.validate_manifest(connector, child_ids)
        digest = nested_manifest_sha256(binding.asset_name, child_ids)
        raw_bindings = config_mapping["tip_rack_bindings"]
        if not isinstance(raw_bindings, list):
            msg = "Matterix config tip_rack_bindings must be an array"
            raise TipRackBindingError(msg)
        matches = [
            item for item in raw_bindings if isinstance(item, dict) and item.get("labware_id") == args.labware_id
        ]
        if len(matches) != 1:
            msg = f"Expected exactly one raw tip-rack binding for {args.labware_id!r}, found {len(matches)}"
            raise TipRackBindingError(msg)
        manifest_file_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        matches[0].update(
            {
                "binding_id": binding_id,
                "status": "VERIFIED",
                "manifest_sha256": digest,
                "evidence": (f"RUNTIME_MANIFEST: {evidence}; path={manifest_path}; file_sha256={manifest_file_sha256}"),
            }
        )
        OT2MatterixAssetConfig.from_mapping(config_mapping)
    except (
        DigitalTwinConfigurationError,
        MatterixAssetConfigurationError,
        TipRackBindingError,
        KeyError,
        OSError,
    ) as error:
        sys.stderr.write(f"error: {error}\n")
        return 1

    payload = (json.dumps(config_mapping, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    candidate_sha256 = hashlib.sha256(payload).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        f"{candidate_sha256}  {destination.name}\n",
        encoding="utf-8",
    )
    sys.stdout.write(
        f"candidate={destination}\nmanifest_sha256={digest}\n"
        f"candidate_sha256={candidate_sha256}\nbinding_id={binding_id}\nactive_config_unchanged=true\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run tip-rack binding inspection or candidate generation."""
    args = _parser().parse_args(argv)
    if args.command == "check":
        return _check(args)
    return _build_candidate(args)


if __name__ == "__main__":
    raise SystemExit(main())
