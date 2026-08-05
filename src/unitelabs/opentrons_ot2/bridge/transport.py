"""Async SiLA 2 transport driven entirely by the packaged OT-2 contract."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import enum
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from unitelabs.cdk import Connector

from ..digital_twin.contract import ContractMismatchError
from .models import EndpointKind, ExecutionMode, OT2Command
from .registry import ContractRegistry, EndpointBinding


class OT2SiLAError(RuntimeError):
    """Base bridge transport error."""


class OT2SiLAConnectionError(OT2SiLAError):
    """The gRPC connection or local connector codec could not be prepared."""


class OT2SiLACommandError(OT2SiLAError):
    """The connector rejected or failed one invocation."""

    def __init__(self, command: OT2Command, details: str) -> None:
        self.command = command
        self.details = details
        super().__init__(f"OT-2 SiLA {command.endpoint} failed for step {command.step_id!r}: {details}")


class OT2SiLACommandTimeout(OT2SiLACommandError):
    """An invocation did not complete before its deadline."""


class ProtobufCodec(Protocol):
    """Subset of the Unitelabs protobuf codec used by the bridge."""

    async def encode(self, path: str, value: dict[str, object]) -> bytes:
        """Encode one generated protobuf message."""
        ...

    async def decode(self, path: str, buffer: bytes) -> dict[str, object]:
        """Decode one generated protobuf message."""
        ...


class UnaryCallable(Protocol):
    """One asynchronous unary gRPC method."""

    def __call__(self, request: bytes) -> Awaitable[bytes]:
        """Invoke the RPC."""
        ...


class GrpcChannel(Protocol):
    """Structural channel contract used by unit and integration tests."""

    def unary_unary(self, path: str) -> UnaryCallable:
        """Return one unary RPC callable."""
        ...

    async def close(self) -> None:
        """Close the channel."""
        ...


class CommandWire(Protocol):
    """Standard SiLA observable-command confirmation codec."""

    def decode_confirmation(self, payload: bytes) -> str:
        """Decode a command execution identifier."""
        ...

    def encode_execution_uuid(self, value: str) -> bytes:
        """Encode a command execution identifier."""
        ...


class StandardCommandWire:
    """Encode and decode standard observable-command envelopes."""

    def decode_confirmation(self, payload: bytes) -> str:
        """Decode a standard SiLA command confirmation."""
        from sila.server import CommandConfirmation

        return CommandConfirmation.decode(payload).command_execution_uuid.value

    def encode_execution_uuid(self, value: str) -> bytes:
        """Encode a standard SiLA command execution identifier."""
        from sila.server import CommandExecutionUUID

        return CommandExecutionUUID(value=value).encode()


@dataclass
class _ConnectorCodecOwner:
    generator: AsyncGenerator[Connector, None]

    async def close(self) -> None:
        await self.generator.aclose()


@dataclass
class OT2SiLATransport:
    """Execute contract-validated OT-2 commands against a SiLA server."""

    channel: GrpcChannel
    codec: ProtobufCodec
    registry: ContractRegistry
    timeout_s: float = 60.0
    poll_interval_s: float = 0.05
    wire: CommandWire | None = None
    codec_owner: _ConnectorCodecOwner | None = None

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            msg = "timeout_s must be greater than zero"
            raise ValueError(msg)
        if self.poll_interval_s <= 0:
            msg = "poll_interval_s must be greater than zero"
            raise ValueError(msg)
        if self.wire is None:
            self.wire = StandardCommandWire()

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int = 50051,
        *,
        tls: bool = False,
        root_certificates: bytes | None = None,
        codec: ProtobufCodec | None = None,
        codec_config_path: str | Path = "config/ot2_dt_config.json",
        timeout_s: float = 60.0,
        connect_timeout_s: float = 10.0,
        poll_interval_s: float = 0.05,
    ) -> OT2SiLATransport:
        """Open a ready channel and compile the connector's exact protobuf codec."""
        if not host:
            msg = "host must not be empty"
            raise ValueError(msg)
        if not 1 <= port <= 65535:
            msg = "port must be between 1 and 65535"
            raise ValueError(msg)
        if connect_timeout_s <= 0:
            msg = "connect_timeout_s must be greater than zero"
            raise ValueError(msg)
        import grpc
        import grpc.aio

        owner: _ConnectorCodecOwner | None = None
        if codec is None:
            codec, owner = await _build_connector_codec(Path(codec_config_path))
        address = f"{host}:{port}"
        if tls:
            channel = grpc.aio.secure_channel(
                address,
                grpc.ssl_channel_credentials(root_certificates=root_certificates),
            )
        else:
            channel = grpc.aio.insecure_channel(address)
        try:
            await asyncio.wait_for(channel.channel_ready(), connect_timeout_s)
        except Exception as error:
            await channel.close()
            if owner is not None:
                await owner.close()
            msg = f"Could not connect to OT-2 SiLA server at {address}"
            raise OT2SiLAConnectionError(msg) from error
        return cls(
            channel=channel,
            codec=codec,
            registry=ContractRegistry.packaged(),
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            codec_owner=owner,
        )

    async def execute(self, command: OT2Command) -> object:
        """Execute a command or property after revalidating its contract binding."""
        try:
            binding = self.registry.binding_for(command)
        except ContractMismatchError as error:
            raise OT2SiLACommandError(command, str(error)) from error
        if command.execution_mode is ExecutionMode.OBSERVABLE:
            decoded = await self._call_observable(binding, command)
        else:
            decoded = await self._call_immediate(binding, command)
        value = next(iter(decoded.values()), None)
        return to_plain(value)

    async def close(self) -> None:
        """Close the channel and local codec owner."""
        try:
            await self.channel.close()
        finally:
            if self.codec_owner is not None:
                await self.codec_owner.close()
                self.codec_owner = None

    async def _call_immediate(self, binding: EndpointBinding, command: OT2Command) -> dict[str, object]:
        request = b""
        if command.endpoint_kind is EndpointKind.COMMAND:
            request = await self.codec.encode(
                f"{binding.package}.{binding.wire_method}_Parameters",
                command.parameters,
            )
        call = self.channel.unary_unary(self._rpc_path(binding, binding.wire_method))
        try:
            response = await asyncio.wait_for(call(request), self.timeout_s)
        except TimeoutError as error:
            raise OT2SiLACommandTimeout(command, f"no response within {self.timeout_s:g}s") from error
        except Exception as error:
            raise self._rpc_error(command, error) from error
        return await self.codec.decode(
            f"{binding.package}.{binding.wire_method}_Responses",
            response,
        )

    async def _call_observable(self, binding: EndpointBinding, command: OT2Command) -> dict[str, object]:
        request = await self.codec.encode(
            f"{binding.package}.{binding.endpoint}_Parameters",
            command.parameters,
        )
        start = self.channel.unary_unary(self._rpc_path(binding, binding.endpoint))
        try:
            confirmation_payload = await asyncio.wait_for(start(request), self.timeout_s)
            if self.wire is None:
                msg = "SiLA observable-command wire codec is unavailable"
                raise OT2SiLAConnectionError(msg)
            execution_uuid = self.wire.decode_confirmation(confirmation_payload)
            uuid_payload = self.wire.encode_execution_uuid(execution_uuid)
        except TimeoutError as error:
            raise OT2SiLACommandTimeout(
                command,
                f"confirmation not received within {self.timeout_s:g}s",
            ) from error
        except OT2SiLAError:
            raise
        except Exception as error:
            raise self._rpc_error(command, error) from error
        result_call = self.channel.unary_unary(self._rpc_path(binding, f"{binding.endpoint}_Result"))
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise OT2SiLACommandTimeout(command, f"result not ready within {self.timeout_s:g}s")
            try:
                response = await asyncio.wait_for(result_call(uuid_payload), remaining)
                return await self.codec.decode(
                    f"{binding.package}.{binding.endpoint}_Responses",
                    response,
                )
            except TimeoutError as error:
                raise OT2SiLACommandTimeout(
                    command,
                    f"result not received within {self.timeout_s:g}s",
                ) from error
            except Exception as error:
                if not _is_result_not_ready(error):
                    raise self._rpc_error(command, error) from error
                await asyncio.sleep(self.poll_interval_s)

    @staticmethod
    def _rpc_path(binding: EndpointBinding, method: str) -> str:
        return f"/{binding.package}.{binding.service}/{method}"

    @staticmethod
    def _rpc_error(command: OT2Command, error: Exception) -> OT2SiLAError:
        details = _decoded_details(error)
        code = _rpc_code_name(error)
        if code:
            return OT2SiLACommandError(command, f"{code}: {details}")
        return OT2SiLAConnectionError(f"OT-2 SiLA call failed for {command.endpoint}: {details}")


