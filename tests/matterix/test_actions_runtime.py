"""OT-2 → Matterix semantic-action translation against the pinned deck config."""

from __future__ import annotations

from pathlib import Path

import pytest

from unitelabs.opentrons_ot2.digital_twin.config import DigitalTwinConfig, DigitalTwinConfigurationError
from unitelabs.opentrons_ot2.matterix.actions import (
    AspirateCfg,
    DispenseCfg,
    DropTipCfg,
    HomeCfg,
    MoveToCfg,
    MoveToWellCfg,
    PickUpTipCfg,
    ReconcileTipCfg,
    ReturnTipCfg,
)
from unitelabs.opentrons_ot2.matterix.actions_runtime import (
    MatterixMoveToJointConfig,
    MatterixSemanticAction,
    OT2MatterixTranslationError,
    TranslatedAction,
    translate_action,
    translate_sequence,
)

from tests.digital_twin.helpers import config_mapping
from tests.matterix.helpers import verified_alignment, verified_tip_binding


@pytest.fixture
def connector(tmp_path: Path) -> DigitalTwinConfig:
    mapping = config_mapping(tmp_path / "state.json")
    mapping["instruments"][0]["channels"] = 1
    mapping["instruments"][0]["expected_model"] = "p300_single_v2.1"
    return DigitalTwinConfig.from_mapping(mapping)


def _first_config(translated: TranslatedAction) -> object:
    assert translated.configs
    return translated.configs[0]


def _semantics(translated: TranslatedAction) -> tuple[object, ...]:
    configs = [item for item in translated.configs if isinstance(item, MatterixSemanticAction)]
    assert configs
    return configs[0].semantics


def _moves(translated: TranslatedAction) -> tuple[MatterixMoveToJointConfig, ...]:
    return tuple(item for item in translated.configs if isinstance(item, MatterixMoveToJointConfig))


