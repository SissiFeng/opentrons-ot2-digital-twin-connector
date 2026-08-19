"""OT-2 joint-level zero/reference, direction, and range contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.matterix.alignment import (
    JointAlignmentError,
    JointAlignmentProfile,
    derive_sign_and_offset,
)
from unitelabs.opentrons_ot2.matterix.asset import MatterixAssetConfigurationError, OT2MatterixAssetConfig

from tests.matterix.helpers import verified_alignment, verified_alignment_mapping


def test_affine_alignment_round_trips_both_directions() -> None:
    alignment = verified_alignment()
    assert alignment.real_to_sim("X", 100.0) == pytest.approx(-0.02)
    assert alignment.sim_to_real("X", -0.02) == pytest.approx(100.0)
    assert alignment.real_to_sim("Y", 200.0) == pytest.approx(-0.02)
    assert alignment.sim_to_real("Y", -0.02) == pytest.approx(200.0)


def test_verified_profile_maps_real_ranges_to_sim_ranges() -> None:
    alignment = verified_alignment()
    for axis in ("X", "Y", "Z", "A"):
        item = alignment.axes[axis]
        mapped = sorted((alignment.real_to_sim(axis, item.real_min), alignment.real_to_sim(axis, item.real_max)))
        assert mapped == pytest.approx([item.sim_min, item.sim_max])


def test_derive_sign_and_offset_from_paired_observations() -> None:
    assert derive_sign_and_offset(
        real_reference=100.0,
        sim_reference=0.05,
        real_delta=10.0,
        sim_delta=-0.01,
    ) == pytest.approx((-1, 0.15))


def test_derive_rejects_scale_mismatch() -> None:
    with pytest.raises(JointAlignmentError, match="magnitude mismatch"):
        derive_sign_and_offset(
            real_reference=100.0,
            sim_reference=0.05,
            real_delta=10.0,
            sim_delta=0.02,
        )


def test_verified_profile_rejects_reference_mismatch() -> None:
    mapping = verified_alignment_mapping()
    mapping["axes"]["X"]["real_reference"] = 319.0
    with pytest.raises(JointAlignmentError, match="reference mismatch"):
        JointAlignmentProfile.from_mapping(mapping)


def test_verified_profile_rejects_range_mismatch() -> None:
    mapping = verified_alignment_mapping()
    mapping["axes"]["X"]["real_max"] = 319.0
    with pytest.raises(JointAlignmentError, match="range mismatch"):
        JointAlignmentProfile.from_mapping(mapping)


def test_unverified_checked_profile_is_readable_but_not_executable() -> None:
    asset = OT2MatterixAssetConfig.from_file("config/matterix_ot2.json")
    assert asset.joint_alignment.verified is False
    assert set(asset.axis_joints) == {"X", "Y", "Z", "A"}
    with pytest.raises(MatterixAssetConfigurationError, match="measure zero/reference"):
        asset.validate_joint_alignment()


def test_b_and_c_are_explicitly_semantic_only() -> None:
    alignment = verified_alignment()
    for axis in ("B", "C"):
        assert alignment.axes[axis].mode == "SEMANTIC"
        assert alignment.axes[axis].sim_joint is None
        with pytest.raises(JointAlignmentError, match="not a physical joint"):
            alignment.joint_indices((axis,))


def test_out_of_range_real_target_fails_closed() -> None:
    with pytest.raises(JointAlignmentError, match="outside"):
        verified_alignment().real_to_sim("X", 500.0)


def test_asset_config_rejects_legacy_six_joint_mapping() -> None:
    value = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    value["axis_joints"] = {axis: axis.lower() for axis in "XYZABC"}
    with pytest.raises(MatterixAssetConfigurationError, match="unknown fields"):
        OT2MatterixAssetConfig.from_mapping(value)


def test_unverified_axis_cannot_be_renamed_verified_without_measurements() -> None:
    value = json.loads(Path("config/matterix_ot2.json").read_text(encoding="utf-8"))
    value = copy.deepcopy(value["joint_alignment"])
    value["alignment_id"] = "FAKE"
    value["axes"]["X"]["status"] = "VERIFIED"
    with pytest.raises(JointAlignmentError, match="requires sign"):
        JointAlignmentProfile.from_mapping(value)
