"""Durable SQLite repository for leases, incidents, and the alert outbox."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    LeaseStatus,
    LiveOperatingLease,
    OperationalIncident,
    OutboxAlert,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS live_leases (
    lease_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    revoked_at TEXT,
    revocation_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_status ON live_leases(status);

CREATE TABLE IF NOT EXISTS operational_incidents (
    incident_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    component TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    consecutive_count INTEGER NOT NULL DEFAULT 1,
    market_id TEXT,
    token_id TEXT,
    client_order_id TEXT,
    resolved_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
    ON operational_incidents(fingerprint);

CREATE TABLE IF NOT EXISTS notification_outbox (
    alert_id TEXT PRIMARY KEY,
    incident_fingerprint TEXT NOT NULL,
    severity TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    delivered_at TEXT,
    last_error TEXT,
    dedupe_window_end TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_next_attempt
    ON notification_outbox(next_attempt_at);
"""


class OperationsRepository:
    """Async facade over one synchronous SQLite connection per operation."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False
        self._init_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return

            def _create() -> None:
                with self._connect() as connection:
                    connection.executescript(_SCHEMA)

            await asyncio.to_thread(_create)
            self._initialized = True

    def _run(self, work: Any) -> Any:
        return work

    async def create_lease(self, lease: LiveOperatingLease) -> None:
        await self._ensure_schema()

        def _work() -> None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE live_leases SET status = ? WHERE status = ?",
                    (LeaseStatus.REVOKED.value, LeaseStatus.ACTIVE.value),
                )
                connection.execute(
                    "INSERT INTO live_leases (lease_id, status, issued_at,"
                    " expires_at, config_fingerprint, revoked_at,"
                    " revocation_reason, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.lease_id,
                        lease.status.value,
                        _to_iso(lease.issued_at),
                        _to_iso(lease.expires_at),
                        lease.config_fingerprint,
                        _to_iso(lease.revoked_at) if lease.revoked_at else None,
                        lease.revocation_reason,
                        _to_iso(_utc_now()),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(_work)

    async def get_active_lease(self) -> LiveOperatingLease | None:
        await self._ensure_schema()

        def _work() -> dict[str, Any] | None:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT * FROM live_leases WHERE status = ?"
                    " ORDER BY issued_at DESC LIMIT 1",
                    (LeaseStatus.ACTIVE.value,),
                ).fetchone()
                return dict(row) if row is not None else None
            finally:
                connection.close()

        row = await asyncio.to_thread(_work)
        if row is None:
            return None
        return LiveOperatingLease(
            lease_id=row["lease_id"],
            issued_at=_from_iso(row["issued_at"]),  # type: ignore[arg-type]
            expires_at=_from_iso(row["expires_at"]),  # type: ignore[arg-type]
            config_fingerprint=row["config_fingerprint"],
            status=LeaseStatus(row["status"]),
            revoked_at=_from_iso(row["revoked_at"]),
            revocation_reason=row["revocation_reason"],
        )

    async def revoke_active_lease(
        self, *, reason: str, revoked_at: datetime
    ) -> LiveOperatingLease | None:
        await self._ensure_schema()

        def _work() -> dict[str, Any] | None:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM live_leases WHERE status = ?"
                    " ORDER BY issued_at DESC LIMIT 1",
                    (LeaseStatus.ACTIVE.value,),
                ).fetchone()
                if row is None:
                    already_revoked = connection.execute(
                        "SELECT * FROM live_leases WHERE status = ?"
                        " ORDER BY revoked_at DESC LIMIT 1",
                        (LeaseStatus.REVOKED.value,),
                    ).fetchone()
                    connection.commit()
                    return dict(already_revoked) if already_revoked else None
                connection.execute(
                    "UPDATE live_leases SET status = ?, revoked_at = ?,"
                    " revocation_reason = ? WHERE lease_id = ?",
                    (
                        LeaseStatus.REVOKED.value,
                        _to_iso(revoked_at),
                        reason[:512],
                        row["lease_id"],
                    ),
                )
                connection.commit()
                updated = connection.execute(
                    "SELECT * FROM live_leases WHERE lease_id = ?",
                    (row["lease_id"],),
                ).fetchone()
                return dict(updated) if updated is not None else None
            finally:
                connection.close()

        row = await asyncio.to_thread(_work)
        if row is None:
            return None
        return LiveOperatingLease(
            lease_id=row["lease_id"],
            issued_at=_from_iso(row["issued_at"]),  # type: ignore[arg-type]
            expires_at=_from_iso(row["expires_at"]),  # type: ignore[arg-type]
            config_fingerprint=row["config_fingerprint"],
            status=LeaseStatus(row["status"]),
            revoked_at=_from_iso(row["revoked_at"]),
            revocation_reason=row["revocation_reason"],
        )

    async def record_incident(self, incident: OperationalIncident) -> None:
        await self._ensure_schema()

        def _work() -> None:
            connection = self._connect()
            try:
                connection.execute(
                    "INSERT INTO operational_incidents (incident_id, fingerprint,"
                    " component, category, severity, reason, first_seen_at,"
                    " last_seen_at, consecutive_count, market_id, token_id,"
                    " client_order_id, resolved_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(incident_id) DO UPDATE SET"
                    " last_seen_at = excluded.last_seen_at,"
                    " consecutive_count = excluded.consecutive_count,"
                    " resolved_at = excluded.resolved_at,"
                    " updated_at = excluded.updated_at",
                    (
                        incident.incident_id,
                        incident.fingerprint,
                        incident.component,
                        incident.category.value,
                        incident.severity.value,
                        incident.reason,
                        _to_iso(incident.first_seen_at),
                        _to_iso(incident.last_seen_at),
                        incident.consecutive_count,
                        incident.market_id,
                        incident.token_id,
                        incident.client_order_id,
                        _to_iso(incident.resolved_at)
                        if incident.resolved_at
                        else None,
                        _to_iso(_utc_now()),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(_work)

    @staticmethod
    def _incident_from_row(row: dict[str, Any]) -> OperationalIncident:
        return OperationalIncident(
            incident_id=row["incident_id"],
            fingerprint=row["fingerprint"],
            component=row["component"],
            category=IncidentCategory(row["category"]),
            severity=IncidentSeverity(row["severity"]),
            reason=row["reason"],
            first_seen_at=_from_iso(row["first_seen_at"]),  # type: ignore[arg-type]
            last_seen_at=_from_iso(row["last_seen_at"]),  # type: ignore[arg-type]
            consecutive_count=row["consecutive_count"],
            market_id=row["market_id"],
            token_id=row["token_id"],
            client_order_id=row["client_order_id"],
            resolved_at=_from_iso(row["resolved_at"]),
        )

    async def recent_incidents(self, *, limit: int = 100) -> list[OperationalIncident]:
        await self._ensure_schema()

        def _work() -> list[dict[str, Any]]:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM operational_incidents"
                    " ORDER BY last_seen_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                connection.close()

        rows = await asyncio.to_thread(_work)
        return [self._incident_from_row(row) for row in rows]

    async def resolve_incident(
        self, incident_id: str, *, resolved_at: datetime
    ) -> OperationalIncident:
        await self._ensure_schema()

        def _work() -> dict[str, Any]:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE operational_incidents SET resolved_at = ?,"
                    " updated_at = ? WHERE incident_id = ?",
                    (_to_iso(resolved_at), _to_iso(_utc_now()), incident_id),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM operational_incidents WHERE incident_id = ?",
                    (incident_id,),
                ).fetchone()
                return dict(row) if row is not None else {}
            finally:
                connection.close()

        row = await asyncio.to_thread(_work)
        if not row:
            raise KeyError(f"unknown incident: {incident_id}")
        return self._incident_from_row(row)

    async def enqueue_alert(
        self, alert: OutboxAlert, *, dedupe_after: datetime
    ) -> OutboxAlert:
        await self._ensure_schema()

        def _work() -> dict[str, Any]:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM notification_outbox"
                    " WHERE incident_fingerprint = ? AND delivered_at IS NULL"
                    " AND created_at >= ?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (alert.incident_fingerprint, _to_iso(dedupe_after)),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO notification_outbox (alert_id,"
                        " incident_fingerprint, severity, text, created_at,"
                        " next_attempt_at, attempt_count, occurrence_count,"
                        " delivered_at, last_error, dedupe_window_end)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            alert.alert_id,
                            alert.incident_fingerprint,
                            alert.severity.value,
                            alert.text,
                            _to_iso(alert.created_at),
                            _to_iso(alert.next_attempt_at),
                            alert.attempt_count,
                            alert.occurrence_count,
                            None,
                            None,
                            _to_iso(dedupe_after),
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM notification_outbox WHERE alert_id = ?",
                        (alert.alert_id,),
                    ).fetchone()
                    connection.commit()
                    return dict(row) if row is not None else {}
                connection.execute(
                    "UPDATE notification_outbox SET occurrence_count ="
                    " occurrence_count + 1, dedupe_window_end = ?"
                    " WHERE alert_id = ?",
                    (_to_iso(dedupe_after), existing["alert_id"]),
                )
                updated = connection.execute(
                    "SELECT * FROM notification_outbox WHERE alert_id = ?",
                    (existing["alert_id"],),
                ).fetchone()
                connection.commit()
                return dict(updated) if updated is not None else {}
            finally:
                connection.close()

        row = await asyncio.to_thread(_work)
        return OutboxAlert(
            alert_id=row["alert_id"],
            incident_fingerprint=row["incident_fingerprint"],
            severity=IncidentSeverity(row["severity"]),
            text=row["text"],
            created_at=_from_iso(row["created_at"]),  # type: ignore[arg-type]
            next_attempt_at=_from_iso(row["next_attempt_at"]),  # type: ignore[arg-type]
            attempt_count=row["attempt_count"],
            occurrence_count=row["occurrence_count"],
            delivered_at=_from_iso(row["delivered_at"]),
            last_error=row["last_error"],
        )

    @staticmethod
    def _alert_from_row(row: dict[str, Any]) -> OutboxAlert:
        return OutboxAlert(
            alert_id=row["alert_id"],
            incident_fingerprint=row["incident_fingerprint"],
            severity=IncidentSeverity(row["severity"]),
            text=row["text"],
            created_at=_from_iso(row["created_at"]),  # type: ignore[arg-type]
            next_attempt_at=_from_iso(row["next_attempt_at"]),  # type: ignore[arg-type]
            attempt_count=row["attempt_count"],
            occurrence_count=row["occurrence_count"],
            delivered_at=_from_iso(row["delivered_at"]),
            last_error=row["last_error"],
        )

    async def due_alerts(self, *, now: datetime, limit: int = 20) -> list[OutboxAlert]:
        await self._ensure_schema()

        def _work() -> list[dict[str, Any]]:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT * FROM notification_outbox"
                    " WHERE delivered_at IS NULL AND next_attempt_at <= ?"
                    " ORDER BY next_attempt_at ASC LIMIT ?",
                    (_to_iso(now), limit),
                ).fetchall()
                return [dict(row) for row in rows]
            finally:
                connection.close()

        rows = await asyncio.to_thread(_work)
        return [self._alert_from_row(row) for row in rows]

    async def mark_alert_delivered(self, alert_id: str, *, delivered_at: datetime) -> None:
        await self._ensure_schema()

        def _work() -> None:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE notification_outbox SET delivered_at = ?"
                    " WHERE alert_id = ?",
                    (_to_iso(delivered_at), alert_id),
                )
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(_work)

    async def reschedule_alert(
        self, alert_id: str, *, next_attempt_at: datetime, error: str
    ) -> None:
        await self._ensure_schema()
        sanitized = error[:256]

        def _work() -> None:
            connection = self._connect()
            try:
                connection.execute(
                    "UPDATE notification_outbox SET next_attempt_at = ?,"
                    " attempt_count = attempt_count + 1, last_error = ?"
                    " WHERE alert_id = ?",
                    (_to_iso(next_attempt_at), sanitized, alert_id),
                )
                connection.commit()
            finally:
                connection.close()

        await asyncio.to_thread(_work)

    async def outbox_stats(self, *, now: datetime) -> tuple[int, float | None]:
        await self._ensure_schema()

        def _work() -> tuple[int, float | None]:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT COUNT(*) AS depth, MIN(created_at) AS oldest"
                    " FROM notification_outbox WHERE delivered_at IS NULL"
                ).fetchone()
                depth = int(row["depth"] or 0)
                oldest = _from_iso(row["oldest"]) if row["oldest"] else None
                if oldest is None:
                    return depth, None
                age_seconds = max(0.0, (now - oldest).total_seconds())
                return depth, age_seconds
            finally:
                connection.close()

        return await asyncio.to_thread(_work)

    async def last_delivered_at(self, incident_fingerprint: str) -> datetime | None:
        await self._ensure_schema()

        def _work() -> str | None:
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT MAX(delivered_at) AS latest FROM notification_outbox"
                    " WHERE incident_fingerprint = ? AND delivered_at IS NOT NULL",
                    (incident_fingerprint,),
                ).fetchone()
                return row["latest"] if row is not None else None
            finally:
                connection.close()

        latest = await asyncio.to_thread(_work)
        return _from_iso(latest)

    async def prune(
        self,
        *,
        delivered_before: datetime,
        incidents_before: datetime,
    ) -> tuple[int, int]:
        await self._ensure_schema()

        def _work() -> tuple[int, int]:
            connection = self._connect()
            try:
                alerts_cursor = connection.execute(
                    "DELETE FROM notification_outbox"
                    " WHERE delivered_at IS NOT NULL AND delivered_at < ?",
                    (_to_iso(delivered_before),),
                )
                incidents_cursor = connection.execute(
                    "DELETE FROM operational_incidents WHERE resolved_at IS NOT NULL"
                    " AND resolved_at < ?",
                    (_to_iso(incidents_before),),
                )
                removed_alerts = alerts_cursor.rowcount
                removed_incidents = incidents_cursor.rowcount
                connection.commit()
                return max(0, removed_alerts), max(0, removed_incidents)
            finally:
                connection.close()

        return await asyncio.to_thread(_work)