def test_home_targets_real_reference_positions(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(HomeCfg(), connector=connector, alignment=alignment)
    assert translated.source == "home"
    move = _first_config(translated)
    assert isinstance(move, MatterixMoveToJointConfig)
    assert move.joint_indices == (0, 1, 2, 3)
    assert move.target_joint_positions == pytest.approx((0.2, 0.18, 0.0, 0.0))


def test_move_to_converts_millimetres_to_metres(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(
        MoveToCfg(mount="RIGHT", x=100.0, y=200.0, z=50.0, speed=80.0, reference="NOZZLE"),
        connector=connector,
        alignment=alignment,
    )
    config = _first_config(translated)
    assert isinstance(config, MatterixMoveToJointConfig)
    assert config.agent_assets == "ot2"
    assert config.joint_indices == (0, 1, 3)
    profile = connector.instrument("RIGHT")
    assert config.target_joint_positions == pytest.approx(
        (
            alignment.real_to_sim("X", 100.0),
            alignment.real_to_sim("Y", 200.0),
            alignment.real_to_sim("A", profile.nozzle_deck_axis_position + 50.0),
        )
    )


def test_move_to_well_resolves_working_height(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(
        MoveToWellCfg(
            mount="RIGHT",
            labware_id="source_plate",
            well="B2",
            height="WORKING",
            speed=80.0,
            reference="TIP",
        ),
        connector=connector,
        alignment=alignment,
    )
    config = _first_config(translated)
    assert isinstance(config, MatterixMoveToJointConfig)
    expected = connector.resolve_well("source_plate", "B2", use_approach=False)
    profile = connector.instrument("RIGHT")
    assert config.joint_indices == (0, 1, 3)
    assert config.target_joint_positions == pytest.approx(
        (
            alignment.real_to_sim("X", expected.x),
            alignment.real_to_sim("Y", expected.y),
            alignment.real_to_sim("A", profile.nozzle_deck_axis_position + expected.z + profile.tip_length),
        )
    )


def test_move_to_well_resolves_approach_height(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(
        MoveToWellCfg(
            mount="RIGHT",
            labware_id="source_plate",
            well="A1",
            height="APPROACH",
            speed=80.0,
            reference="TIP",
        ),
        connector=connector,
        alignment=alignment,
    )
    config = _first_config(translated)
    assert isinstance(config, MatterixMoveToJointConfig)
    expected = connector.resolve_well("source_plate", "A1", use_approach=True)
    profile = connector.instrument("RIGHT")
    assert config.joint_indices == (0, 1, 3)
    assert config.target_joint_positions == pytest.approx(
        (
            alignment.real_to_sim("X", expected.x),
            alignment.real_to_sim("Y", expected.y),
            alignment.real_to_sim("A", profile.nozzle_deck_axis_position + expected.z + profile.tip_length),
        )
    )


def test_pick_up_tip_moves_to_rack_well_and_selects_nested_child(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(
        PickUpTipCfg(mount="RIGHT", labware_id="tips_300", well="A1"),
        connector=connector,
        alignment=alignment,
        tip_rack_bindings=(verified_tip_binding(),),
    )
    moves = _moves(translated)
    assert len(moves) == 4
    expected = connector.resolve_well("tips_300", "A1", use_approach=False)
    profile = connector.instrument("RIGHT")
    assert moves[0].joint_indices == (3,)
    assert moves[0].target_joint_positions == pytest.approx(
        (alignment.real_to_sim("A", profile.nozzle_deck_axis_position + profile.safe_travel_height),)
    )
    assert moves[1].joint_indices == (0, 1)
    assert moves[1].target_joint_positions == pytest.approx(
        (
            alignment.real_to_sim("X", expected.x),
            alignment.real_to_sim("Y", expected.y),
        )
    )
    assert moves[2].joint_indices == (3,)
    assert moves[2].target_joint_positions == pytest.approx(
        (alignment.real_to_sim("A", profile.nozzle_deck_axis_position + expected.z),)
    )
    assert moves[-1] == moves[0]
    (tip,) = _semantics(translated)
    assert (tip.type, tip.asset_name, tip.value) == ("IsTipAttached", "ot2", True)
    assert tip.additional_info == {"tip_asset_name": "tips", "tip_child_id": "pipette_tip_mesh_00"}


def test_drop_tip_moves_to_configured_trash_and_clears_tip(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(DropTipCfg(mount="RIGHT"), connector=connector, alignment=alignment)
    moves = _moves(translated)
    assert len(moves) == 4
    profile = connector.instrument("RIGHT")
    assert moves[1].joint_indices == (0, 1)
    assert moves[1].target_joint_positions == pytest.approx(
        (
            alignment.real_to_sim("X", connector.trash.x),
            alignment.real_to_sim("Y", connector.trash.y),
        )
    )
    assert moves[2].joint_indices == (3,)
    assert moves[2].target_joint_positions == pytest.approx(
        (
            alignment.real_to_sim(
                "A",
                profile.nozzle_deck_axis_position + connector.trash.z + profile.tip_length,
            ),
        )
    )
    (tip,) = _semantics(translated)
    assert (tip.type, tip.asset_name, tip.value) == ("IsTipAttached", "ot2", False)
    assert tip.additional_info == {}


def test_return_tip_targets_same_nested_child_then_releases(connector: DigitalTwinConfig) -> None:
    alignment = verified_alignment()
    translated = translate_action(
        ReturnTipCfg(mount="RIGHT", labware_id="tips_300", well="B2"),
        connector=connector,
        alignment=alignment,
        tip_rack_bindings=(verified_tip_binding(),),
    )
    moves = _moves(translated)
    assert len(moves) == 4
    point = connector.resolve_well("tips_300", "B2", use_approach=False)
    profile = connector.instrument("RIGHT")
    assert moves[1].target_joint_positions == pytest.approx(
        (alignment.real_to_sim("X", point.x), alignment.real_to_sim("Y", point.y))
    )
    assert moves[2].target_joint_positions == pytest.approx(
        (alignment.real_to_sim("A", profile.nozzle_deck_axis_position + point.z + profile.tip_length),)
    )
    (tip,) = _semantics(translated)
    assert (tip.type, tip.asset_name, tip.value) == ("IsTipAttached", "ot2", False)
    assert tip.additional_info == {"tip_asset_name": "tips", "tip_child_id": "pipette_tip_mesh_09"}


def test_reconcile_absent_releases_selected_nested_tip(connector: DigitalTwinConfig) -> None:
    translated = translate_action(
        ReconcileTipCfg(mount="LEFT", present=False),
        connector=connector,
        alignment=verified_alignment(),
    )
    (tip,) = _semantics(translated)
    assert (tip.type, tip.asset_name, tip.value) == ("IsTipAttached", "ot2", False)


def test_reconcile_present_fails_without_physical_child_selection(connector: DigitalTwinConfig) -> None:
    with pytest.raises(OT2MatterixTranslationError, match="cannot select a physical nested tip"):
        translate_action(
            ReconcileTipCfg(mount="LEFT", present=True),
            connector=connector,
            alignment=verified_alignment(),
        )


def test_pick_up_tip_requires_accepted_nested_binding(connector: DigitalTwinConfig) -> None:
    with pytest.raises(OT2MatterixTranslationError, match="exactly one tip-rack binding"):
        translate_action(
            PickUpTipCfg(mount="RIGHT", labware_id="tips_300", well="A1"),
            connector=connector,
            alignment=verified_alignment(),
        )


def test_aspirate_adds_liquid_volume(connector: DigitalTwinConfig) -> None:
    translated = translate_action(
        AspirateCfg(mount="RIGHT", volume=100.0, flow_rate=50.0),
        connector=connector,
        alignment=verified_alignment(),
    )
    (semantic,) = _semantics(translated)
    assert semantic.type == "LiquidVolume"
    assert semantic.asset_name == "pipette_right"
    assert semantic.value == 100.0
    assert semantic.additional_info == {"operation": "add"}
    assert len(translated.configs) == 1


def test_dispense_subtracts_liquid_volume(connector: DigitalTwinConfig) -> None:
    translated = translate_action(
        DispenseCfg(mount="RIGHT", volume=40.0, flow_rate=50.0),
        connector=connector,
        alignment=verified_alignment(),
    )
    (semantic,) = _semantics(translated)
    assert semantic.type == "LiquidVolume"
    assert semantic.asset_name == "pipette_right"
    assert semantic.value == 40.0
    assert semantic.additional_info == {"operation": "subtract"}
    assert len(translated.configs) == 1


def test_unknown_well_raises_configuration_error(connector: DigitalTwinConfig) -> None:
    with pytest.raises(DigitalTwinConfigurationError, match="Unknown well"):
        translate_action(
            MoveToWellCfg(
                mount="RIGHT",
                labware_id="source_plate",
                well="Z99",
                height="WORKING",
                speed=80.0,
                reference="TIP",
            ),
            connector=connector,
            alignment=verified_alignment(),
        )


def test_unknown_labware_raises_configuration_error(connector: DigitalTwinConfig) -> None:
    with pytest.raises(DigitalTwinConfigurationError, match="Unknown labware_id"):
        translate_action(
            PickUpTipCfg(mount="RIGHT", labware_id="missing", well="A1"),
            connector=connector,
            alignment=verified_alignment(),
        )


def test_translate_sequence_preserves_source_order(connector: DigitalTwinConfig) -> None:
    actions = (
        HomeCfg(),
        MoveToCfg(mount="RIGHT", x=10.0, y=20.0, z=30.0, speed=80.0, reference="NOZZLE"),
        AspirateCfg(mount="RIGHT", volume=50.0, flow_rate=50.0),
        DispenseCfg(mount="RIGHT", volume=50.0, flow_rate=50.0),
    )
    sequence = translate_sequence(actions, connector=connector, alignment=verified_alignment())
    assert [item.source for item in sequence] == ["home", "move_to", "aspirate", "dispense"]


def test_unsupported_action_raises_translation_error(connector: DigitalTwinConfig) -> None:
    class ForeignAction:
        pass

    with pytest.raises(OT2MatterixTranslationError, match="Unsupported OT-2 action"):
        translate_action(
            ForeignAction(),
            connector=connector,
            alignment=verified_alignment(),
        )  # type: ignore[arg-type]
