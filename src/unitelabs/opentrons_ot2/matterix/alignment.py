"""
Versioned OT-2 joint-level alignment between the connector and Matterix.

The real OT-2 exposes positions in millimetres while Isaac Lab represents its
prismatic joints in metres.  After unit normalization, every modeled axis uses
the same affine contract::

    q_real = sign * q_sim + offset

``sign`` is restricted to ``-1`` or ``+1``.  A profile is executable only when
the reference position, direction, and both endpoints of the motion range all
agree with that equation.  Unverified source defaults may be recorded for an
operator worksheet, but they can never be used to produce simulation commands.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence

ALIGNMENT_SCHEMA_VERSION = "1.0"
ALIGNMENT_EQUATION = "q_real = sign * q_sim + offset"
REAL_AXES = ("X", "Y", "Z", "A", "B", "C")
JOINT_AXES = ("X", "Y", "Z", "A")
SEMANTIC_AXES = ("B", "C")

_JOINT_MODE = "JOINT"
_SEMANTIC_MODE = "SEMANTIC"
_VERIFIED = "VERIFIED"
_UNVERIFIED = "UNVERIFIED"
_NOT_MODELED = "NOT_MODELED"
_POSITION_TOLERANCE = 1.0e-6


class JointAlignmentError(ValueError):
    """Joint alignment data is incomplete, inconsistent, or out of range."""


def _number(value: object, field: str) -> float | None:
    """Parse a finite optional number without accepting booleans."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{field} must be a finite number or null"
        raise JointAlignmentError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"{field} must be finite"
        raise JointAlignmentError(msg)
    return result


def _required_number(value: float | None, field: str) -> float:
    if value is None:
        msg = f"{field} is required for a verified joint"
        raise JointAlignmentError(msg)
    return value


