"""World/base/deck rigid-transform contract tests."""

from __future__ import annotations

import copy
import math

import pytest

from unitelabs.opentrons_ot2.matterix.reference_frames import (
    ReferenceFrameAlignment,
    ReferenceFrameAlignmentError,
    RigidTransform,
    derive_rigid_transform,
)

from tests.matterix.helpers import verified_frame_alignment_mapping


def test_verified_chain_round_trips_deck_and_world_points() -> None:
    alignment = ReferenceFrameAlignment.from_mapping(verified_frame_alignment_mapping())
    world = alignment.deck_point_mm_to_world_m((10.0, 20.0, 30.0))
    assert world == pytest.approx((1.11, 2.22, 3.33))
    assert alignment.world_point_m_to_deck_mm(world) == pytest.approx((10.0, 20.0, 30.0))


def test_transform_composes_translation_and_rotation() -> None:
    half = math.sqrt(0.5)
    world_from_base = RigidTransform("world", "base", (1.0, 0.0, 0.0), (half, 0.0, 0.0, half))
    base_from_deck = RigidTransform("base", "deck", (1.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))
    world_from_deck = world_from_base.compose(base_from_deck)
    assert world_from_deck.transform_point((1.0, 0.0, 0.0)) == pytest.approx((1.0, 2.0, 0.0))


def test_unverified_chain_is_readable_but_not_executable() -> None:
    mapping = verified_frame_alignment_mapping()
    mapping["alignment_id"] = "UNVERIFIED"
    mapping["status"] = "UNVERIFIED"
    alignment = ReferenceFrameAlignment.from_mapping(mapping)
    with pytest.raises(ReferenceFrameAlignmentError, match="UNVERIFIED"):
        alignment.deck_point_mm_to_world_m((0.0, 0.0, 0.0))


def test_non_normalized_quaternion_is_rejected() -> None:
    mapping = verified_frame_alignment_mapping()
    mapping["world_from_base"]["rotation_wxyz"] = [2.0, 0.0, 0.0, 0.0]
    with pytest.raises(ReferenceFrameAlignmentError, match="normalized"):
        ReferenceFrameAlignment.from_mapping(mapping)


def test_kabsch_derives_known_transform_and_reports_residual() -> None:
    source = ((0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.0, 0.1, 0.0), (0.0, 0.0, 0.1))
    target = tuple((1.0 - point[1], 2.0 + point[0], 3.0 + point[2]) for point in source)
    transform, metrics = derive_rigid_transform(
        source,
        target,
        parent_frame="world",
        child_frame="deck",
        max_rms_error_m=1.0e-9,
    )
    assert transform.transform_point((0.02, 0.03, 0.04)) == pytest.approx((0.97, 2.02, 3.04))
    assert metrics["point_count"] == 4
    assert metrics["rms_error_m"] < 1.0e-12


def test_kabsch_rejects_collinear_fiducials() -> None:
    points = ((0.0, 0.0, 0.0), (0.1, 0.0, 0.0), (0.2, 0.0, 0.0))
    with pytest.raises(ReferenceFrameAlignmentError, match="non-collinear"):
        derive_rigid_transform(
            points,
            copy.deepcopy(points),
            parent_frame="world",
            child_frame="deck",
            max_rms_error_m=0.001,
        )
