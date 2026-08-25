"""Atomic runtime-health snapshots and liveness/readiness answers."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from clients.ws_client import WebSocketHealth
from models.operations import OperationalState, TaskHealth
from persistence.operations import OperationsRepository
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class HealthAnswer(BaseModel):
    """Read-only answer for one deployment health concept."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    state: OperationalState
    reason: str
    generated_at: datetime = Field(default_factory=utc_now)


class RuntimeHealthSnapshot(BaseModel):
    """Secret-free operational health view of the whole process."""

    model_config = ConfigDict(extra="forbid")

    process_live: bool
    service_ready: bool
    trading_ready: bool
    state: OperationalState
    reason: str | None
    tasks: list[TaskHealth]
    websocket: WebSocketHealth
    market_data_source: Literal["websocket", "rest_fallback", "unavailable"]
    last_reconciliation_at: datetime | None
    outbox_pending: int = 0
    oldest_outbox_age_seconds: float | None = None
    disk_percent: float = 0.0
    lease_expires_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class HealthSnapshotStore:
    """Temp-file + fsync + replace persistence for health snapshots."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def save(self, snapshot: RuntimeHealthSnapshot) -> None:
        payload = snapshot.model_dump(mode="json")
        async with self._lock:
            fd, temp_name = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                Path(temp_name).replace(self._path)
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise

    async def load(self) -> RuntimeHealthSnapshot | None:
        if not self._path.exists():
            return None
        try:
            async with self._lock:
                with self._path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            return RuntimeHealthSnapshot.model_validate(payload)
        except Exception:
            return None


async def build_runtime_health(
    *,
    operational_state: OperationalState,
    reason: str | None,
    tasks: list[TaskHealth],
    supervisor_alive: bool,
    websocket: WebSocketHealth | None = None,
    rest_fallback_active: bool = False,
    last_reconciliation_at: datetime | None = None,
    repository: OperationsRepository | None = None,
    data_path: Path | None = None,
    disk_usage: Callable[[Path], float] | None = None,
    now: datetime | None = None,
) -> RuntimeHealthSnapshot:
    """Assemble one atomic health snapshot from typed inputs."""

    moment = now or utc_now()
    ws_health = websocket or WebSocketHealth()
    if ws_health.connected:
        source: Literal["websocket", "rest_fallback", "unavailable"] = "websocket"
    elif rest_fallback_active:
        source = "rest_fallback"
    else:
        source = "unavailable"

    service_ready = (
        supervisor_alive and len(tasks) > 0 and all(item.running for item in tasks)
    )
    trading_ready = service_ready and operational_state == OperationalState.RUNNING

    outbox_pending = 0
    oldest_outbox_age_seconds: float | None = None
    lease_expires_at: datetime | None = None
    if repository is not None:
        try:
            outbox_pending, oldest_outbox_age_seconds = await repository.outbox_stats(
                now=moment
            )
            lease = await repository.get_active_lease()
            lease_expires_at = lease.expires_at if lease else None
        except Exception:
            outbox_pending = 0
            oldest_outbox_age_seconds = None
            lease_expires_at = None

    disk_percent = 0.0
    if disk_usage is not None and data_path is not None:
        try:
            disk_percent = float(disk_usage(data_path))
        except Exception:
            disk_percent = 0.0

    return RuntimeHealthSnapshot(
        process_live=supervisor_alive,
        service_ready=service_ready,
        trading_ready=trading_ready,
        state=operational_state,
        reason=reason,
        tasks=tasks,
        websocket=ws_health,
        market_data_source=source,
        last_reconciliation_at=last_reconciliation_at,
        outbox_pending=outbox_pending,
        oldest_outbox_age_seconds=oldest_outbox_age_seconds,
        disk_percent=disk_percent,
        lease_expires_at=lease_expires_at,
        updated_at=moment,
    )
