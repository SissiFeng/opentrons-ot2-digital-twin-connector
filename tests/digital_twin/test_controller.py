"""High-level OT-2 unit-operation behavior and evidence semantics."""

import asyncio

import pytest

from unitelabs.opentrons_ot2.digital_twin.config import DeckPoint, DigitalTwinConfig
from unitelabs.opentrons_ot2.digital_twin.state import EvidenceSource, TipState
from unitelabs.opentrons_ot2.io import (
    CalibrationNotConfirmedError,
    LiquidVolumeOutOfRangeError,
    OT2DigitalTwinController,
    StateReconciliationRequiredError,
    TipStateError,
)

from .helpers import FakeMotionController, config_mapping


async def _controller(tmp_path, *, confirmed: bool = True):
    motion = FakeMotionController()
    config = DigitalTwinConfig.from_mapping(config_mapping(tmp_path / "state.json", calibration_confirmed=confirmed))
    return motion, await OT2DigitalTwinController.build(motion, config)


@pytest.mark.asyncio
async def test_move_uses_mount_deck_coordinates_and_connector_calibration(tmp_path):
    motion, controller = await _controller(tmp_path)
    result = await controller.move_to("RIGHT", DeckPoint(100.0, 80.0, 20.0), 50.0, tip_reference=False)
    assert motion.moves[-1] == {
        "target": {"X": 100.0, "Y": 80.0, "A": 70.0},
        "speed": 50.0,
    }
    assert result == DeckPoint(100.0, 80.0, 20.0)


@pytest.mark.asyncio
async def test_unconfirmed_calibration_fails_before_motion(tmp_path):
    motion, controller = await _controller(tmp_path, confirmed=False)
    with pytest.raises(CalibrationNotConfirmedError):
        await controller.move_to("RIGHT", DeckPoint(100.0, 80.0, 20.0), 50.0, tip_reference=False)
    assert motion.moves == []


@pytest.mark.asyncio
async def test_tip_lifecycle_is_atomic_and_evidence_is_not_overclaimed(tmp_path):
    motion, controller = await _controller(tmp_path)
    with pytest.raises(StateReconciliationRequiredError):
        await controller.pick_up_tip("RIGHT", "tips_300", "A1")
    await controller.reconcile_tip("RIGHT", present=False)
    picked_up = await controller.pick_up_tip("RIGHT", "tips_300", "A1")
    assert picked_up.mount("RIGHT").tip_state is TipState.PRESENT
    assert picked_up.mount("RIGHT").tip_evidence is EvidenceSource.SOFTWARE_TRACKED
    assert len(motion.tip_pickups) == 1

    dropped = await controller.drop_tip("RIGHT")
    assert dropped.mount("RIGHT").tip_state is TipState.ABSENT
    assert dropped.mount("RIGHT").tip_evidence is EvidenceSource.SOFTWARE_TRACKED
    assert len(motion.tip_drops) == 1


@pytest.mark.asyncio
async def test_liquid_calibration_stays_inside_connector(tmp_path):
    motion, controller = await _controller(tmp_path)
    await controller.reconcile_tip("RIGHT", present=False)
    await controller.pick_up_tip("RIGHT", "tips_300", "A1")
    aspirated = await controller.aspirate("RIGHT", 100.0, 50.0)
    assert aspirated.mount("RIGHT").liquid_volume == 100.0
    assert motion.aspirations == [("C", 100.0, 18.5, 50.0)]

    dispensed = await controller.dispense("RIGHT", 40.0, 60.0)
    assert dispensed.mount("RIGHT").liquid_volume == 60.0
    assert motion.dispenses == [("C", 40.0, 18.5, 60.0)]

    with pytest.raises(LiquidVolumeOutOfRangeError):
        await controller.dispense("RIGHT", 100.0, 60.0)


@pytest.mark.asyncio
async def test_tip_reference_requires_present_tip(tmp_path):
    _, controller = await _controller(tmp_path)
    await controller.reconcile_tip("RIGHT", present=False)
    with pytest.raises(TipStateError):
        await controller.move_to("RIGHT", DeckPoint(10.0, 10.0, 10.0), 10.0, tip_reference=True)


@pytest.mark.asyncio
async def test_cancelled_tip_pickup_forces_unknown_state(tmp_path):
    motion, controller = await _controller(tmp_path)
    await controller.reconcile_tip("RIGHT", present=False)

    async def cancel(**kwargs):
        raise asyncio.CancelledError

    motion.pick_up_tip_at = cancel
    with pytest.raises(asyncio.CancelledError):
        await controller.pick_up_tip("RIGHT", "tips_300", "A1")
    state = controller.state.snapshot.mount("RIGHT")
    assert state.tip_state is TipState.UNKNOWN
    assert state.tip_evidence is EvidenceSource.UNRECONCILED
    assert state.liquid_volume_known is False
