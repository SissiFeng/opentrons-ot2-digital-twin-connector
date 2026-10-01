"""Inject device calls and native Matterix builders at the package boundary."""

from __future__ import annotations

import importlib
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence

from .mirror import Call
from .models import Operation, Outcome

Builder = Callable[[Operation], Sequence[object]]
Submit = Callable[[Operation, tuple[object, ...]], Awaitable[Outcome]]


class CallableAdapter:
    """Adapt an existing API without importing its transport or orchestrator.

    The mapper must validate capability/identity/parameters synchronously and
    return an async completion call. That call executes once, observes post-state
    and returns an Outcome. It must yield to the loop; blocking drivers need a
    backend-owned worker, not an event-loop-blocking wrapper.
    """

    def __init__(self, prepare: Callable[[Operation], Call]) -> None:
        self._prepare = prepare

    def prepare(self, operation: Operation) -> Call:
        """Delegate side-effect-free request mapping to the integration owner."""
        return self._prepare(operation)


class MatterixAdapter:
    """Map operations to native configs and await an injected persistent runtime.

    submit owns the StateMachine/environment and must return only after the
    complete sequence finishes (not merely after set_action_sequence). Builders
    own geometry, tip binding and mount/channel capability validation. No legacy
    connector translation or synthetic state prediction is used here.
    """

    def __init__(self, device_id: str, builders: Mapping[str, Builder], submit: Submit) -> None:
        self._device_id = device_id
        self._builders = dict(builders)
        self._submit = submit

    def prepare(self, operation: Operation) -> Call:
        """Build a flat native sequence before real dispatch is allowed."""
        if operation.schema != "ot2.operation/1" or operation.device_id != self._device_id:
            raise ValueError("Matterix operation schema or device binding mismatch")
        if operation.action not in self._builders:
            raise ValueError(f"No Matterix builder for {operation.action!r}")
        configs = tuple(self._builders[operation.action](operation))
        if not configs or any(item is None or isinstance(item, (list, tuple)) for item in configs):
            raise ValueError("Matterix builder must return a non-empty flat configuration sequence")

        async def execute() -> Outcome:
            return await self._submit(operation, configs)

        return execute


def native_liquid_builders(asset_name: str) -> dict[str, Builder]:
    """Load PR46 native liquid configs in an already initialized Matterix process.

    These operations change liquid at the current pose; they do not include
    well motion. Only the inspected LEFT/single-channel contract is accepted.
    Multi-channel or well-aware operations need an explicit upstream builder.
    """
    if not asset_name:
        raise ValueError("Matterix asset name is required")
    module = importlib.import_module("matterix_sm.compositional_actions.ot2_liquid_handling")

    def builder(name: str) -> Builder:
        constructor = getattr(module, name)

        def build(operation: Operation) -> Sequence[object]:
            params = operation.parameters
            if set(params) != {"mount", "channels", "volume_ul", "flow_rate_ul_s"}:
                raise ValueError("Native liquid mapping requires exactly mount, channels, volume_ul, flow_rate_ul_s")
            if params["mount"] != "LEFT" or type(params["channels"]) is not int or params["channels"] != 1:
                raise ValueError("Native liquid mapping supports only the inspected LEFT/single-channel capability")
            for key in ("volume_ul", "flow_rate_ul_s"):
                value = params[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError(f"{key} must be positive and finite")
            return (constructor(asset_name=asset_name, volume=params["volume_ul"], flow_rate=params["flow_rate_ul_s"]),)

        return build

    return {"aspirate": builder("AspirateOT2Cfg"), "dispense": builder("DispenseOT2Cfg")}
