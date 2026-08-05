"""Durable connector-owned digital-twin state with explicit evidence quality."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
from collections.abc import AsyncIterator
from pathlib import Path


class TipState(enum.Enum):
    """Tip state; UNKNOWN is mandatory after interrupted or unreconciled operations."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"
    UNKNOWN = "UNKNOWN"


class EvidenceSource(enum.Enum):
    """Evidence backing a connector state claim."""

    SOFTWARE_TRACKED = "SOFTWARE_TRACKED"
    HARDWARE_REPORTED = "HARDWARE_REPORTED"
    UNRECONCILED = "UNRECONCILED"


@dataclasses.dataclass(frozen=True)
class MountState:
    """Connector-owned pipette state for one mount."""

    mount: str
    tip_state: TipState
    tip_evidence: EvidenceSource
    liquid_volume: float
    liquid_volume_known: bool


@dataclasses.dataclass(frozen=True)
class StateSnapshot:
    """Revisioned state written after every state-changing unit operation."""

    revision: int
    mounts: tuple[MountState, ...]
    last_operation: str
    last_error: str

    def mount(self, name: str) -> MountState:
        """Return one mount state."""
        normalized = name.upper()
        for state in self.mounts:
            if state.mount == normalized:
                return state
        msg = f"Unknown mount {normalized}"
        raise KeyError(msg)


class DigitalTwinStateStore:
    """Owns durable state and wakes observable-property subscribers on changes."""

    def __init__(self, path: str, mounts: tuple[str, ...]) -> None:
        self._path = Path(path).expanduser() if path else None
        self._condition = asyncio.Condition()
        self._snapshot = self._load(mounts)

    @property
    def snapshot(self) -> StateSnapshot:
        """Return the current immutable snapshot."""
        return self._snapshot

    def _load(self, mounts: tuple[str, ...]) -> StateSnapshot:
        if self._path is not None and self._path.exists():
            try:
                value = json.loads(self._path.read_text(encoding="utf-8"))
                loaded = tuple(
                    MountState(
                        mount=str(item["mount"]),
                        tip_state=TipState(str(item["tip_state"])),
                        tip_evidence=EvidenceSource(str(item["tip_evidence"])),
                        liquid_volume=float(item["liquid_volume"]),
                        liquid_volume_known=bool(item["liquid_volume_known"]),
                    )
                    for item in value["mounts"]
                )
                if {item.mount for item in loaded} == set(mounts):
                    return StateSnapshot(
                        revision=int(value["revision"]),
                        mounts=loaded,
                        last_operation=str(value["last_operation"]),
                        last_error=str(value["last_error"]),
                    )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
        return StateSnapshot(
            revision=0,
            mounts=tuple(
                MountState(
                    mount=mount,
                    tip_state=TipState.UNKNOWN,
                    tip_evidence=EvidenceSource.UNRECONCILED,
                    liquid_volume=0.0,
                    liquid_volume_known=False,
                )
                for mount in mounts
            ),
            last_operation="STARTUP",
            last_error="State requires explicit reconciliation after startup.",
        )

    async def update_mount(
        self,
        mount: str,
        *,
        tip_state: TipState | None = None,
        tip_evidence: EvidenceSource | None = None,
        liquid_volume: float | None = None,
        liquid_volume_known: bool | None = None,
        operation: str,
        error: str = "",
    ) -> StateSnapshot:
        """Atomically update one mount and persist the new revision."""
        normalized = mount.upper()
        updated: list[MountState] = []
        found = False
        for current in self._snapshot.mounts:
            if current.mount != normalized:
                updated.append(current)
                continue
            found = True
            updated.append(
                MountState(
                    mount=current.mount,
                    tip_state=tip_state if tip_state is not None else current.tip_state,
                    tip_evidence=tip_evidence if tip_evidence is not None else current.tip_evidence,
                    liquid_volume=liquid_volume if liquid_volume is not None else current.liquid_volume,
                    liquid_volume_known=(
                        liquid_volume_known if liquid_volume_known is not None else current.liquid_volume_known
                    ),
                )
            )
        if not found:
            msg = f"Unknown mount {normalized}"
            raise KeyError(msg)
        snapshot = StateSnapshot(
            revision=self._snapshot.revision + 1,
            mounts=tuple(updated),
            last_operation=operation,
            last_error=error,
        )
        async with self._condition:
            self._snapshot = snapshot
            self._persist()
            self._condition.notify_all()
        return snapshot

    async def reconcile_tip(self, mount: str, present: bool) -> StateSnapshot:
        """Apply an explicit operator or hardware reconciliation."""
        return await self.update_mount(
            mount,
            tip_state=TipState.PRESENT if present else TipState.ABSENT,
            tip_evidence=EvidenceSource.SOFTWARE_TRACKED,
            liquid_volume=0.0,
            liquid_volume_known=not present,
            operation="RECONCILE_TIP",
        )

    async def record(self, operation: str, error: str = "") -> StateSnapshot:
        """Record a global operation without changing any mount state."""
        snapshot = StateSnapshot(
            revision=self._snapshot.revision + 1,
            mounts=self._snapshot.mounts,
            last_operation=operation,
            last_error=error,
        )
        async with self._condition:
            self._snapshot = snapshot
            self._persist()
            self._condition.notify_all()
        return snapshot

    async def subscribe(self) -> AsyncIterator[StateSnapshot]:
        """Yield the current state and every subsequent revision."""
        last_revision = -1
        while True:
            current = self._snapshot
            if current.revision != last_revision:
                last_revision = current.revision
                yield current
            async with self._condition:
                await self._condition.wait_for(
                    lambda observed_revision=last_revision: self._snapshot.revision != observed_revision
                )

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": self._snapshot.revision,
            "mounts": [
                {
                    "mount": item.mount,
                    "tip_state": item.tip_state.value,
                    "tip_evidence": item.tip_evidence.value,
                    "liquid_volume": item.liquid_volume,
                    "liquid_volume_known": item.liquid_volume_known,
                }
                for item in self._snapshot.mounts
            ],
            "last_operation": self._snapshot.last_operation,
            "last_error": self._snapshot.last_error,
        }
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self._path)