@dataclasses.dataclass(frozen=True)
class AxisAlignment:
    """Alignment and evidence for one real OT-2 axis."""

    axis: str
    mode: str
    status: str
    sim_joint: str | None
    sign: int | None
    offset: float | None
    real_reference: float | None
    sim_reference: float | None
    real_min: float | None
    real_max: float | None
    sim_min: float | None
    sim_max: float | None
    evidence: str
    real_input_scale: float = dataclasses.field(repr=False)

    @classmethod
    def from_mapping(
        cls,
        axis: str,
        value: Mapping[str, object],
        *,
        real_input_scale: float,
    ) -> AxisAlignment:
        """Parse one strict axis mapping."""
        allowed = {
            "mode",
            "status",
            "sim_joint",
            "sign",
            "offset",
            "real_reference",
            "sim_reference",
            "real_min",
            "real_max",
            "sim_min",
            "sim_max",
            "evidence",
        }
        unknown = set(value) - allowed
        if unknown:
            msg = f"joint_alignment.axes.{axis} contains unknown fields {sorted(unknown)}"
            raise JointAlignmentError(msg)

        raw_sign = value.get("sign")
        if raw_sign is None:
            sign = None
        elif isinstance(raw_sign, bool) or raw_sign not in (-1, 1):
            msg = f"joint_alignment.axes.{axis}.sign must be -1, +1, or null"
            raise JointAlignmentError(msg)
        else:
            sign = int(raw_sign)

        raw_sim_joint = value.get("sim_joint")
        sim_joint = None if raw_sim_joint is None else str(raw_sim_joint).strip()
        evidence = str(value.get("evidence", "")).strip()
        result = cls(
            axis=axis,
            mode=str(value.get("mode", "")).upper(),
            status=str(value.get("status", "")).upper(),
            sim_joint=sim_joint,
            sign=sign,
            offset=_number(value.get("offset"), f"joint_alignment.axes.{axis}.offset"),
            real_reference=_number(
                value.get("real_reference"),
                f"joint_alignment.axes.{axis}.real_reference",
            ),
            sim_reference=_number(
                value.get("sim_reference"),
                f"joint_alignment.axes.{axis}.sim_reference",
            ),
            real_min=_number(value.get("real_min"), f"joint_alignment.axes.{axis}.real_min"),
            real_max=_number(value.get("real_max"), f"joint_alignment.axes.{axis}.real_max"),
            sim_min=_number(value.get("sim_min"), f"joint_alignment.axes.{axis}.sim_min"),
            sim_max=_number(value.get("sim_max"), f"joint_alignment.axes.{axis}.sim_max"),
            evidence=evidence,
            real_input_scale=real_input_scale,
        )
        result._validate()
        return result

    @property
    def verified(self) -> bool:
        """Return whether this physical joint has complete verified evidence."""
        return self.mode == _JOINT_MODE and self.status == _VERIFIED

    def _validate(self) -> None:
        if self.mode not in {_JOINT_MODE, _SEMANTIC_MODE}:
            msg = f"Axis {self.axis} mode must be JOINT or SEMANTIC"
            raise JointAlignmentError(msg)
        if self.status not in {_VERIFIED, _UNVERIFIED, _NOT_MODELED}:
            msg = f"Axis {self.axis} status must be VERIFIED, UNVERIFIED, or NOT_MODELED"
            raise JointAlignmentError(msg)
        if not self.evidence:
            msg = f"Axis {self.axis} evidence must explain the data source"
            raise JointAlignmentError(msg)
        self._validate_interval(self.real_min, self.real_max, "real")
        self._validate_interval(self.sim_min, self.sim_max, "sim")

        if self.mode == _SEMANTIC_MODE:
            self._validate_semantic_axis()
            return
        if not self.sim_joint:
            msg = f"Axis {self.axis} must name its simulation joint"
            raise JointAlignmentError(msg)
        if self.status == _NOT_MODELED:
            msg = f"Modeled axis {self.axis} cannot have NOT_MODELED status"
            raise JointAlignmentError(msg)
        if self.status == _VERIFIED:
            self._validate_verified_joint()

    def _validate_semantic_axis(self) -> None:
        if self.status != _NOT_MODELED:
            msg = f"Semantic axis {self.axis} must have NOT_MODELED status"
            raise JointAlignmentError(msg)
        fields = {
            "sim_joint": self.sim_joint,
            "sign": self.sign,
            "offset": self.offset,
            "sim_reference": self.sim_reference,
            "sim_min": self.sim_min,
            "sim_max": self.sim_max,
        }
        populated = [name for name, value in fields.items() if value is not None and value != ""]
        if populated:
            msg = f"Semantic axis {self.axis} cannot define simulation-joint fields {populated}"
            raise JointAlignmentError(msg)

    @staticmethod
    def _validate_interval(lower: float | None, upper: float | None, label: str) -> None:
        if lower is not None and upper is not None and lower >= upper:
            msg = f"{label} motion range must satisfy min < max"
            raise JointAlignmentError(msg)

    def _validate_verified_joint(self) -> None:
        sign = self.sign
        if sign not in (-1, 1):
            msg = f"Verified axis {self.axis} requires sign -1 or +1"
            raise JointAlignmentError(msg)
        offset = _required_number(self.offset, f"Axis {self.axis} offset")
        real_reference = _required_number(self.real_reference, f"Axis {self.axis} real_reference")
        sim_reference = _required_number(self.sim_reference, f"Axis {self.axis} sim_reference")
        real_min = _required_number(self.real_min, f"Axis {self.axis} real_min")
        real_max = _required_number(self.real_max, f"Axis {self.axis} real_max")
        sim_min = _required_number(self.sim_min, f"Axis {self.axis} sim_min")
        sim_max = _required_number(self.sim_max, f"Axis {self.axis} sim_max")

        expected_reference = sign * sim_reference + offset
        if not math.isclose(real_reference * self.real_input_scale, expected_reference, abs_tol=_POSITION_TOLERANCE):
            msg = (
                f"Axis {self.axis} reference mismatch: real={real_reference}, sim={sim_reference}, "
                f"sign={sign}, offset={offset}"
            )
            raise JointAlignmentError(msg)

        mapped_limits = sorted((sign * sim_min + offset, sign * sim_max + offset))
        real_limits = (real_min * self.real_input_scale, real_max * self.real_input_scale)
        if not all(
            math.isclose(mapped, real, abs_tol=_POSITION_TOLERANCE)
            for mapped, real in zip(mapped_limits, real_limits, strict=True)
        ):
            msg = (
                f"Axis {self.axis} range mismatch: simulation {sim_min, sim_max} maps to "
                f"{tuple(value / self.real_input_scale for value in mapped_limits)} real units, "
                f"not {real_min, real_max}"
            )
            raise JointAlignmentError(msg)

    def require_verified(self) -> None:
        """Reject use of an unverified or semantic-only axis as a joint."""
        if not self.verified:
            msg = (
                f"Axis {self.axis} is {self.status}/{self.mode}; measure zero/reference, direction, and limits "
                "before producing Matterix joint commands"
            )
            raise JointAlignmentError(msg)

    def real_to_sim(self, real_position: float) -> float:
        """Convert a connector position to an aligned simulation joint position."""
        self.require_verified()
        if not math.isfinite(real_position):
            msg = f"Axis {self.axis} real position must be finite"
            raise JointAlignmentError(msg)
        real_min = _required_number(self.real_min, f"Axis {self.axis} real_min")
        real_max = _required_number(self.real_max, f"Axis {self.axis} real_max")
        if not real_min - _POSITION_TOLERANCE <= real_position <= real_max + _POSITION_TOLERANCE:
            msg = f"Axis {self.axis} real position {real_position} is outside [{real_min}, {real_max}]"
            raise JointAlignmentError(msg)
        sign = self.sign
        if sign not in (-1, 1):
            msg = f"Axis {self.axis} has no verified direction"
            raise JointAlignmentError(msg)
        offset = _required_number(self.offset, f"Axis {self.axis} offset")
        sim_position = (real_position * self.real_input_scale - offset) / sign
        self._check_sim_bounds(sim_position)
        return sim_position

    def sim_to_real(self, sim_position: float) -> float:
        """Convert a simulation joint position to the connector's input unit."""
        self.require_verified()
        if not math.isfinite(sim_position):
            msg = f"Axis {self.axis} simulation position must be finite"
            raise JointAlignmentError(msg)
        self._check_sim_bounds(sim_position)
        sign = self.sign
        if sign not in (-1, 1):
            msg = f"Axis {self.axis} has no verified direction"
            raise JointAlignmentError(msg)
        offset = _required_number(self.offset, f"Axis {self.axis} offset")
        return (sign * sim_position + offset) / self.real_input_scale

    def _check_sim_bounds(self, sim_position: float) -> None:
        sim_min = _required_number(self.sim_min, f"Axis {self.axis} sim_min")
        sim_max = _required_number(self.sim_max, f"Axis {self.axis} sim_max")
        if not sim_min - _POSITION_TOLERANCE <= sim_position <= sim_max + _POSITION_TOLERANCE:
            msg = f"Axis {self.axis} simulation position {sim_position} is outside [{sim_min}, {sim_max}]"
            raise JointAlignmentError(msg)


