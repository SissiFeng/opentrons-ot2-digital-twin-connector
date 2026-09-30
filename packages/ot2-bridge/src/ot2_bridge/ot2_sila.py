"""Standard SiLA client for the actual OT-2 motion feature (not the Flex feature)."""

from .flex_sila import FlexSiLATransport, single
from .network import sila_tunnel_host


class OT2SiLATransport(FlexSiLATransport):
    feature_identifiers = {"MotionControlFeature": "ca.accelerationconsortium/robots/MotionControlFeature/v1"}
    validate_host = staticmethod(sila_tunnel_host)

    def _client_feature_definition(self, name, xml):
        """Compile only the home/readback contract from the live definition.

        The deployed motion feature also exposes MoveThrough, whose constrained
        list of inline structures cannot be compiled by sila2 0.14. Selecting
        our consumed endpoints avoids coupling this adapter to unrelated RPCs.
        Endpoint definitions, identity, types and execution errors stay intact;
        missing endpoints fail the connection instead of synthesizing a schema.
        """
        if name != "MotionControlFeature":
            return xml
        # lxml is already provided by the optional SiLA dependency. Preserve the
        # default namespace: sila2's constraint parser uses XML name(), which
        # treats an introduced prefix (e.g. ns0:Pattern) as a different name.
        from lxml import etree as ElementTree

        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise ValueError("OT-2 feature definition must not contain XML entities or a DTD")
        parser = ElementTree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
        root = ElementTree.fromstring(xml.encode("utf-8"), parser)
        ns = "{http://www.sila-standard.org}"
        if root.tag != ns + "Feature":
            raise ValueError("Invalid OT-2 feature definition namespace or root")
        required = {
            "Command": {"Home", "EmergencyStop", "GetPosition"},
            "Property": {"HomedFlags", "IsSimulating"},
        }
        for kind, names in required.items():
            nodes = root.findall(ns + kind)
            for endpoint in sorted(names):
                if sum(node.findtext(ns + "Identifier") == endpoint for node in nodes) != 1:
                    raise ValueError(f"OT-2 connector must expose exactly one {endpoint} {kind.lower()}")
            for node in nodes:
                if node.findtext(ns + "Identifier") not in names:
                    root.remove(node)
        return ElementTree.tostring(root, encoding="unicode")

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
