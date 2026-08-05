"""Validated, immutable configuration shared by the connector, bridge, and twin."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema

DT_CONFIG_SCHEMA_VERSION = "1.0"
_MOUNTS = frozenset({"LEFT", "RIGHT"})
_TIP_ACTUATION_MODES = frozenset({"HARDWARE_API", "CALIBRATED_PLUNGER"})


class DigitalTwinConfigurationError(ValueError):
    """The digital-twin configuration is invalid and must be corrected before startup."""


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    resource = files("unitelabs.opentrons_ot2").joinpath("contracts").joinpath("ot2_dt_config.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _validate_schema(value: dict[str, Any]) -> None:
    validator = jsonschema.Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(value), key=lambda error: tuple(str(item) for item in error.absolute_path))
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "<root>"
        msg = f"Digital-twin configuration schema error at {location}: {first.message}"
        raise DigitalTwinConfigurationError(msg)


@dataclasses.dataclass(frozen=True)
class DeckPoint:
    """A point in the calibrated OT-2 deck coordinate frame, in millimetres."""

    x: float
    y: float
    z: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, field: str) -> DeckPoint:
        """Parse and validate one point."""
        try:
            point = cls(x=float(value["x"]), y=float(value["y"]), z=float(value["z"]))
        except (KeyError, TypeError, ValueError) as error:
            msg = f"{field} must contain finite numeric x, y, and z values"
            raise DigitalTwinConfigurationError(msg) from error
        if not all(math.isfinite(component) for component in dataclasses.astuple(point)):
            msg = f"{field} must contain finite numeric x, y, and z values"
            raise DigitalTwinConfigurationError(msg)
        return point


@dataclasses.dataclass(frozen=True)
class DeckBounds:
    """Allowed logical deck-coordinate range, in millimetres."""

    minimum: DeckPoint
    maximum: DeckPoint

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DeckBounds:
        """Parse deck bounds."""
        result = cls(
            minimum=DeckPoint.from_mapping(value["minimum"], field="deck_bounds.minimum"),
            maximum=DeckPoint.from_mapping(value["maximum"], field="deck_bounds.maximum"),
        )
        if any(
            low >= high
            for low, high in zip(
                dataclasses.astuple(result.minimum),
                dataclasses.astuple(result.maximum),
                strict=True,
            )
        ):
            msg = "Every deck_bounds.minimum component must be lower than deck_bounds.maximum"
            raise DigitalTwinConfigurationError(msg)
        return result

    def contains(self, point: DeckPoint) -> bool:
        """Return whether a point is inside the configured logical deck volume."""
        return (
            self.minimum.x <= point.x <= self.maximum.x
            and self.minimum.y <= point.y <= self.maximum.y
            and self.minimum.z <= point.z <= self.maximum.z
        )


@dataclasses.dataclass(frozen=True)
class WellGrid:
    """Regular well grid resolved relative to a labware placement origin."""

    rows: tuple[str, ...]
    columns: int
    first_well: DeckPoint
    row_spacing: float
    column_spacing: float
    approach_height: float
    working_height: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, field: str) -> WellGrid:
        """Parse a regular well grid."""
        rows = tuple(str(row).upper() for row in value["rows"])
        result = cls(
            rows=rows,
            columns=int(value["columns"]),
            first_well=DeckPoint.from_mapping(value["first_well"], field=f"{field}.first_well"),
            row_spacing=float(value["row_spacing"]),
            column_spacing=float(value["column_spacing"]),
            approach_height=float(value["approach_height"]),
            working_height=float(value["working_height"]),
        )
        numeric = (
            result.row_spacing,
            result.column_spacing,
            result.approach_height,
            result.working_height,
        )
        if (
            not rows
            or len(set(rows)) != len(rows)
            or result.columns < 1
            or not all(math.isfinite(item) for item in numeric)
            or result.row_spacing <= 0
            or result.column_spacing <= 0
        ):
            msg = f"{field} must define unique rows, positive columns and spacing, and finite heights"
            raise DigitalTwinConfigurationError(msg)
        return result

    def resolve(self, well: str, *, placement_origin: DeckPoint, use_approach: bool) -> DeckPoint:
        """Resolve a well label into the calibrated deck frame."""
        normalized = well.strip().upper()
        row = "".join(character for character in normalized if character.isalpha())
        column_text = "".join(character for character in normalized if character.isdigit())
        if row not in self.rows or not column_text:
            msg = f"Unknown well {well!r}; expected rows {self.rows} and columns 1-{self.columns}"
            raise DigitalTwinConfigurationError(msg)
        column = int(column_text)
        if not 1 <= column <= self.columns:
            msg = f"Unknown well {well!r}; expected rows {self.rows} and columns 1-{self.columns}"
            raise DigitalTwinConfigurationError(msg)
        height = self.approach_height if use_approach else self.working_height
        return DeckPoint(
            x=placement_origin.x + self.first_well.x + (column - 1) * self.column_spacing,
            y=placement_origin.y + self.first_well.y + self.rows.index(row) * self.row_spacing,
            z=placement_origin.z + self.first_well.z + height,
        )


@dataclasses.dataclass(frozen=True)
class LabwarePlacement:
    """One named labware placement and its geometry."""

    labware_id: str
    definition_uri: str
    definition_hash: str
    slot: str
    origin: DeckPoint
    grid: WellGrid

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, index: int) -> LabwarePlacement:
        """Parse one labware placement."""
        field = f"labware[{index}]"
        result = cls(
            labware_id=str(value["labware_id"]),
            definition_uri=str(value["definition_uri"]),
            definition_hash=str(value["definition_hash"]),
            slot=str(value["slot"]),
            origin=DeckPoint.from_mapping(value["origin"], field=f"{field}.origin"),
            grid=WellGrid.from_mapping(value["grid"], field=f"{field}.grid"),
        )
        if not result.labware_id or not result.definition_uri or not result.definition_hash or not result.slot:
            msg = f"{field} identifiers must not be empty"
            raise DigitalTwinConfigurationError(msg)
        return result


@dataclasses.dataclass(frozen=True)
class InstrumentProfile:
    """Expected pipette identity, limits, calibration, and tip actuation settings."""

    mount: str
    aliases: tuple[str, ...]
    expected_model: str
    name: str
    channels: int
    minimum_volume: float
    maximum_volume: float
    microlitres_per_millimetre: float
    default_aspirate_flow_rate: float
    default_dispense_flow_rate: float
    nozzle_deck_axis_position: float
    safe_travel_height: float
    tip_length: float
    tip_actuation_mode: str
    pickup_presses: int
    pickup_increment: float
    drop_tip_plunger_position: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, index: int) -> InstrumentProfile:
        """Parse one instrument profile."""
        field = f"instruments[{index}]"
        result = cls(
            mount=str(value["mount"]).upper(),
            aliases=tuple(str(alias) for alias in value["aliases"]),
            expected_model=str(value["expected_model"]),
            name=str(value["name"]),
            channels=int(value["channels"]),
            minimum_volume=float(value["minimum_volume"]),
            maximum_volume=float(value["maximum_volume"]),
            microlitres_per_millimetre=float(value["microlitres_per_millimetre"]),
            default_aspirate_flow_rate=float(value["default_aspirate_flow_rate"]),
            default_dispense_flow_rate=float(value["default_dispense_flow_rate"]),
            nozzle_deck_axis_position=float(value["nozzle_deck_axis_position"]),
            safe_travel_height=float(value["safe_travel_height"]),
            tip_length=float(value["tip_length"]),
            tip_actuation_mode=str(value["tip_actuation_mode"]).upper(),
            pickup_presses=int(value["pickup_presses"]),
            pickup_increment=float(value["pickup_increment"]),
            drop_tip_plunger_position=float(value["drop_tip_plunger_position"]),
        )
        numeric = (
            result.minimum_volume,
            result.maximum_volume,
            result.microlitres_per_millimetre,
            result.default_aspirate_flow_rate,
            result.default_dispense_flow_rate,
            result.nozzle_deck_axis_position,
            result.safe_travel_height,
            result.tip_length,
            result.pickup_increment,
            result.drop_tip_plunger_position,
        )
        if result.mount not in _MOUNTS:
            msg = f"{field}.mount must be LEFT or RIGHT"
            raise DigitalTwinConfigurationError(msg)
        if result.tip_actuation_mode not in _TIP_ACTUATION_MODES:
            msg = f"{field}.tip_actuation_mode must be one of {sorted(_TIP_ACTUATION_MODES)}"
            raise DigitalTwinConfigurationError(msg)
        if (
            not result.aliases
            or len(result.aliases) != len(set(result.aliases))
            or any(not alias for alias in result.aliases)
            or not all(math.isfinite(item) for item in numeric)
            or result.channels < 1
            or result.minimum_volume < 0
            or result.maximum_volume <= result.minimum_volume
            or result.microlitres_per_millimetre <= 0
            or result.default_aspirate_flow_rate <= 0
            or result.default_dispense_flow_rate <= 0
            or result.tip_length <= 0
            or result.pickup_presses < 1
            or result.pickup_increment < 0
        ):
            msg = f"{field} contains invalid pipette limits or calibration values"
            raise DigitalTwinConfigurationError(msg)
        return result


@dataclasses.dataclass(frozen=True)
class DigitalTwinConfig:
    """Immutable configuration snapshot shared across physical and simulated paths."""

    schema_version: str
    config_revision: str
    deck_definition: str
    calibration_id: str
    calibration_confirmed: bool
    deck_bounds: DeckBounds
    deck_offset: DeckPoint
    labware: tuple[LabwarePlacement, ...]
    instruments: tuple[InstrumentProfile, ...]
    trash: DeckPoint
    state_path: str

    @classmethod
    def from_file(cls, path: str | Path) -> DigitalTwinConfig:
        """Load a JSON configuration snapshot."""
        config_path = Path(path)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            msg = f"Unable to load digital-twin configuration from {config_path}: {error}"
            raise DigitalTwinConfigurationError(msg) from error
        if not isinstance(raw, dict):
            msg = "Digital-twin configuration root must be an object"
            raise DigitalTwinConfigurationError(msg)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DigitalTwinConfig:
        """Validate and construct a configuration snapshot."""
        _validate_schema(value)
        try:
            labware = tuple(
                LabwarePlacement.from_mapping(item, index=index) for index, item in enumerate(value["labware"])
            )
            instruments = tuple(
                InstrumentProfile.from_mapping(item, index=index) for index, item in enumerate(value["instruments"])
            )
            result = cls(
                schema_version=str(value["schema_version"]),
                config_revision=str(value["config_revision"]),
                deck_definition=str(value["deck_definition"]),
                calibration_id=str(value["calibration_id"]),
                calibration_confirmed=bool(value["calibration_confirmed"]),
                deck_bounds=DeckBounds.from_mapping(value["deck_bounds"]),
                deck_offset=DeckPoint.from_mapping(value["deck_offset"], field="deck_offset"),
                labware=labware,
                instruments=instruments,
                trash=DeckPoint.from_mapping(value["trash"], field="trash"),
                state_path=str(value["state_path"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DigitalTwinConfigurationError):
                raise
            msg = f"Digital-twin configuration is missing or contains an invalid field: {error}"
            raise DigitalTwinConfigurationError(msg) from error
        if result.schema_version != DT_CONFIG_SCHEMA_VERSION:
            msg = (
                f"Unsupported digital-twin schema_version {result.schema_version!r}; "
                f"expected {DT_CONFIG_SCHEMA_VERSION!r}"
            )
            raise DigitalTwinConfigurationError(msg)
        if not result.config_revision or not result.deck_definition or not result.calibration_id:
            msg = "config_revision, deck_definition, and calibration_id must not be empty"
            raise DigitalTwinConfigurationError(msg)
        labware_ids = [item.labware_id for item in result.labware]
        mounts = [item.mount for item in result.instruments]
        aliases = [alias for item in result.instruments for alias in item.aliases]
        if len(labware_ids) != len(set(labware_ids)):
            msg = "labware_id values must be unique"
            raise DigitalTwinConfigurationError(msg)
        if len(mounts) != len(set(mounts)):
            msg = "Only one instrument profile may be configured per mount"
            raise DigitalTwinConfigurationError(msg)
        if len(aliases) != len(set(aliases)):
            msg = "Instrument aliases must be globally unique"
            raise DigitalTwinConfigurationError(msg)
        return result

    @property
    def config_id(self) -> str:
        """Return the SHA-256 identity of the canonical configuration."""
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def canonical_json(self) -> str:
        """Serialize the configuration deterministically for hashing and audit."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    def instrument(self, mount: str) -> InstrumentProfile:
        """Return the configured instrument profile for a mount."""
        normalized = mount.upper()
        for profile in self.instruments:
            if profile.mount == normalized:
                return profile
        msg = f"No instrument profile is configured for mount {normalized}"
        raise DigitalTwinConfigurationError(msg)

    def instrument_for_alias(self, alias: str) -> InstrumentProfile:
        """Resolve an orchestrator instrument alias to a configured mount."""
        for profile in self.instruments:
            if alias in profile.aliases:
                return profile
        msg = f"No instrument profile is configured for alias {alias!r}"
        raise DigitalTwinConfigurationError(msg)

    def placement(self, labware_id: str) -> LabwarePlacement:
        """Return a named labware placement."""
        for placement in self.labware:
            if placement.labware_id == labware_id:
                return placement
        msg = f"Unknown labware_id {labware_id!r}"
        raise DigitalTwinConfigurationError(msg)

    def resolve_well(self, labware_id: str, well: str, *, use_approach: bool) -> DeckPoint:
        """Resolve a symbolic labware well into the calibrated deck frame."""
        placement = self.placement(labware_id)
        return placement.grid.resolve(well, placement_origin=placement.origin, use_approach=use_approach)

    def to_machine_point(self, point: DeckPoint) -> DeckPoint:
        """Apply the configured deck-frame translation."""
        return DeckPoint(
            x=point.x + self.deck_offset.x,
            y=point.y + self.deck_offset.y,
            z=point.z + self.deck_offset.z,
        )
