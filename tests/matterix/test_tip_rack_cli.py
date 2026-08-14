"""CLI tests for nested tip-rack binding acceptance."""

from __future__ import annotations

import json
from pathlib import Path

from unitelabs.opentrons_ot2.matterix.asset import OT2MatterixAssetConfig
from unitelabs.opentrons_ot2.matterix.tip_rack import nested_manifest_sha256
from unitelabs.opentrons_ot2.matterix.tip_rack_cli import main

from tests.digital_twin.helpers import config_mapping


def _candidate_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    connector_mapping = config_mapping(tmp_path / "state.json")
    connector_mapping["instruments"][0]["channels"] = 1
    connector_mapping["instruments"][0]["expected_model"] = "p300_single_v2.1"
    connector_path = tmp_path / "connector.json"
    connector_path.write_text(json.dumps(connector_mapping), encoding="utf-8")

    matterix_mapping = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    binding = matterix_mapping["tip_rack_bindings"][0]
    binding["mount"] = "RIGHT"
    matterix_path = tmp_path / "matterix.json"
    matterix_path.write_text(json.dumps(matterix_mapping), encoding="utf-8")

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "asset_name": binding["asset_name"],
                "child_ids": [*binding["child_ids"].values(), *binding["non_tip_child_ids"]],
                "evidence": "TEST_ONLY: runtime NestedRigidObject child enumeration",
            }
        ),
        encoding="utf-8",
    )
    return connector_path, matterix_path, manifest_path


def test_check_strict_rejects_unverified_example(capsys) -> None:
    code = main(["check", "--strict", "--json"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


def test_build_candidate_validates_manifest_and_preserves_active_config(tmp_path: Path, capsys) -> None:
    connector_path, matterix_path, manifest_path = _candidate_inputs(tmp_path)
    before = matterix_path.read_bytes()
    output = tmp_path / "candidate.json"
    code = main(
        [
            "build-candidate",
            "--config",
            str(matterix_path),
            "--connector-config",
            str(connector_path),
            "--manifest",
            str(manifest_path),
            "--labware-id",
            "tips_300",
            "--binding-id",
            "TEST-RUNTIME-MANIFEST-1",
            "--output",
            str(output),
        ]
    )
    assert code == 0
    assert matterix_path.read_bytes() == before
    assert output.with_suffix(".json.sha256").is_file()
    candidate = OT2MatterixAssetConfig.from_file(output)
    binding = candidate.tip_rack_bindings[0]
    assert binding.verified is True
    assert binding.manifest_sha256 == nested_manifest_sha256(
        binding.asset_name,
        [*binding.child_ids.values(), *binding.non_tip_child_ids],
    )
    assert "active_config_unchanged=true" in capsys.readouterr().out


def test_build_candidate_rejects_manifest_drift(tmp_path: Path, capsys) -> None:
    connector_path, matterix_path, manifest_path = _candidate_inputs(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["child_ids"][-1] = "unexpected_rigid_body"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    code = main(
        [
            "build-candidate",
            "--config",
            str(matterix_path),
            "--connector-config",
            str(connector_path),
            "--manifest",
            str(manifest_path),
            "--labware-id",
            "tips_300",
            "--binding-id",
            "TEST-RUNTIME-MANIFEST-1",
            "--output",
            str(tmp_path / "candidate.json"),
        ]
    )
    assert code == 1
    assert "does not match" in capsys.readouterr().err
