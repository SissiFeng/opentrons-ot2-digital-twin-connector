"""Import-safe diagnostic for a real Matterix OT-2 runtime and pinned asset."""

from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import platform
import sys
from pathlib import Path

from ..bridge.registry import ContractRegistry
from ..digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError
from .asset import MatterixAssetConfigurationError, OT2MatterixAssetConfig


@dataclasses.dataclass(frozen=True)
class Check:
    """One non-launching Matterix readiness check."""

    name: str
    ok: bool
    detail: str
    remedy: str

    @property
    def status(self) -> str:
        """Return the machine-readable status."""
        return "OK" if self.ok else "MISSING"

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible diagnostic record."""
        return {**dataclasses.asdict(self), "status": self.status}


def _module(name: str, remedy: str) -> Check:
    try:
        found = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError) as error:
        found = None
        detail = f"{type(error).__name__}: {error}"
    else:
        detail = str(found.origin) if found is not None else "not importable"
    return Check(name=name, ok=found is not None, detail=detail, remedy=remedy)


def _platform_check() -> Check:
    current = platform.system()
    return Check(
        name="Linux platform",
        ok=current == "Linux",
        detail=current,
        remedy="Run the real Isaac Lab and Matterix workflow on a supported Linux machine.",
    )


def _configuration_checks(
    connector_path: Path,
    matterix_path: Path,
) -> tuple[list[Check], OT2MatterixAssetConfig | None]:
    checks = []
    try:
        connector = DigitalTwinConfig.from_file(connector_path)
    except DigitalTwinConfigurationError as error:
        checks.append(
            Check(
                "OT-2 connector configuration",
                False,
                f"{type(error).__name__}: {error}",
                "Provide the same validated digital-twin config used by the connector.",
            )
        )
        return checks, None
    checks.append(Check("OT-2 connector configuration", True, connector.config_id, ""))
    checks.append(
        Check(
            "Confirmed physical calibration",
            connector.calibration_confirmed,
            connector.calibration_id,
            "Verify physical deck and pipette calibration, then set calibration_confirmed=true.",
        )
    )
    try:
        asset = OT2MatterixAssetConfig.from_file(matterix_path)
        asset.validate_connector(connector, ContractRegistry.packaged())
    except MatterixAssetConfigurationError as error:
        checks.append(
            Check(
                "Matterix connector identity pins",
                False,
                str(error),
                "Regenerate Matterix config pins from the packaged contract and active OT-2 config.",
            )
        )
        return checks, None
    checks.append(Check("Matterix connector identity pins", True, asset.contract_id, ""))
    try:
        asset_path = asset.validate_asset_file()
    except MatterixAssetConfigurationError as error:
        checks.append(
            Check(
                "Matterix OT-2 USD asset",
                False,
                str(error),
                "Export or obtain the OT-2 USD asset, verify its SHA-256, and pin both in config.",
            )
        )
    else:
        checks.append(Check("Matterix OT-2 USD asset", True, str(asset_path), ""))
    return checks, asset


def build_checks(
    connector_config_path: str | Path,
    matterix_config_path: str | Path,
) -> list[Check]:
    """Build readiness checks without launching Omniverse or registering gym tasks."""
    checks = [
        _platform_check(),
        _module("isaaclab", "Activate the Isaac Lab Python environment."),
        _module("matterix_sm", "Install Matterix state-machine packages in the Isaac Lab environment."),
        _module("matterix_tasks", "Install Matterix task packages and register the OT-2 task."),
        _module("gymnasium", "Install the Isaac Lab/Matterix Python environment."),
    ]
    config_checks, asset = _configuration_checks(
        Path(connector_config_path),
        Path(matterix_config_path),
    )
    checks.extend(config_checks)
    if asset is not None:
        checks.append(
            _module(
                asset.action_factory_module,
                "Install the OT-2 Matterix compositional-action extension exposing build_ot2_action_cfg(action).",
            )
        )
    return checks


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connector-config", default="config/ot2_dt_config.json")
    parser.add_argument("--matterix-config", default="config/matterix_ot2.json")
    parser.add_argument("--json", action="store_true", help="Emit a machine-readable report.")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when any check is missing.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run non-launching Matterix readiness diagnostics."""
    args = _parser().parse_args(argv)
    checks = build_checks(args.connector_config, args.matterix_config)
    ready = all(check.ok for check in checks)
    if args.json:
        output = json.dumps(
            {
                "ready": ready,
                "python": sys.executable,
                "platform": platform.platform(),
                "checks": [check.to_mapping() for check in checks],
            },
            indent=2,
            sort_keys=True,
        )
    else:
        lines = ["Matterix OT-2 readiness (no simulator launched)"]
        for check in checks:
            lines.append(f"[{check.status}] {check.name}: {check.detail}")
            if not check.ok and check.remedy:
                lines.append(f"  fix: {check.remedy}")
        lines.append("Summary: ready" if ready else "Summary: real Matterix OT-2 execution is not ready")
        output = "\n".join(lines)
    sys.stdout.write(output + "\n")
    return 0 if ready or not args.strict else 1
