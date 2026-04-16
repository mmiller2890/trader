"""Simple runtime snapshot persistence."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from config.schema import Mode
from models.order import OrderResult
from models.position import Balance, Position
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
        )
        await self.save(snapshot)
        return snapshot

    async def save(self, snapshot: StateSnapshot) -> None:
        """Persist snapshot."""

        payload = snapshot.model_dump(mode="json")
        async with self._lock:
            with self._path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, indent=2)

    async def load(self) -> StateSnapshot | None:
        """Load snapshot from disk if present."""

        if not self._path.exists():
            return None
        async with self._lock:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        return StateSnapshot.model_validate(payload)

    async def _collect_heartbeats(self, state_store: InMemoryStateStore) -> dict[str, datetime]:
        names = ("app", "market_data", "housekeeping", "execution")
        result: dict[str, datetime] = {}
        for name in names:
            heartbeat = await state_store.get_heartbeat(name)
            if heartbeat is not None:
                result[name] = heartbeat
        return result
