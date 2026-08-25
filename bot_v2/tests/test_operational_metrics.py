from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from models.events import BotEvent, EventType
from persistence.operations import OperationsRepository
from reliability.metrics import OperationalMetrics


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def event(
    event_id: str,
    event_type: EventType,
    *,
    created_at: datetime = NOW,
    reason: str | None = None,
) -> BotEvent:
    return BotEvent(
        event_id=event_id,
        event_type=event_type,
        component="test",
        mode="dry_run",
        message="metric test",
        client_order_id="client-order-0001",
        reason=reason,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_same_event_counts_once_across_repository_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.sqlite3"
    first = OperationalMetrics(repository=OperationsRepository(path))
    await first.record_event(event("event-00000001", EventType.ORDER_SUBMITTED))
    await first.record_event(event("event-00000001", EventType.ORDER_SUBMITTED))

    restored = OperationalMetrics(repository=OperationsRepository(path))
    await restored.record_event(event("event-00000001", EventType.ORDER_SUBMITTED))

    summary = await restored.summary(NOW.date())
    assert summary.orders_submitted == 1


@pytest.mark.asyncio
async def test_market_rotation_counts_once_per_market_per_day(tmp_path: Path) -> None:
    metrics = OperationalMetrics(repository=OperationsRepository(tmp_path / "b.sqlite3"))
    await metrics.record_market_rotation("market-auto-1", at=NOW)
    await metrics.record_market_rotation("market-auto-1", at=NOW + timedelta(minutes=15))
    await metrics.record_market_rotation("market-auto-2", at=NOW + timedelta(minutes=30))

    summary = await metrics.summary(NOW.date())
    assert summary.markets_rotated == 2


@pytest.mark.asyncio
async def test_recovery_accumulates_duration_once_per_fingerprint(
    tmp_path: Path,
) -> None:
    metrics = OperationalMetrics(repository=OperationsRepository(tmp_path / "b.sqlite3"))
    await metrics.record_recovery("incident:ws-outage", 140.5, at=NOW)
    await metrics.record_recovery("incident:ws-outage", 999.0, at=NOW)
    await metrics.record_recovery("incident:data-api", 60.0, at=NOW)

    summary = await metrics.summary(NOW.date())
    assert summary.recoveries == 2
    assert summary.degraded_seconds == pytest.approx(200.5)


@pytest.mark.asyncio
async def test_rejections_are_distinct_from_submissions_and_fills(
    tmp_path: Path,
) -> None:
    metrics = OperationalMetrics(repository=OperationsRepository(tmp_path / "b.sqlite3"))
    await metrics.record_event(event("ev-sub-000001", EventType.ORDER_SUBMITTED))
    await metrics.record_event(
        event("ev-rej-000002", EventType.ORDER_RESULT, reason="rejected:insufficient_balance")
    )
    await metrics.record_event(event("ev-fill-00003", EventType.POSITION_UPDATED))

    summary = await metrics.summary(NOW.date())
    assert summary.orders_submitted == 1
    assert summary.fills_accounted == 1
    assert summary.orders_rejected == 1


@pytest.mark.asyncio
async def test_utc_day_boundaries_keep_counters_separate(tmp_path: Path) -> None:
    metrics = OperationalMetrics(repository=OperationsRepository(tmp_path / "b.sqlite3"))
    late_day_one = datetime(2026, 8, 23, 23, 0, tzinfo=UTC)
    late_day_one_b = datetime(2026, 8, 23, 23, 30, tzinfo=UTC)
    next_day = datetime(2026, 8, 25, 1, 0, tzinfo=UTC)
    await metrics.record_event(
        event("ev-day1-000001", EventType.ORDER_SUBMITTED, created_at=late_day_one)
    )
    await metrics.record_event(
        event("ev-day2-000001", EventType.ORDER_SUBMITTED, created_at=late_day_one_b)
    )
    await metrics.record_event(
        event("ev-day3-000001", EventType.ORDER_SUBMITTED, created_at=next_day)
    )

    assert (await metrics.summary(late_day_one.date())).orders_submitted == 2
    assert (await metrics.summary(next_day.date())).orders_submitted == 1
    assert (await metrics.summary(NOW.date())).orders_submitted == 0


@pytest.mark.asyncio
async def test_summary_pulls_authoritative_pnl_and_runtime_fields(
    tmp_path: Path,
) -> None:
    async def pnl() -> tuple[Decimal | None, Decimal | None]:
        return Decimal("12.5"), Decimal("0.75")

    async def state() -> str:
        return "running"

    async def pending() -> int:
        return 4

    metrics = OperationalMetrics(
        repository=OperationsRepository(tmp_path / "b.sqlite3"),
        now=lambda: NOW + timedelta(hours=2),
        pnl_provider=pnl,
        state_provider=state,
        outbox_pending_provider=pending,
        disk_percent_provider=lambda: 61.5,
        lease_remaining_seconds_provider=lambda: 3600.0,
    )

    summary = await metrics.summary(NOW.date())

    assert summary.realized_pnl == Decimal("12.5")
    assert summary.unrealized_pnl == Decimal("0.75")
    assert summary.state == "running"
    assert summary.pending_alerts == 4
    assert summary.disk_percent == 61.5
    assert summary.lease_remaining_seconds == 3600.0
