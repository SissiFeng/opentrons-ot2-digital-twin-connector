"""
Validated world, OT-2 base, and deck reference-frame alignment.

The connector addresses labware in the physical OT-2 deck frame while
Matterix places assets in its world frame.  This module keeps the two rigid
transforms that bridge those coordinate systems explicit and import-safe:

``world_from_base @ base_from_deck``.

Translation values are metres and quaternions use Isaac Lab's ``w, x, y, z``
ordering.  Unverified transforms may be stored as operator worksheets, but
cannot be used to place an executable Matterix scene.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence

import numpy as np

FRAME_ALIGNMENT_SCHEMA_VERSION = "1.0"
_VERIFIED = "VERIFIED"
_UNVERIFIED = "UNVERIFIED"
_QUATERNION_TOLERANCE = 1.0e-5


class ReferenceFrameAlignmentError(ValueError):
    """Reference-frame data is incomplete, inconsistent, or degenerate."""


def _finite_tuple(value: object, length: int, field: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != length:
        msg = f"{field} must contain exactly {length} finite numbers"
        raise ReferenceFrameAlignmentError(msg)
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            msg = f"{field} must contain exactly {length} finite numbers"
            raise ReferenceFrameAlignmentError(msg)
        result.append(float(item))
    if not all(math.isfinite(item) for item in result):
        msg = f"{field} must contain exactly {length} finite numbers"
        raise ReferenceFrameAlignmentError(msg)
    return tuple(result)


def _quaternion_multiply(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def _rotate_vector(
    rotation_wxyz: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    pure = (0.0, *vector)
    conjugate = (rotation_wxyz[0], -rotation_wxyz[1], -rotation_wxyz[2], -rotation_wxyz[3])
    rotated = _quaternion_multiply(_quaternion_multiply(rotation_wxyz, pure), conjugate)
    return rotated[1], rotated[2], rotated[3]


@dataclasses.dataclass(frozen=True)
class RigidTransform:
    """One parent-from-child rigid transform in metres and ``wxyz`` order."""

    parent_frame: str
    child_frame: str
    translation_m: tuple[float, float, float]
    rotation_wxyz: tuple[float, float, float, float]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object], *, field: str) -> RigidTransform:
        """Parse a strict transform mapping."""
        allowed = {"parent_frame", "child_frame", "translation_m", "rotation_wxyz"}
        unknown = set(value) - allowed
        if unknown:
            msg = f"{field} contains unknown fields {sorted(unknown)}"
            raise ReferenceFrameAlignmentError(msg)
        try:
            result = cls(
                parent_frame=str(value["parent_frame"]).strip(),
                child_frame=str(value["child_frame"]).strip(),
                translation_m=_finite_tuple(value["translation_m"], 3, f"{field}.translation_m"),
                rotation_wxyz=_finite_tuple(value["rotation_wxyz"], 4, f"{field}.rotation_wxyz"),
            )
        except KeyError as error:
            msg = f"{field} is missing {error.args[0]!r}"
            raise ReferenceFrameAlignmentError(msg) from error
        result._validate(field)
        return result

    def _validate(self, field: str) -> None:
        if not self.parent_frame or not self.child_frame or self.parent_frame == self.child_frame:
            msg = f"{field} must name two distinct non-empty frames"
            raise ReferenceFrameAlignmentError(msg)
        norm = math.sqrt(sum(value * value for value in self.rotation_wxyz))
        if not math.isclose(norm, 1.0, abs_tol=_QUATERNION_TOLERANCE):
            msg = f"{field}.rotation_wxyz must be normalized"
            raise ReferenceFrameAlignmentError(msg)

    def transform_point(self, point_m: Sequence[float]) -> tuple[float, float, float]:
        """Transform one child-frame point into the parent frame."""
        point = _finite_tuple(point_m, 3, "point_m")
        rotated = _rotate_vector(self.rotation_wxyz, point)
        return tuple(rotated[index] + self.translation_m[index] for index in range(3))  # type: ignore[return-value]

    def transform_orientation(self, rotation_wxyz: Sequence[float]) -> tuple[float, float, float, float]:
        """Rotate a child-frame orientation into the parent frame."""
        rotation = _finite_tuple(rotation_wxyz, 4, "rotation_wxyz")
        norm = math.sqrt(sum(value * value for value in rotation))
        if not math.isclose(norm, 1.0, abs_tol=_QUATERNION_TOLERANCE):
            msg = "rotation_wxyz must be normalized"
            raise ReferenceFrameAlignmentError(msg)
        return _quaternion_multiply(self.rotation_wxyz, rotation)  # type: ignore[arg-type]

    def inverse(self) -> RigidTransform:
        """Return the child-from-parent transform."""
        rotation = (
            self.rotation_wxyz[0],
            -self.rotation_wxyz[1],
            -self.rotation_wxyz[2],
            -self.rotation_wxyz[3],
        )
        inverse_translation = _rotate_vector(rotation, tuple(-value for value in self.translation_m))
        return RigidTransform(
            parent_frame=self.child_frame,
            child_frame=self.parent_frame,
            translation_m=inverse_translation,
            rotation_wxyz=rotation,
        )

    def compose(self, child: RigidTransform) -> RigidTransform:
        """Compose ``self @ child`` after checking the shared frame."""
        if self.child_frame != child.parent_frame:
            msg = (
                f"Cannot compose {self.parent_frame}_from_{self.child_frame} with "
                f"{child.parent_frame}_from_{child.child_frame}"
            )
            raise ReferenceFrameAlignmentError(msg)
        return RigidTransform(
            parent_frame=self.parent_frame,
            child_frame=child.child_frame,
            translation_m=self.transform_point(child.translation_m),
            rotation_wxyz=_quaternion_multiply(self.rotation_wxyz, child.rotation_wxyz),
        )

    def as_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ReferenceFrameAlignment:
    """Audited transform chain from the OT-2 deck to Matterix world."""

    schema_version: str
    alignment_id: str
    status: str
    world_from_base: RigidTransform
    base_from_deck: RigidTransform
    evidence: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ReferenceFrameAlignment:
        """Parse and validate a strict frame-alignment profile."""
        allowed = {
            "schema_version",
            "alignment_id",
            "status",
            "world_from_base",
            "base_from_deck",
            "evidence",
        }
        unknown = set(value) - allowed
        if unknown:
            msg = f"reference_frame_alignment contains unknown fields {sorted(unknown)}"
            raise ReferenceFrameAlignmentError(msg)
        try:
            world_from_base_value = value["world_from_base"]
            base_from_deck_value = value["base_from_deck"]
            if not isinstance(world_from_base_value, Mapping) or not isinstance(base_from_deck_value, Mapping):
                msg = "reference-frame transforms must be objects"
                raise TypeError(msg)
            result = cls(
                schema_version=str(value["schema_version"]),
                alignment_id=str(value["alignment_id"]).strip(),
                status=str(value["status"]).upper(),
                world_from_base=RigidTransform.from_mapping(
                    world_from_base_value,
                    field="reference_frame_alignment.world_from_base",
                ),
                base_from_deck=RigidTransform.from_mapping(
                    base_from_deck_value,
                    field="reference_frame_alignment.base_from_deck",
                ),
                evidence=str(value["evidence"]).strip(),
            )
        except (KeyError, TypeError) as error:
            msg = f"reference_frame_alignment is missing or invalid: {error}"
            raise ReferenceFrameAlignmentError(msg) from error
        result._validate()
        return result

    def _validate(self) -> None:
        if self.schema_version != FRAME_ALIGNMENT_SCHEMA_VERSION:
            msg = f"reference_frame_alignment.schema_version must be {FRAME_ALIGNMENT_SCHEMA_VERSION!r}"
            raise ReferenceFrameAlignmentError(msg)
        if self.status not in {_VERIFIED, _UNVERIFIED}:
            msg = "reference_frame_alignment.status must be VERIFIED or UNVERIFIED"
            raise ReferenceFrameAlignmentError(msg)
        if not self.alignment_id or not self.evidence:
            msg = "reference_frame_alignment alignment_id and evidence must not be empty"
            raise ReferenceFrameAlignmentError(msg)
        if self.world_from_base.child_frame != self.base_from_deck.parent_frame:
            msg = "world_from_base.child_frame must equal base_from_deck.parent_frame"
            raise ReferenceFrameAlignmentError(msg)
        names = {
            self.world_from_base.parent_frame,
            self.world_from_base.child_frame,
            self.base_from_deck.child_frame,
        }
        if len(names) != 3:
            msg = "world, base, and deck frame names must be distinct"
            raise ReferenceFrameAlignmentError(msg)
        if self.status == _VERIFIED and self.alignment_id.upper().startswith("UNVERIFIED"):
            msg = "A VERIFIED reference-frame profile cannot use an UNVERIFIED alignment_id"
            raise ReferenceFrameAlignmentError(msg)

    @property
    def verified(self) -> bool:
        """Return whether the transform chain has accepted evidence."""
        return self.status == _VERIFIED

    @property
    def world_from_deck(self) -> RigidTransform:
        """Return the composed world-from-deck transform."""
        return self.world_from_base.compose(self.base_from_deck)

    def require_verified(self) -> None:
        """Reject executable placement from an unverified profile."""
        if not self.verified:
            msg = (
                "Reference-frame alignment is UNVERIFIED; measure and approve world/base/deck "
                "fiducials before placing the OT-2 or labware in Matterix"
            )
            raise ReferenceFrameAlignmentError(msg)

    def deck_point_mm_to_world_m(self, point_mm: Sequence[float]) -> tuple[float, float, float]:
        """Convert an OT-2 deck point in millimetres into Matterix world metres."""
        self.require_verified()
        point = _finite_tuple(point_mm, 3, "point_mm")
        return self.world_from_deck.transform_point(tuple(value * 0.001 for value in point))

    def world_point_m_to_deck_mm(self, point_m: Sequence[float]) -> tuple[float, float, float]:
        """Convert a Matterix world point back into OT-2 deck millimetres."""
        self.require_verified()
        point = self.world_from_deck.inverse().transform_point(point_m)
        return tuple(value * 1000.0 for value in point)


def derive_rigid_transform(
    source_points_m: Sequence[Sequence[float]],
    target_points_m: Sequence[Sequence[float]],
    *,
    parent_frame: str,
    child_frame: str,
    max_rms_error_m: float,
) -> tuple[RigidTransform, dict[str, float | int]]:
    """Derive a rigid transform from at least three non-collinear point pairs."""
    if not math.isfinite(max_rms_error_m) or max_rms_error_m < 0.0:
        msg = "max_rms_error_m must be finite and non-negative"
        raise ReferenceFrameAlignmentError(msg)
    try:
        source = np.asarray(source_points_m, dtype=float)
        target = np.asarray(target_points_m, dtype=float)
    except (TypeError, ValueError) as error:
        msg = "Frame observations must be numeric point arrays"
        raise ReferenceFrameAlignmentError(msg) from error
    if source.shape != target.shape or source.ndim != 2 or source.shape[1:] != (3,) or source.shape[0] < 3:
        msg = "Frame derivation requires matching N x 3 arrays with at least three point pairs"
        raise ReferenceFrameAlignmentError(msg)
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        msg = "Frame observations must be finite"
        raise ReferenceFrameAlignmentError(msg)

    source_centered = source - source.mean(axis=0)
    target_centered = target - target.mean(axis=0)
    source_rank = int(np.linalg.matrix_rank(source_centered))
    target_rank = int(np.linalg.matrix_rank(target_centered))
    if min(source_rank, target_rank) < 2:
        msg = "Frame observations are collinear or coincident; use at least three non-collinear fiducials"
        raise ReferenceFrameAlignmentError(msg)

    covariance = source_centered.T @ target_centered
    left, _singular_values, right_t = np.linalg.svd(covariance)
    rotation = right_t.T @ left.T
    if np.linalg.det(rotation) < 0.0:
        right_t[-1, :] *= -1.0
        rotation = right_t.T @ left.T
    translation = target.mean(axis=0) - rotation @ source.mean(axis=0)
    predicted = (rotation @ source.T).T + translation
    errors = np.linalg.norm(predicted - target, axis=1)
    rms_error = float(np.sqrt(np.mean(np.square(errors))))
    max_error = float(np.max(errors))
    if rms_error > max_rms_error_m:
        msg = f"Frame alignment RMS error {rms_error:.9g} m exceeds {max_rms_error_m:.9g} m"
        raise ReferenceFrameAlignmentError(msg)

    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        quaternion = (
            0.25 * scale,
            (rotation[2, 1] - rotation[1, 2]) / scale,
            (rotation[0, 2] - rotation[2, 0]) / scale,
            (rotation[1, 0] - rotation[0, 1]) / scale,
        )
    else:
        diagonal = np.diag(rotation)
        index = int(np.argmax(diagonal))
        if index == 0:
            scale = math.sqrt(1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2]) * 2.0
            quaternion = (
                (rotation[2, 1] - rotation[1, 2]) / scale,
                0.25 * scale,
                (rotation[0, 1] + rotation[1, 0]) / scale,
                (rotation[0, 2] + rotation[2, 0]) / scale,
            )
        elif index == 1:
            scale = math.sqrt(1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2]) * 2.0
            quaternion = (
                (rotation[0, 2] - rotation[2, 0]) / scale,
                (rotation[0, 1] + rotation[1, 0]) / scale,
                0.25 * scale,
                (rotation[1, 2] + rotation[2, 1]) / scale,
            )
        else:
            scale = math.sqrt(1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1]) * 2.0
            quaternion = (
                (rotation[1, 0] - rotation[0, 1]) / scale,
                (rotation[0, 2] + rotation[2, 0]) / scale,
                (rotation[1, 2] + rotation[2, 1]) / scale,
                0.25 * scale,
            )
    norm = math.sqrt(sum(value * value for value in quaternion))
    normalized = tuple(float(value / norm) for value in quaternion)
    transform = RigidTransform(
        parent_frame=parent_frame,
        child_frame=child_frame,
        translation_m=tuple(float(value) for value in translation),
        rotation_wxyz=normalized,
    )
    return transform, {
        "point_count": int(source.shape[0]),
        "source_rank": source_rank,
        "target_rank": target_rank,
        "rms_error_m": rms_error,
        "max_error_m": max_error,
    }


__all__ = [
    "FRAME_ALIGNMENT_SCHEMA_VERSION",
    "ReferenceFrameAlignment",
    "ReferenceFrameAlignmentError",
    "RigidTransform",
    "derive_rigid_transform",
]
