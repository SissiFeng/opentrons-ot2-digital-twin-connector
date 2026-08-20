"""Individual tip-rack well to nested-child binding tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig
from unitelabs.opentrons_ot2.matterix.tip_rack import (
    TipRackBinding,
    TipRackBindingError,
    nested_manifest_sha256,
)

from tests.digital_twin.helpers import config_mapping
from tests.matterix.helpers import verified_tip_binding, verified_tip_binding_mapping


@pytest.fixture
def connector(tmp_path: Path) -> DigitalTwinConfig:
    mapping = config_mapping(tmp_path / "state.json")
    mapping["instruments"][0]["channels"] = 1
    mapping["instruments"][0]["expected_model"] = "p300_single_v2.1"
    return DigitalTwinConfig.from_mapping(mapping)


def test_verified_binding_maps_all_96_wells_uniquely(connector: DigitalTwinConfig) -> None:
    binding = verified_tip_binding()
    child_ids = binding.validate_connector(connector)
    assert len(child_ids) == len(set(child_ids)) == 96
    assert child_ids[0] == "pipette_tip_mesh_00"
    assert child_ids[-1] == "pipette_tip_mesh_95"


def test_manifest_must_equal_well_mapping(connector: DigitalTwinConfig) -> None:
    binding = verified_tip_binding()
    child_ids = [*binding.validate_connector(connector), *binding.non_tip_child_ids]
    binding.validate_manifest(connector, child_ids)
    child_ids[-1] = "Tips__EXTRA"
    with pytest.raises(TipRackBindingError, match="does not match"):
        binding.validate_manifest(connector, child_ids)


def test_unverified_binding_cannot_drive_attachment(connector: DigitalTwinConfig) -> None:
    mapping = verified_tip_binding_mapping()
    mapping["binding_id"] = "UNVERIFIED"
    mapping["status"] = "UNVERIFIED"
    mapping["manifest_sha256"] = "UNVERIFIED"
    binding = TipRackBinding.from_mapping(mapping, index=0)
    with pytest.raises(TipRackBindingError, match="UNVERIFIED"):
        binding.validate_connector(connector)


def test_unverified_candidate_manifest_can_be_checked_before_acceptance(connector: DigitalTwinConfig) -> None:
    mapping = verified_tip_binding_mapping()
    mapping["binding_id"] = "UNVERIFIED"
    mapping["status"] = "UNVERIFIED"
    mapping["manifest_sha256"] = "UNVERIFIED"
    binding = TipRackBinding.from_mapping(mapping, index=0)
    child_ids = [*binding.child_ids.values(), *binding.non_tip_child_ids]
    binding.validate_manifest(connector, child_ids)


def test_multi_channel_profile_fails_until_semantics_support_child_groups(tmp_path: Path) -> None:
    connector = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    with pytest.raises(TipRackBindingError, match="multi-channel attachment"):
        verified_tip_binding().validate_connector(connector)


def test_unknown_well_has_no_implicit_name_fallback() -> None:
    mapping = verified_tip_binding_mapping()
    binding = TipRackBinding.from_mapping(mapping, index=0)
    with pytest.raises(TipRackBindingError, match="no nested child mapping"):
        binding.child_id("Z99")


def test_checked_config_pins_source_inspected_manifest_without_claiming_runtime_acceptance() -> None:
    config = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    raw = config["tip_rack_bindings"][0]
    binding = TipRackBinding.from_mapping(raw, index=0)
    configured_children = [*binding.child_ids.values(), *binding.non_tip_child_ids]

    assert binding.status == "UNVERIFIED"
    assert binding.manifest_sha256 == nested_manifest_sha256(binding.asset_name, configured_children)
    assert "tip-rack-runtime-manifest.json" in binding.evidence
