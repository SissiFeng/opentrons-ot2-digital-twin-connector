"""Revisioned current and observable OT-2 state provider."""

from unitelabs.cdk import sila

from ..digital_twin.state import StateSnapshot
from ..io import OT2DigitalTwinController
from ._digital_twin_types import (
    Mount,
    MountState,
    Position,
    RobotState,
    StateEvidence,
    TipPresence,
)
from .device_information import CATEGORY, ORIGINATOR


def _to_state(controller: OT2DigitalTwinController, snapshot: StateSnapshot) -> RobotState:
    mounts = []
    for item in snapshot.mounts:
        position = controller.mount_position(item.mount)
        mount_axis = "Z" if item.mount == "LEFT" else "A"
        mounts.append(
            MountState(
                mount=Mount[item.mount],
                position=Position(x=position.x, y=position.y, z=position.z),
                homed=all(controller.homed_flags.get(axis, False) for axis in ("X", "Y", mount_axis)),
                tip_presence=TipPresence[item.tip_state.name],
                tip_evidence=StateEvidence[item.tip_evidence.name],
                liquid_volume=item.liquid_volume,
                liquid_volume_known=item.liquid_volume_known,
            )
        )
    return RobotState(
        revision=snapshot.revision,
        door_closed=controller.door_closed,
        simulation=controller.is_simulating,
        last_operation=snapshot.last_operation,
        last_error=snapshot.last_error,
        mounts=mounts,
        modules=list(controller.modules),
    )


class RobotStateProvider(sila.Feature):
    """Provides revisioned state for reconciliation after every side effect."""

    def __init__(self, controller: OT2DigitalTwinController) -> None:
        super().__init__(
            originator=ORIGINATOR,
            category=CATEGORY,
            identifier="RobotStateProvider",
            name="Robot State Provider",
            version="1.0",
        )
        self._controller = controller

    @sila.UnobservableProperty()
    def current_state(self) -> RobotState:
        """Return the current connector-owned state snapshot."""
        return _to_state(self._controller, self._controller.state.snapshot)

    @sila.ObservableProperty()
    async def state_updates(self) -> sila.Stream[RobotState]:
        """Stream the current state and every subsequent revision."""
        async for snapshot in self._controller.state.subscribe():
            yield _to_state(self._controller, snapshot)