@dataclasses.dataclass(frozen=True)
class JointAlignmentProfile:
    """Complete six-axis real/simulation alignment contract for one OT-2 asset."""

    schema_version: str
    alignment_id: str
    equation: str
    canonical_unit: str
    real_input_unit: str
    real_input_scale: float
    axes: dict[str, AxisAlignment]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> JointAlignmentProfile:
        """Parse and validate a strict alignment profile."""
        allowed = {
            "schema_version",
            "alignment_id",
            "equation",
            "canonical_unit",
            "real_input_unit",
            "real_input_scale",
            "axes",
        }
        unknown = set(value) - allowed
        if unknown:
            msg = f"joint_alignment contains unknown fields {sorted(unknown)}"
            raise JointAlignmentError(msg)
        scale = _number(value.get("real_input_scale"), "joint_alignment.real_input_scale")
        if scale is None or scale <= 0.0:
            msg = "joint_alignment.real_input_scale must be greater than zero"
            raise JointAlignmentError(msg)
        raw_axes = value.get("axes")
        if not isinstance(raw_axes, Mapping):
            msg = "joint_alignment.axes must be an object"
            raise JointAlignmentError(msg)
        normalized_axes = {str(axis).upper(): raw for axis, raw in raw_axes.items()}
        if set(normalized_axes) != set(REAL_AXES):
            msg = "joint_alignment.axes must contain exactly X, Y, Z, A, B, and C"
            raise JointAlignmentError(msg)
        axes = {}
        for axis in REAL_AXES:
            raw_axis = normalized_axes[axis]
            if not isinstance(raw_axis, Mapping):
                msg = f"joint_alignment.axes.{axis} must be an object"
                raise JointAlignmentError(msg)
            axes[axis] = AxisAlignment.from_mapping(axis, raw_axis, real_input_scale=scale)

        result = cls(
            schema_version=str(value.get("schema_version", "")),
            alignment_id=str(value.get("alignment_id", "")).strip(),
            equation=str(value.get("equation", "")),
            canonical_unit=str(value.get("canonical_unit", "")).strip(),
            real_input_unit=str(value.get("real_input_unit", "")).strip(),
            real_input_scale=scale,
            axes=axes,
        )
        result._validate()
        return result

    @property
    def verified(self) -> bool:
        """Return whether all four USD joints have complete alignment evidence."""
        return all(self.axes[axis].verified for axis in JOINT_AXES)

    @property
    def axis_joints(self) -> dict[str, str]:
        """Return the real-axis to simulation-joint mapping in action order."""
        return {axis: str(self.axes[axis].sim_joint) for axis in JOINT_AXES}

    def _validate(self) -> None:
        if self.schema_version != ALIGNMENT_SCHEMA_VERSION:
            msg = f"joint_alignment.schema_version must be {ALIGNMENT_SCHEMA_VERSION!r}"
            raise JointAlignmentError(msg)
        if not self.alignment_id:
            msg = "joint_alignment.alignment_id must not be empty"
            raise JointAlignmentError(msg)
        if self.equation != ALIGNMENT_EQUATION:
            msg = f"joint_alignment.equation must be {ALIGNMENT_EQUATION!r}"
            raise JointAlignmentError(msg)
        if self.canonical_unit != "metre" or self.real_input_unit != "millimetre":
            msg = "OT-2 alignment requires canonical_unit='metre' and real_input_unit='millimetre'"
            raise JointAlignmentError(msg)
        for axis in JOINT_AXES:
            if self.axes[axis].mode != _JOINT_MODE:
                msg = f"Axis {axis} must be modeled as a JOINT"
                raise JointAlignmentError(msg)
        for axis in SEMANTIC_AXES:
            if self.axes[axis].mode != _SEMANTIC_MODE:
                msg = f"Axis {axis} must be modeled as SEMANTIC because the OT-2 USD has no plunger joint"
                raise JointAlignmentError(msg)
        joints = list(self.axis_joints.values())
        if len(set(joints)) != len(joints):
            msg = "X, Y, Z, and A must map to unique simulation joint names"
            raise JointAlignmentError(msg)
        if self.verified and self.alignment_id.upper() == _UNVERIFIED:
            msg = "A verified joint profile requires a non-placeholder alignment_id"
            raise JointAlignmentError(msg)

    def require_verified(self, axes: Sequence[str] = JOINT_AXES) -> None:
        """Require verified alignment for every requested modeled axis."""
        for raw_axis in axes:
            axis = raw_axis.upper()
            if axis not in self.axes:
                msg = f"Unknown OT-2 axis {raw_axis!r}"
                raise JointAlignmentError(msg)
            self.axes[axis].require_verified()

    def joint_indices(self, axes: Sequence[str]) -> tuple[int, ...]:
        """Resolve real axes into the four-dimensional Matterix action tensor."""
        result = []
        for raw_axis in axes:
            axis = raw_axis.upper()
            if axis not in JOINT_AXES:
                msg = f"Axis {axis} is not a physical joint in the Matterix OT-2 articulation"
                raise JointAlignmentError(msg)
            result.append(JOINT_AXES.index(axis))
        return tuple(result)

    def real_to_sim(self, axis: str, real_position: float) -> float:
        """Convert one connector axis position to an aligned simulation target."""
        normalized = axis.upper()
        if normalized not in self.axes:
            msg = f"Unknown OT-2 axis {axis!r}"
            raise JointAlignmentError(msg)
        return self.axes[normalized].real_to_sim(real_position)

    def sim_to_real(self, axis: str, sim_position: float) -> float:
        """Convert one simulation joint position to a connector axis position."""
        normalized = axis.upper()
        if normalized not in self.axes:
            msg = f"Unknown OT-2 axis {axis!r}"
            raise JointAlignmentError(msg)
        return self.axes[normalized].sim_to_real(sim_position)

    def home_targets(self) -> tuple[float, ...]:
        """Return verified Matterix targets corresponding to real OT-2 home readings."""
        self.require_verified()
        targets = []
        for axis in JOINT_AXES:
            reference = _required_number(self.axes[axis].real_reference, f"Axis {axis} real_reference")
            targets.append(self.axes[axis].real_to_sim(reference))
        return tuple(targets)


