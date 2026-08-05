from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.bridge import OT2WorkflowAdapter, parse_workflow
from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig
from unitelabs.opentrons_ot2.matterix import (
    OT2MatterixPlanner,
    compare_with_connector_state,
    predict_plan,
)

from tests.digital_twin.helpers import config_mapping


def _initial_state() -> dict:
    return {
        "revision": 0,
        "mounts": [
            {
                "mount": "RIGHT",
                "position": {"x": 0.0, "y": 0.0, "z": 100.0},
                "homed": False,
                "tip_presence": "UNKNOWN",
                "tip_evidence": "UNRECONCILED",
                "liquid_volume": 0.0,
                "liquid_volume_known": False,
            }
        ],
    }


def test_shadow_prediction_matches_expected_atomic_end_state(tmp_path: Path) -> None:
    connector = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json"))
    adapter = OT2WorkflowAdapter.build(connector)
    workflow = parse_workflow(Path(__file__).parents[2] / "examples" / "workflows" / "ot2_tip_transfer.json")
    plan = OT2MatterixPlanner(adapter).plan(workflow)
    prediction = predict_plan(plan, _initial_state(), connector)
    mount = prediction.mounts[0]
    assert mount.position == pytest.approx(
        (connector.trash.x, connector.trash.y, connector.instruments[0].safe_travel_height)
    )
    assert mount.homed is True
    assert mount.tip_presence == "ABSENT"
    assert mount.tip_evidence == "SOFTWARE_TRACKED"
    assert mount.liquid_volume == pytest.approx(0.0)
    assert mount.liquid_volume_known is True

    matching = {
        "mounts": [
            {
                "mount": "RIGHT",
                "position": {
                    "x": mount.position[0],
                    "y": mount.position[1],
                    "z": mount.position[2],
                },
                "homed": True,
                "tip_presence": "ABSENT",
                "tip_evidence": "SOFTWARE_TRACKED",
                "liquid_volume": 0.0,
                "liquid_volume_known": True,
            }
        ]
    }
    assert compare_with_connector_state(prediction, matching) == ()

    divergent = deepcopy(matching)
    divergent["mounts"][0]["position"]["x"] += 2.0
    divergent["mounts"][0]["tip_presence"] = "PRESENT"
    differences = compare_with_connector_state(prediction, divergent)
    assert {item.field for item in differences} == {"position", "tip_presence"}
