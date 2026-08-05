"""Configuration, contract hashing, and FDL compatibility gates."""

import json
from importlib.resources import files
from pathlib import Path

import pytest
from sila.framework.fdl import Serializer
from unitelabs.cdk import Connector, SiLAServerConfig

from unitelabs.opentrons_ot2 import OpentronsOt2Config, _register_digital_twin_features
from unitelabs.opentrons_ot2.digital_twin.config import (
    DT_CONFIG_SCHEMA_VERSION,
    DigitalTwinConfig,
    DigitalTwinConfigurationError,
)
from unitelabs.opentrons_ot2.digital_twin.contract import (
    ContractMismatchError,
    ContractSnapshot,
)
from unitelabs.opentrons_ot2.io import OT2MotionController


def test_config_is_schema_valid_and_hash_stable():
    config = DigitalTwinConfig.from_file("config/ot2_dt_config.json")
    assert config.schema_version == DT_CONFIG_SCHEMA_VERSION
    assert config.config_id == DigitalTwinConfig.from_mapping(json.loads(config.canonical_json())).config_id
    assert config.resolve_well("tips_300", "A1", use_approach=False).z == -7.0


def test_config_rejects_unknown_fields():
    value = json.loads(Path("config/ot2_dt_config.json").read_text(encoding="utf-8"))
    value["unreviewed_override"] = True
    with pytest.raises(DigitalTwinConfigurationError, match="Additional properties"):
        DigitalTwinConfig.from_mapping(value)


def test_contract_artifact_rejects_tampering():
    resource = files("unitelabs.opentrons_ot2").joinpath("contracts").joinpath("ot2_dt_contract.json")
    value = json.loads(resource.read_text(encoding="utf-8"))
    value["features"][0]["version"] = "99.0"
    with pytest.raises(ContractMismatchError, match="does not match"):
        ContractSnapshot.from_mapping(value)


@pytest.mark.asyncio
async def test_serialized_fdl_matches_packaged_contract():
    motion = await OT2MotionController.build(simulate=True)
    config = OpentronsOt2Config(
        use_simulator=True,
        digital_twin_config_path="config/ot2_dt_config.json",
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    connector = Connector(config)
    started = False
    try:
        _, actual = await _register_digital_twin_features(connector, motion, config, ())
        resource = files("unitelabs.opentrons_ot2").joinpath("contracts").joinpath("ot2_dt_contract.json")
        expected = ContractSnapshot.from_mapping(json.loads(resource.read_text(encoding="utf-8")))
        assert actual == expected
        assert actual.contract_id == "2092afe298601f3a946b0746cc17f52bd9ea504d19e9d34a4c6f3eb4cf24899c"
        await connector.start()
        started = True
        assert connector.sila_server.protobuf is not None
    finally:
        if started:
            await connector.stop()
        await motion.disconnect()


@pytest.mark.asyncio
async def test_new_features_use_explicit_identifiers_and_no_optional_parameters():
    motion = await OT2MotionController.build(simulate=True)
    config = OpentronsOt2Config(
        use_simulator=True,
        digital_twin_config_path="config/ot2_dt_config.json",
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    connector = Connector(config)
    try:
        controller, _ = await _register_digital_twin_features(connector, motion, config, ())
        assert controller.config.calibration_confirmed is False
        serialized = "\n".join(
            Serializer.serialize(feature.serialize) for feature in connector.sila_server.features.values()
        )
        for identifier in (
            "DeviceInformationProvider",
            "DeckConfigurationProvider",
            "RobotStateProvider",
            "MotionController",
            "PipetteController",
            "TipController",
            "LiquidHandlingController",
        ):
            assert f"<Identifier>{identifier}</Identifier>" in serialized
        assert "<Optional>" not in serialized
    finally:
        await motion.disconnect()
