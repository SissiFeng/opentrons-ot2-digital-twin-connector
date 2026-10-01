"""Optional real SiLA-wire integration against the pinned Flex OT3 simulator.

Set FLEX_CONNECTOR_SOURCE to the pinned connector checkout; no physical target is used.
"""

import asyncio
import os
import sys

import pytest

if not os.environ.get("FLEX_CONNECTOR_SOURCE"):
    pytest.skip("FLEX_CONNECTOR_SOURCE required for connector simulator integration", allow_module_level=True)
sys.path.insert(0, os.path.join(os.environ["FLEX_CONNECTOR_SOURCE"], "src"))

from ot2_bridge.flex_sila import FlexSiLATransport


@pytest.mark.asyncio
@pytest.mark.parametrize("run_mode", ["real-only", "shadow"])
async def test_actual_flex_wire_round_trip(run_mode, tmp_path, monkeypatch):
    from opentrons.hardware_control.ot3api import OT3API
    from opentrons.hardware_control.types import OT3Mount
    from unitelabs.cdk import Connector, SiLAServerConfig
    from unitelabs.opentrons_flex import OpentronsFlexConfig
    from unitelabs.opentrons_flex.features.motion_control import MotionControlFeature
    from unitelabs.opentrons_flex.features.tip_controller import TipController
    from unitelabs.opentrons_flex.features.pipette import PipetteFeature
    from unitelabs.opentrons_flex.io import FlexMotionController

    api = await OT3API.build_hardware_simulator(
        attached_instruments={
            OT3Mount.LEFT: {"model": "p1000_single_v3.0", "id": "sim-left"},
        }
    )
    config = OpentronsFlexConfig(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    connector = Connector(config)
    controller = FlexMotionController.from_api(api, lock=asyncio.Lock())
    connector.register(MotionControlFeature(controller))
    connector.register(TipController(controller))
    connector.register(PipetteFeature(controller))
    await connector.start()
    port = int(connector.sila_server._address.rsplit(":", 1)[1])
    transport = FlexSiLATransport(port=port)
    try:
        await transport.connect()
        await transport.command("MotionController", "Home")
        initial = await transport.snapshot()
        assert initial["is_simulating"] is True
        assert initial["tip_present"] is False
        assert initial["pipette"]["id"] == "sim-left"
        assert initial["pipette"]["channels"] == 1
        with pytest.raises(RuntimeError, match="TipNotAttachedError"):
            await transport.command(
                "TipController",
                "DropTip",
                Mount="LEFT",
                Location={k.upper(): v for k, v in initial["position"].items()},
                HomeAfter=False,
            )
        location = {k.upper(): v for k, v in initial["position"].items()}
        await transport.command(
            "TipController", "PickUpTip", Mount="LEFT", Location=location, TipLength=95.6, PrepAfter=False
        )
        assert (await transport.snapshot())["tip_present"] is True
        location["Z"] -= 95.6
        await transport.command("TipController", "DropTip", Mount="LEFT", Location=location, HomeAfter=False)
        assert (await transport.snapshot())["tip_present"] is False

        from ot2_bridge.flex import profile_template, make_plan
        from ot2_bridge.flex_cli import run_connected
        from ot2_bridge.remote import SimulationService
        from ot2_bridge.models import Observation, Fact, Evidence, Outcome, Status

        monkeypatch.setenv("MATTERIX_BRIDGE_STATE", str(tmp_path / "attempts"))
        await transport.command("MotionController", "Home")
        initial = await transport.snapshot()
        profile = profile_template()
        point = initial["position"]
        high = {"x": point["x"] - 20, "y": point["y"] - 20, "z": point["z"] - 100}
        low = {**high, "z": high["z"] - 20}
        drop = {**low, "x": low["x"] - 20}
        drop_high = {**drop, "z": high["z"]}
        profile["real"].update(
            port=port,
            server_uuid=initial["server_uuid"],
            pipette_id=initial["pipette"]["id"],
            pipette_model=initial["pipette"]["model"],
            calibration_revision="OT3-simulator-fixture-only",
            tiprack="simulated-A1",
            tip_length_mm=58.35,
            pickup=low,
            pickup_clearance=high,
            drop=drop,
            drop_clearance=drop_high,
            park=high,
        )
        plan = make_plan(profile, "wire-" + run_mode)

        class SyntheticMatterix:
            def prepare(self, op):
                async def execute():
                    return Outcome(
                        Status.SUCCEEDED,
                        Observation(
                            "synthetic-matterix",
                            {"pipette.tip_attached": Fact(op.action == "pick_up_tip", Evidence.SIMULATED)},
                        ),
                    )

                return execute

        service = SimulationService(
            SyntheticMatterix(),
            binding=plan["binding"],
            initial_observation=Observation(
                "synthetic-matterix", {"pipette.tip_attached": Fact(False, Evidence.SIMULATED)}
            ),
        )
        server = await service.listen(0)
        try:
            report = await run_connected(
                plan, mode=run_mode, hardware=False, sim_port=server.sockets[0].getsockname()[1]
            )
            assert report["status"] == "completed", report
            assert len(report["steps"]) == 3
            assert (
                report["steps"][-1]["real"]["outcome"]["observation"]["facts"]["pipette.tip_attached"]["value"] is False
            )
        finally:
            server.close()
            await server.wait_closed()
    finally:
        await transport.close()
        await connector.stop()
        await api.clean_up()
