"""Build a verified joint-alignment candidate from real and simulation evidence."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from .alignment import (
    ALIGNMENT_EQUATION,
    ALIGNMENT_SCHEMA_VERSION,
    JOINT_AXES,
    SEMANTIC_AXES,
    AxisAlignment,
    JointAlignmentError,
    JointAlignmentProfile,
    derive_sign_and_offset,
)

SIMULATION_EVIDENCE_SCHEMA_VERSION = "1.0"


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{field} must be a finite number"
        raise JointAlignmentError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"{field} must be a finite number"
        raise JointAlignmentError(msg)
    return result


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        msg = f"{field} must be an object"
        raise JointAlignmentError(msg)
    return value


def _sha256(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _semantic_axis_mapping(item: AxisAlignment) -> dict[str, object]:
    return {
        "mode": item.mode,
        "status": item.status,
        "sim_joint": item.sim_joint,
        "sign": item.sign,
        "offset": item.offset,
        "real_reference": item.real_reference,
        "sim_reference": item.sim_reference,
        "real_min": item.real_min,
        "real_max": item.real_max,
        "sim_min": item.sim_min,
        "sim_max": item.sim_max,
        "evidence": item.evidence,
    }


def build_verified_alignment_candidate(
    template: JointAlignmentProfile,
    hardware_evidence: Mapping[str, object],
    simulation_evidence: Mapping[str, object],
    *,
    alignment_id: str,
    hardware_evidence_label: str,
    simulation_evidence_label: str,
) -> JointAlignmentProfile:
    """Combine complete endpoint evidence into a self-validating alignment."""
    normalized_id = alignment_id.strip()
    if not normalized_id or normalized_id.upper().startswith("UNVERIFIED"):
        msg = "alignment_id must identify the reviewed evidence and cannot start with UNVERIFIED"
        raise JointAlignmentError(msg)
    if hardware_evidence.get("status") != "COMPLETE":
        msg = "Hardware calibration evidence status must be COMPLETE"
        raise JointAlignmentError(msg)
    if simulation_evidence.get("schema_version") != SIMULATION_EVIDENCE_SCHEMA_VERSION:
        msg = f"Simulation evidence schema_version must be {SIMULATION_EVIDENCE_SCHEMA_VERSION!r}"
        raise JointAlignmentError(msg)
    if simulation_evidence.get("status") != "COMPLETE":
        msg = "Simulation alignment evidence status must be COMPLETE"
        raise JointAlignmentError(msg)
    hardware_axes = _mapping(hardware_evidence.get("axis_results"), "hardware.axis_results")
    simulation_axes = _mapping(simulation_evidence.get("axes"), "simulation.axes")
    if set(hardware_axes) != set(JOINT_AXES) or set(simulation_axes) != set(JOINT_AXES):
        msg = "Hardware and simulation evidence must each contain exactly X, Y, Z, and A"
        raise JointAlignmentError(msg)

    hardware_digest = _sha256(hardware_evidence)
    simulation_digest = _sha256(simulation_evidence)
    axes: dict[str, object] = {}
    for axis in JOINT_AXES:
        hardware = _mapping(hardware_axes[axis], f"hardware.axis_results.{axis}")
        simulation = _mapping(simulation_axes[axis], f"simulation.axes.{axis}")
        if hardware.get("probe_pass") is not True:
            msg = f"Hardware axis {axis} probe_pass must be true"
            raise JointAlignmentError(msg)
        endpoints = _mapping(hardware.get("endpoints"), f"hardware.axis_results.{axis}.endpoints")
        if endpoints.get("pass") is not True:
            msg = f"Hardware axis {axis} endpoint replay must pass"
            raise JointAlignmentError(msg)
        if simulation.get("probe_pass") is not True or simulation.get("limits_pass") is not True:
            msg = f"Simulation axis {axis} probe and limit checks must pass"
            raise JointAlignmentError(msg)

        real_reference = _finite(hardware.get("reference_mm"), f"hardware.{axis}.reference_mm")
        real_delta = _finite(hardware.get("observed_delta_mm"), f"hardware.{axis}.observed_delta_mm")
        real_limits = sorted(
            (
                _finite(endpoints.get("observed_min_mm"), f"hardware.{axis}.observed_min_mm"),
                _finite(endpoints.get("observed_max_mm"), f"hardware.{axis}.observed_max_mm"),
            )
        )
        sim_reference = _finite(simulation.get("reference_m"), f"simulation.{axis}.reference_m")
        sim_delta = _finite(simulation.get("observed_delta_m"), f"simulation.{axis}.observed_delta_m")
        sim_limits = sorted(
            (
                _finite(simulation.get("min_m"), f"simulation.{axis}.min_m"),
                _finite(simulation.get("max_m"), f"simulation.{axis}.max_m"),
            )
        )
        sign, offset = derive_sign_and_offset(
            real_reference=real_reference,
            sim_reference=sim_reference,
            real_delta=real_delta,
            sim_delta=sim_delta,
            real_input_scale=template.real_input_scale,
        )
        template_axis = template.axes[axis]
        axes[axis] = {
            "mode": "JOINT",
            "status": "VERIFIED",
            "sim_joint": template_axis.sim_joint,
            "sign": sign,
            "offset": offset,
            "real_reference": real_reference,
            "sim_reference": sim_reference,
            "real_min": real_limits[0],
            "real_max": real_limits[1],
            "sim_min": sim_limits[0],
            "sim_max": sim_limits[1],
            "evidence": (
                f"PAIRED_EVIDENCE: {hardware_evidence_label} sha256={hardware_digest}; "
                f"{simulation_evidence_label} sha256={simulation_digest}"
            ),
        }
    for axis in SEMANTIC_AXES:
        axes[axis] = _semantic_axis_mapping(template.axes[axis])

    return JointAlignmentProfile.from_mapping(
        {
            "schema_version": ALIGNMENT_SCHEMA_VERSION,
            "alignment_id": normalized_id,
            "equation": ALIGNMENT_EQUATION,
            "canonical_unit": template.canonical_unit,
            "real_input_unit": template.real_input_unit,
            "real_input_scale": template.real_input_scale,
            "axes": axes,
        }
    )


def alignment_as_mapping(profile: JointAlignmentProfile) -> dict[str, object]:
    """Serialize an alignment profile without leaking dataclass-only fields."""
    return {
        "schema_version": profile.schema_version,
        "alignment_id": profile.alignment_id,
        "equation": profile.equation,
        "canonical_unit": profile.canonical_unit,
        "real_input_unit": profile.real_input_unit,
        "real_input_scale": profile.real_input_scale,
        "axes": {axis: _semantic_axis_mapping(item) for axis, item in profile.axes.items()},
    }


__all__ = [
    "SIMULATION_EVIDENCE_SCHEMA_VERSION",
    "alignment_as_mapping",
    "build_verified_alignment_candidate",
]
