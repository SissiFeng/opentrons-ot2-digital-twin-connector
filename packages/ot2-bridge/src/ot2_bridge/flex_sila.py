"""Optional bounded SiLA transport using live feature definitions, with no Opentrons imports."""

from __future__ import annotations

import asyncio
import base64
import time

FEATURES = {
    "MotionController": "ca.accelerationconsortium/robots/MotionController/v2",
    "TipController": "ca.accelerationconsortium/robots/TipController/v1",
    "PipetteController": "ca.accelerationconsortium/robots/PipetteController/v1",
}


def native(value):
    """Convert SiLA named tuples to plain data without inventing missing values."""
    if hasattr(value, "_asdict"):
        return {key: native(item) for key, item in value._asdict().items()}
    if isinstance(value, (list, tuple)):
        return [native(item) for item in value]
    if isinstance(value, dict):
        return {key: native(item) for key, item in value.items()}
    return value


def single(value):
    values = native(value)
    if not isinstance(values, dict) or len(values) != 1:
        raise ValueError("Expected exactly one SiLA response value")
    return next(iter(values.values()))


class FlexSiLATransport:
    """Discover only required features and await observable command completion.

    All RPCs have deadlines. Cancelling a client await does not claim a physical
    stop. EmergencyStop is a separate explicit operator action.
    """

    feature_identifiers = FEATURES

    @staticmethod
    def validate_host(host):
        if host not in ("127.0.0.1", "localhost"):
            raise ValueError("Forward SiLA over SSH to localhost")
        return host

    def __init__(self, host="127.0.0.1", port=50051, *, timeout=30, progress=None):
        host = self.validate_host(host)
        if timeout <= 0:
            raise ValueError("Positive RPC timeout required")
        self.host, self.port, self.timeout, self.progress = host, port, timeout, progress
        self.channel = None
        self.features = {}

    async def connect(self, feature_names=None):
        import grpc
        from sila2.features.silaservice import SiLAServiceFeature
        from sila2.framework.feature import Feature

        self.channel = grpc.aio.insecure_channel(
            f"{self.host}:{self.port}",
            options=[
                ("grpc.max_receive_message_length", 2 * 1024 * 1024),
            ],
        )
        try:
            try:
                await asyncio.wait_for(self.channel.channel_ready(), self.timeout)
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    f"SiLA connection to {self.host}:{self.port} timed out after {self.timeout:g}s. "
                    "Check reachability from the Bridge backend computer, the SSH forward, "
                    "Tailscale access and proxy settings. No robot command was sent."
                ) from error
            self._add("SiLAService", Feature(SiLAServiceFeature._feature_definition))
            implemented = await self.property("SiLAService", "ImplementedFeatures")
            for name in feature_names if feature_names is not None else self.feature_identifiers:
                identifier = self.feature_identifiers[name]
                if identifier not in implemented:
                    raise ValueError(f"Connector does not expose required feature {identifier}")
                xml = single(await self.command("SiLAService", "GetFeatureDefinition", FeatureIdentifier=identifier))
                if not isinstance(xml, str) or len(xml) > 1_000_000 or not xml.lstrip().startswith("<"):
                    raise ValueError("Invalid feature definition")
                model = Feature(self._client_feature_definition(name, xml))
                if str(model.fully_qualified_identifier) != identifier:
                    raise ValueError("Feature definition identity mismatch")
                self._add(name, model)
            return self
        except BaseException:
            await self.close()
            raise

    def _client_feature_definition(self, name, xml):
        """Allow an adapter to select the endpoints it consumes before codegen."""
        return xml

    def _add(self, name, model):
        stub = getattr(model._grpc_module, f"{model._identifier}Stub")(self.channel)
        self.features[name] = (model, stub)

    async def close(self):
        if self.channel is not None:
            await self.channel.close()
            self.channel = None

    async def property(self, feature, name):
        model, stub = self.features[feature]
        prop = model._unobservable_properties[name]
        response = await self._rpc(getattr(stub, f"Get_{name}"), prop.get_parameters_message())
        return native(prop.to_native_type(response))

    async def _rpc(self, rpc, request, timeout=None):
        import grpc
        from sila2.framework.pb2 import SiLAFramework_pb2

        try:
            return await rpc(request, timeout=timeout or self.timeout)
        except grpc.aio.AioRpcError as error:
            # Preserve structured SiLA error identifiers and vendor/recovery messages.
            detail = error.details() or ""
            try:
                decoded = SiLAFramework_pb2.SiLAError.FromString(base64.b64decode(detail, validate=True))
                detail = str(decoded)
            except Exception:
                pass
            raise RuntimeError(f"SiLA {error.code().name}: {detail}") from error

    async def command(self, feature, name, **params):
        model, stub = self.features[feature]
        observable = name in model._observable_commands
        command = (model._observable_commands if observable else model._unobservable_commands)[name]
        # UniteLabs CDK serializes enum names as lower-case wire values.
        if "Mount" in params:
            params["Mount"] = params["Mount"].lower()
        request = command.parameters.to_message(**params)
        start = time.monotonic()
        response = await self._rpc(getattr(stub, name), request)
        if not observable:
            return native(command.responses.to_native_type(response))
        framework = model._pb2_module.SiLAFramework__pb2
        execution = framework.CommandExecutionUUID(value=response.commandExecutionUUID.value)
        stream = getattr(stub, f"{name}_Info")(execution, timeout=self.timeout)
        finished = False
        try:
            async for info in stream:
                status = framework.ExecutionInfo.CommandStatus.Name(info.commandStatus)
                if self.progress:
                    self.progress(
                        {
                            "feature": feature,
                            "command": name,
                            "status": status,
                            "execution_id": execution.value,
                            "progress": info.progressInfo.value if info.HasField("progressInfo") else None,
                        }
                    )
                if status in ("finishedSuccessfully", "finishedWithError"):
                    finished = True
                    break
        finally:
            stream.cancel()
        if not finished:
            raise RuntimeError(f"{name} ended without terminal execution evidence")
        remaining = self.timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError(f"{name} exceeded its deadline; hardware stop is not confirmed")
        response = await self._rpc(getattr(stub, f"{name}_Result"), execution, remaining)
        return native(command.responses.to_native_type(response))

    async def describe(self):
        """Read identity, attached pipette and machine status without motion."""
        pipettes = single(await self.command("PipetteController", "GetAttachedPipettes"))
        left = next((p for p in pipettes if p["Mount"] == "left"), None)
        if left is None or left["Attached"] is not True:
            raise ValueError("A left pipette must be installed")
        status = await self.property("MotionController", "MachineStatus")
        presence = single(await self.command("TipController", "GetTipPresence", Mount="LEFT"))
        if presence not in ("present", "absent"):
            raise ValueError("Unknown tip presence")
        return {
            "server_uuid": await self.property("SiLAService", "ServerUUID"),
            "is_simulating": await self.property("MotionController", "IsSimulating"),
            "pipette": {"id": left["PipetteId"], "model": left["Model"], "channels": left["Channels"]},
            "tip_present": presence == "present",
            "machine_error": status["IsErrorState"],
            "estop": status["Estop"],
            "door_open": status["DoorOpen"],
            "message": status["Message"],
        }

    async def snapshot(self):
        result = await self.describe()
        position = single(await self.command("MotionController", "GetPosition", Mount="LEFT"))
        result["position"] = {axis.lower(): value for axis, value in position.items()}
        return result
