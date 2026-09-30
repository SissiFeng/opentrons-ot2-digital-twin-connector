"""Standard SiLA client for the actual OT-2 motion feature (not the Flex feature)."""

from .flex_sila import FlexSiLATransport, single
from .network import sila_tunnel_host


class OT2SiLATransport(FlexSiLATransport):
    feature_identifiers = {"MotionControlFeature": "ca.accelerationconsortium/robots/MotionControlFeature/v1"}
    validate_host = staticmethod(sila_tunnel_host)

    async def describe(self):
        return {
            "server_uuid": await self.property("SiLAService", "ServerUUID"),
            "is_simulating": await self.property("MotionControlFeature", "IsSimulating"),
            "position": {
                key.lower(): value
                for key, value in single(await self.command("MotionControlFeature", "GetPosition")).items()
            },
            "homed": {
                key.lower(): value for key, value in (await self.property("MotionControlFeature", "HomedFlags")).items()
            },
        }

    async def snapshot(self):
        return await self.describe()
