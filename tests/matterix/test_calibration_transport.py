"""Wire-level coverage for the production calibration gRPC adapter."""

from __future__ import annotations

import contextlib

import grpc.aio
import pytest
from unitelabs.cdk import SiLAServerConfig

from unitelabs.opentrons_ot2 import OpentronsOt2Config, create_app
from unitelabs.opentrons_ot2.matterix.calibration_pipeline import GrpcCalibrationRobotClient


@pytest.mark.asyncio
async def test_grpc_adapter_reads_mode_identity_bounds_and_motion() -> None:
    """The pipeline adapter uses the actual serialized MotionControl surface."""
    config = OpentronsOt2Config(
        use_simulator=True,
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    generator = create_app(config)
    connector = await generator.__anext__()
    await connector.start()
    channel = grpc.aio.insecure_channel(connector.sila_server._address)
    try:
        client = GrpcCalibrationRobotClient(channel, connector.sila_server.protobuf)
        assert await client.is_simulating() is True
        assert await client.serial_number() == ""
        assert await client.firmware_version() == "Virtual Smoothie"
        identity = await client.device_information()
        assert identity["simulation"] is True
        assert identity["contract_id"]
        assert (await client.axis_bounds())["X"] == (0.0, 418.0)
        home = await client.home("XYZA")
        moved = await client.move_relative_axis("X", -5.0, 20.0)
        assert moved["X"] == pytest.approx(home["X"] - 5.0)
        returned = await client.move_axis("X", home["X"], 20.0)
        assert returned["X"] == pytest.approx(home["X"])
    finally:
        await channel.close()
        await connector.stop()
        with contextlib.suppress(StopAsyncIteration):
            await generator.__anext__()
