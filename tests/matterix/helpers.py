"""Reusable verified fixtures for import-safe Matterix tests."""

from __future__ import annotations

from unitelabs.opentrons_ot2.matterix.alignment import ALIGNMENT_EQUATION, JointAlignmentProfile
from unitelabs.opentrons_ot2.matterix.tip_rack import TipRackBinding, nested_manifest_sha256


def verified_alignment_mapping() -> dict[str, object]:
    """Return a synthetic, internally consistent four-joint alignment."""
    axes: dict[str, object] = {
        "X": _joint("PrismaticJointMiddleBar", 1, 0.12, 320.0, 0.2, 0.0, 420.0, -0.12, 0.3),
        "Y": _joint("PrismaticJointPipetteHolder", -1, 0.18, 0.0, 0.18, 0.0, 360.0, -0.18, 0.18),
        "Z": _joint("PrismaticJointLeftPipette", -1, 0.1, 100.0, 0.0, -100.0, 200.0, -0.1, 0.2),
        "A": _joint("PrismaticJointRightPipette", 1, 0.1, 100.0, 0.0, 0.0, 300.0, -0.1, 0.2),
        "B": _semantic("left plunger is represented by Matterix aspiration/dispensing semantics"),
        "C": _semantic("right plunger is represented by Matterix aspiration/dispensing semantics"),
    }
    return {
        "schema_version": "1.0",
        "alignment_id": "TEST-ALIGNMENT",
        "equation": ALIGNMENT_EQUATION,
        "canonical_unit": "metre",
        "real_input_unit": "millimetre",
        "real_input_scale": 0.001,
        "axes": axes,
    }


def verified_alignment() -> JointAlignmentProfile:
    """Build the synthetic verified profile."""
    return JointAlignmentProfile.from_mapping(verified_alignment_mapping())


def verified_frame_alignment_mapping() -> dict[str, object]:
    """Return a synthetic verified world/base/deck transform chain."""
    return {
        "schema_version": "1.0",
        "alignment_id": "TEST-FRAMES",
        "status": "VERIFIED",
        "world_from_base": {
            "parent_frame": "matterix_world",
            "child_frame": "ot2_base",
            "translation_m": [1.0, 2.0, 3.0],
            "rotation_wxyz": [1.0, 0.0, 0.0, 0.0],
        },
        "base_from_deck": {
            "parent_frame": "ot2_base",
            "child_frame": "ot2_deck",
            "translation_m": [0.1, 0.2, 0.3],
            "rotation_wxyz": [1.0, 0.0, 0.0, 0.0],
        },
        "evidence": "TEST_ONLY: synthetic surveyed fiducials",
    }


def verified_tip_binding_mapping() -> dict[str, object]:
    """Return a synthetic accepted 96-child tip-rack binding."""
    child_ids = {
        f"{row}{column}": f"pipette_tip_mesh_{(column - 1) * 8 + row_index:02d}"
        for column in range(1, 13)
        for row_index, row in enumerate("ABCDEFGH")
    }
    non_tip_child_ids = ["tiprack_mesh"]
    return {
        "schema_version": "1.0",
        "binding_id": "TEST-TIPS-96",
        "status": "VERIFIED",
        "labware_id": "tips_300",
        "asset_name": "tips",
        "mount": "RIGHT",
        "ee_sensor_name": "ee_frame_ot2",
        "child_ids": child_ids,
        "non_tip_child_ids": non_tip_child_ids,
        "expected_child_count": 96,
        "manifest_sha256": nested_manifest_sha256("tips", [*child_ids.values(), *non_tip_child_ids]),
        "evidence": "TEST_ONLY: synthetic rack plus 96-child nested manifest",
    }


def verified_tip_binding() -> TipRackBinding:
    """Build the synthetic accepted tip-rack binding."""
    return TipRackBinding.from_mapping(verified_tip_binding_mapping(), index=0)


def _joint(
    sim_joint: str,
    sign: int,
    offset: float,
    real_reference: float,
    sim_reference: float,
    real_min: float,
    real_max: float,
    sim_min: float,
    sim_max: float,
) -> dict[str, object]:
    return {
        "mode": "JOINT",
        "status": "VERIFIED",
        "sim_joint": sim_joint,
        "sign": sign,
        "offset": offset,
        "real_reference": real_reference,
        "sim_reference": sim_reference,
        "real_min": real_min,
        "real_max": real_max,
        "sim_min": sim_min,
        "sim_max": sim_max,
        "evidence": "TEST_ONLY: paired real/sim reference, safe delta, and endpoint observations",
    }


def _semantic(evidence: str) -> dict[str, object]:
    return {
        "mode": "SEMANTIC",
        "status": "NOT_MODELED",
        "sim_joint": None,
        "sign": None,
        "offset": None,
        "real_reference": 19.0,
        "sim_reference": None,
        "real_min": -37.0,
        "real_max": 19.0,
        "sim_min": None,
        "sim_max": None,
        "evidence": evidence,
    }