async def _build_connector_codec(config_path: Path) -> tuple[ProtobufCodec, _ConnectorCodecOwner]:
    """Compile messages from the connector itself without starting another server."""
    from unitelabs.cdk import SiLAServerConfig

    from .. import OpentronsOt2Config, create_app

    config = OpentronsOt2Config(
        use_simulator=True,
        digital_twin_config_path=str(config_path),
        sila_server=SiLAServerConfig(hostname="127.0.0.1", port=0, tls=False),
        cloud_server_endpoint=None,
        discovery=None,
    )
    generator = create_app(config)
    try:
        connector = await generator.__anext__()
    except Exception as error:
        await generator.aclose()
        msg = f"Could not compile the OT-2 protobuf codec from configuration {config_path}"
        raise OT2SiLAConnectionError(msg) from error
    return connector.sila_server.protobuf, _ConnectorCodecOwner(generator)


def to_plain(value: object) -> object:
    """Convert decoded dataclasses and enums into JSON-compatible evidence."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_plain(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return to_plain(value.value)
    if isinstance(value, Mapping):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(item) for item in value]
    return value


def _rpc_code_name(error: Exception) -> str:
    code_method = getattr(error, "code", None)
    if not callable(code_method):
        return ""
    code = code_method()
    return str(getattr(code, "name", None) or code)


def _decoded_details(error: Exception) -> str:
    details_method: Callable[[], object] | None = getattr(error, "details", None)
    if not callable(details_method):
        return str(error)
    raw = details_method()
    payload = raw if isinstance(raw, bytes) else str(raw or "").encode()
    try:
        return base64.b64decode(payload, validate=True).decode(errors="replace")
    except (ValueError, UnicodeError):
        return payload.decode(errors="replace")


def _is_result_not_ready(error: Exception) -> bool:
    return _rpc_code_name(error).upper() == "ABORTED" and "Result is not ready" in _decoded_details(error)
