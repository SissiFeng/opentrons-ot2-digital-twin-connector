"""Test fixtures for the digital-twin controller."""

from __future__ import annotations

import copy
import json
from pathlib import Path


def config_mapping(state_path: Path, *, calibration_confirmed: bool = True) -> dict:
    """Return an isolated copy of the checked example configuration."""
    path = Path(__file__).parents[2] / "config" / "ot2_dt_config.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state_path"] = str(state_path)
    value["calibration_confirmed"] = calibration_confirmed
    value["calibration_id"] = "TEST-CALIBRATION"
    value["config_revision"] = "test"
    value["instruments"][0]["expected_model"] = "p300_multi_v2.1"
    return copy.deepcopy(value)


class FakeMotionController:
    """Deterministic simulated motion boundary for high-level controller tests."""

    def __init__(self) -> None:
        self.position = {"X": 0.0, "Y": 0.0, "Z": 150.0, "A": 150.0, "B": 0.0, "C": 0.0}
        self.homed_flags = dict.fromkeys("XYZABC", True)
        self.axis_bounds = {"X": 500.0, "Y": 500.0, "Z": 250.0, "A": 250.0, "B": 20.0, "C": 20.0}
        self.is_simulating = True
        self.has_hardware_api = False
        self.tip_pickups: list[dict] = []
        self.tip_drops: list[dict] = []
        self.aspirations: list[tuple] = []
        self.dispenses: list[tuple] = []
        self.moves: list[dict] = []
        self.stopped = False

    async def get_serial_number(self) -> str:
        return "SIM-OT2-001"

    async def get_firmware_version(self) -> str:
        return "SIM-FW"

    async def read_pipette_model(self, mount: str) -> str:
        return ""

    async def read_pipette_id(self, mount: str) -> str:
        return ""

    async def home(self, axes: str) -> dict[str, float]:
        for axis in axes:
            self.homed_flags[axis] = True
        return dict(self.position)

    async def move(self, target: dict[str, float], speed: float) -> None:
        self.position.update(target)
        self.moves.append({"target": dict(target), "speed": speed})

    async def pick_up_tip_at(self, **kwargs) -> None:
        self.tip_pickups.append(kwargs)

    async def drop_tip_at(self, **kwargs) -> None:
        self.tip_drops.append(kwargs)

    async def aspirate(self, axis: str, volume: float, conversion: float, flow_rate: float) -> None:
        self.aspirations.append((axis, volume, conversion, flow_rate))

    async def dispense(self, axis: str, volume: float, conversion: float, flow_rate: float) -> None:
        self.dispenses.append((axis, volume, conversion, flow_rate))

    async def stop(self) -> None:
        self.stopped = True
        self.homed_flags = dict.fromkeys("XYZABC", False)

    def read_door_switch(self) -> bool:
        return True
