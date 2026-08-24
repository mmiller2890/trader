"""Bounded-state retention with archive-before-prune safety."""

from __future__ import annotations

import shutil
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from config.schema import ReliabilityConfig
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)
from models.position import FillCheckpoint, PositionLifecycle
from persistence.journal import JsonlJournal
from persistence.operations import OperationsRepository
from pydantic import BaseModel
from reliability.incidents import IncidentFactory
from state.store import InMemoryStateStore

MarketTokenKey = tuple[str, str]
Reporter = Callable[[OperationalIncident], Awaitable[None]]


class RetentionReport(BaseModel):
    """Counts of identities removed or archived during one pass."""

    signals_removed: int = 0
    market_books_removed: int = 0
    market_snapshots_removed: int = 0
    fill_checkpoints_removed: int = 0
    pnl_days_archived: int = 0
    closed_lifecycles_archived: int = 0
    outbox_rows_removed: int = 0
    incidents_removed: int = 0
    journals_removed: int = 0
    disk_percent: float


def _default_disk_usage(path: Path) -> float:
    total, _, free = shutil.disk_usage(path)
    if total <= 0:
        return 0.0
    return (total - free) / total * 100.0


class RetentionManager:
    """Runs one archive-before-prune retention pass over runtime state."""

    def __init__(
        self,
        *,
        repository: OperationsRepository,
        config: ReliabilityConfig,
        journal: JsonlJournal | None = None,
        data_path: Path | None = None,
        disk_usage: Callable[[Path], float] | None = None,
        delivered_outbox_retention_days: int = 30,
        incident_retention_days: int = 90,
    ) -> None:
        self._repository = repository
        self._config = config
        self._journal = journal
        self._data_path = Path(data_path or Path("data"))
        self._disk_usage = disk_usage or _default_disk_usage
        self._delivered_outbox_retention_days = max(
            1, int(delivered_outbox_retention_days)
        )
        self._incident_retention_days = max(1, int(incident_retention_days))
        self._reporter: Reporter | None = None
        self._factory = IncidentFactory()

    def set_reporter(self, reporter: Reporter) -> None:
        """Attach the incident reporter used for disk and persistence events."""

        self._reporter = reporter

    async def _emit(
        self,
        *,
        category: IncidentCategory,
        severity: IncidentSeverity,
        reason: str,
    ) -> None:
        if self._reporter is None:
            return
        try:
            await self._reporter(
                self._factory.from_reason(
                    component="retention",
                    category=category,
                    severity=severity,
                    reason=reason,
                )
            )
        except Exception:
            return

    async def run_once(
        self,
        *,
        state_store: InMemoryStateStore,
        active_market_keys: set[tuple[str, str]],
        now: datetime,
    ) -> RetentionReport:
        disk_percent = float(self._disk_usage(self._data_path))
        report = RetentionReport(disk_percent=disk_percent)

        if disk_percent >= self._config.disk_halt_percent:
            await self._emit(
                category=IncidentCategory.DISK,
                severity=IncidentSeverity.URGENT,
                reason="disk_halt",
            )
        elif disk_percent >= self._config.disk_degraded_percent:
            await self._emit(
                category=IncidentCategory.DISK,
                severity=IncidentSeverity.WARNING,
                reason="disk_degraded",
            )
        elif disk_percent >= self._config.disk_warning_percent:
            await self._emit(
                category=IncidentCategory.DISK,
                severity=IncidentSeverity.WARNING,
                reason="disk_warning",
            )

        book_keys = await state_store.copy_orderbook_keys()
        snapshot_keys = await state_store.copy_snapshot_keys()
        stale_books = [key for key in book_keys if key not in active_market_keys]
        stale_snapshots = [
            key for key in snapshot_keys if key not in active_market_keys
        ]
        signal_candidates = await self._signal_removal_candidates(
            state_store, now=now
        )
        checkpoint_candidates, lifecycle_candidates, pnl_candidates = (
            await self._archive_candidates(state_store, now=now)
        )

        try:
            await self._repository.archive_retention(
                checkpoints=checkpoint_candidates,
                pnl_days=pnl_candidates,
                lifecycles=lifecycle_candidates,
                archived_at=now,
            )
        except Exception as exc:
            await self._emit(
                category=IncidentCategory.PERSISTENCE,
                severity=IncidentSeverity.WARNING,
                reason=f"archive_write_failed:{type(exc).__name__}",
            )
            report.disk_percent = float(self._disk_usage(self._data_path))
            return report

        report.market_books_removed = await state_store.remove_orderbooks(
            stale_books
        )
        report.market_snapshots_removed = await state_store.remove_market_snapshots(
            stale_snapshots
        )
        report.signals_removed = await state_store.remove_signals(
            signal_candidates
        )
        report.fill_checkpoints_removed = await state_store.remove_fill_checkpoints(
            [item.order_key for item in checkpoint_candidates]
        )
        report.pnl_days_archived = await state_store.remove_realized_pnl_days(
            [day for day, _value in pnl_candidates]
        )
        report.closed_lifecycles_archived = (
            await state_store.remove_closed_position_lifecycles(
                lifecycle_candidates
            )
        )

        if self._journal is not None:
            report.journals_removed = await self._journal.maintain(now=now)

        outbox_rows, incidents = await self._repository.prune(
            delivered_before=now
            - timedelta(days=self._delivered_outbox_retention_days),
            incidents_before=now - timedelta(days=self._incident_retention_days),
        )
        report.outbox_rows_removed = outbox_rows
        report.incidents_removed = incidents

        report.disk_percent = float(self._disk_usage(self._data_path))
        return report

    async def _signal_removal_candidates(
        self,
        state_store: InMemoryStateStore,
        *,
        now: datetime,
    ) -> list[str]:
        index = await state_store.copy_signal_index()
        ordered = sorted(index, key=lambda item: item[1], reverse=True)
        beyond_cap = ordered[self._config.signal_retention_count :]
        age_cutoff = now - timedelta(hours=self._config.signal_retention_hours)
        too_old = [
            signal_id
            for signal_id, created_at in ordered
            if created_at < age_cutoff
        ]
        seen: set[str] = set()
        combined: list[str] = []
        for signal_id, _created_at in beyond_cap:
            if signal_id not in seen:
                seen.add(signal_id)
                combined.append(signal_id)
        for signal_id in too_old:
            if signal_id not in seen:
                seen.add(signal_id)
                combined.append(signal_id)
        return combined

    async def _archive_candidates(
        self,
        state_store: InMemoryStateStore,
        *,
        now: datetime,
    ) -> tuple[list[FillCheckpoint], list[PositionLifecycle], list[tuple[str, Decimal]]]:
        protected_order_ids: set[str] = set()
        for order in await state_store.get_open_orders():
            protected_order_ids.add(order.client_order_id)
            if order.exchange_order_id:
                protected_order_ids.add(order.exchange_order_id)
        position_keys = {
            (position.market_id, position.token_id)
            for position in await state_store.get_positions()
        }
        unresolved_incidents = [
            incident
            for incident in await self._repository.recent_incidents(limit=200)
            if incident.resolved_at is None
        ]

        def _incident_protects(checkpoint: FillCheckpoint) -> bool:
            for incident in unresolved_incidents:
                if incident.client_order_id == checkpoint.order_key:
                    return True
                if (
                    incident.market_id == checkpoint.market_id
                    and incident.token_id == checkpoint.token_id
                ):
                    return True
            return False

        checkpoint_cutoff = now - timedelta(
            days=self._config.fill_checkpoint_retention_days
        )
        checkpoint_candidates = [
            checkpoint
            for checkpoint in await state_store.get_fill_checkpoints()
            if checkpoint.confirmed_at < checkpoint_cutoff
            and checkpoint.order_key not in protected_order_ids
            and (checkpoint.market_id, checkpoint.token_id) not in position_keys
            and not _incident_protects(checkpoint)
        ]

        ledger = await state_store.get_realized_pnl_by_day()
        hot_floor = (
            now.astimezone(UTC).date()
            - timedelta(days=self._config.realized_pnl_hot_days - 1)
        ).isoformat()
        pnl_candidates = [
            (day, value)
            for day, value in sorted(ledger.items())
            if day < hot_floor
        ]

        closed = await state_store.get_closed_position_lifecycles()
        ordered_lifecycles = sorted(
            closed,
            key=lambda item: item.closed_at or item.last_fill_at,
            reverse=True,
        )
        lifecycle_candidates = ordered_lifecycles[
            self._config.closed_lifecycle_hot_count :
        ]

        return checkpoint_candidates, lifecycle_candidates, pnl_candidates
