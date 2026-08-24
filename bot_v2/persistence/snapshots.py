"""Simple runtime snapshot persistence."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from config.schema import Mode
from models.order import OrderResult
from models.position import Balance, FillCheckpoint, Position, PositionLifecycle
from pydantic import BaseModel, ConfigDict, Field
from state.store import InMemoryStateStore


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class StateSnapshot(BaseModel):
    """Serializable runtime snapshot."""

    model_config = ConfigDict(extra="forbid")

    mode: Mode
    open_orders: list[OrderResult] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    balances: list[Balance] = Field(default_factory=list)
    heartbeats: dict[str, datetime] = Field(default_factory=dict)
    kill_switch_active: bool = False
    kill_switch_reason: str | None = None
    fill_checkpoints: list[FillCheckpoint] = Field(default_factory=list)
    position_lifecycles: list[PositionLifecycle] = Field(default_factory=list)
    saved_at: datetime = Field(default_factory=utc_now)


class SnapshotStore:
    """JSON snapshot persistence helper."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Snapshot path."""

        return self._path

    async def save_from_state(self, state_store: InMemoryStateStore) -> StateSnapshot:
        """Serialize current in-memory state to disk."""

        snapshot = StateSnapshot(
            mode=state_store.mode,
            open_orders=await state_store.get_open_orders(),
            positions=await state_store.get_positions(),
            balances=await state_store.get_balances(),
            heartbeats=await self._collect_heartbeats(state_store),
            kill_switch_active=await state_store.is_kill_switch_active(),
            kill_switch_reason=await state_store.get_kill_switch_reason(),
            fill_checkpoints=await state_store.get_fill_checkpoints(),
            position_lifecycles=await state_store.get_position_lifecycles(),
        )
        await self.save(snapshot)
        return snapshot

    async def save(self, snapshot: StateSnapshot) -> None:
        """Persist snapshot atomically via a temporary file replacement."""

        payload = snapshot.model_dump(mode="json")
        async with self._lock:
            fd, temp_name = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temp_name).replace(self._path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise

    async def load(self) -> StateSnapshot | None:
        """Load snapshot from disk if present."""

        if not self._path.exists():
            return None
        async with self._lock:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        return StateSnapshot.model_validate(payload)

    async def restore_into_state(
        self,
        state_store: InMemoryStateStore,
        *,
        restore_heartbeats: bool = True,
        restore_positions: bool = True,
    ) -> bool:
        """Restore a compatible snapshot into a fresh in-memory store."""

        snapshot = await self.load()
        if snapshot is None or snapshot.mode != state_store.mode:
            return False
        for order in snapshot.open_orders:
            await state_store.set_order_status(order)
        if restore_positions:
            for position in snapshot.positions:
                await state_store.set_position(position)
        for balance in snapshot.balances:
            await state_store.set_balance(balance)
        for checkpoint in snapshot.fill_checkpoints:
            await state_store.restore_fill_checkpoint(checkpoint)
        for lifecycle in snapshot.position_lifecycles:
            await state_store.restore_position_lifecycle(lifecycle)
        if restore_heartbeats:
            for component, timestamp in snapshot.heartbeats.items():
                await state_store.update_heartbeat(component, timestamp)
        return True

    async def _collect_heartbeats(self, state_store: InMemoryStateStore) -> dict[str, datetime]:
        names = (
            "app",
            "market_data",
            "market_transport",
            "housekeeping",
            "execution",
        )
        result: dict[str, datetime] = {}
        for name in names:
            heartbeat = await state_store.get_heartbeat(name)
            if heartbeat is not None:
                result[name] = heartbeat
        return result