def derive_sign_and_offset(
    *,
    real_reference: float,
    sim_reference: float,
    real_delta: float,
    sim_delta: float,
    real_input_scale: float = 0.001,
) -> tuple[int, float]:
    """
    Derive direction and offset from one paired reference and one safe delta.

    The deltas must have the same magnitude after converting the real OT-2
    millimetres to canonical metres.  A magnitude mismatch indicates an asset
    scale problem, which cannot be repaired with a sign and offset alone.
    """
    values = (real_reference, sim_reference, real_delta, sim_delta, real_input_scale)
    if not all(math.isfinite(value) for value in values):
        msg = "Alignment observations must be finite"
        raise JointAlignmentError(msg)
    if real_input_scale <= 0.0 or real_delta == 0.0 or sim_delta == 0.0:
        msg = "Alignment scale and observation deltas must be non-zero"
        raise JointAlignmentError(msg)
    real_delta_canonical = real_delta * real_input_scale
    if not math.isclose(abs(real_delta_canonical), abs(sim_delta), abs_tol=_POSITION_TOLERANCE):
        msg = (
            f"Observed delta magnitude mismatch: real={real_delta_canonical} canonical units, sim={sim_delta}; "
            "check the USD scale before deriving sign/offset"
        )
        raise JointAlignmentError(msg)
    sign = 1 if real_delta_canonical * sim_delta > 0.0 else -1
    offset = real_reference * real_input_scale - sign * sim_reference
    return sign, offset


__all__ = [
    "ALIGNMENT_EQUATION",
    "ALIGNMENT_SCHEMA_VERSION",
    "JOINT_AXES",
    "REAL_AXES",
    "SEMANTIC_AXES",
    "AxisAlignment",
    "JointAlignmentError",
    "JointAlignmentProfile",
    "derive_sign_and_offset",
]
