from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.schema import AppConfig
from models.events import BotEvent, EventType
from models.operations import IncidentSeverity, OutboxAlert
from notifications.outbox import AlertService
from persistence.operations import OperationsRepository
from reliability.metrics import DailySummaryEmitter, OperationalMetrics


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def make_alert_service(repository: OperationsRepository) -> AlertService:
    return AlertService(repository, AppConfig())


@pytest.mark.asyncio
async def test_emits_one_summary_per_utc_day_through_outbox(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    alerts = make_alert_service(repository)
    metrics = OperationalMetrics(
        repository=repository,
        now=lambda: NOW,
        state_provider=None,
    )
    await metrics.record_event(
        BotEvent(
            event_id="ev-start-00001",
            event_type=EventType.BOT_STARTED,
            component="runtime",
            mode="dry_run",
            message="bot started",
            created_at=NOW - timedelta(hours=3),
        )
    )
    emitter = DailySummaryEmitter(
        metrics=metrics,
        alert_service=alerts,
        repository=repository,
        hour_utc=0,
        now=lambda: NOW,
    )

    first = await emitter.maybe_emit()
    second = await emitter.maybe_emit()

    assert first is True
    assert second is False
    due = await repository.due_alerts(now=NOW + timedelta(days=2), limit=10)
    summaries = [alert for alert in due if alert.text.startswith("daily_summary")]
    assert len(summaries) == 1
    assert summaries[0].severity == IncidentSeverity.INFO
    assert "uptime_seconds" in summaries[0].text


@pytest.mark.asyncio
async def test_summary_survives_restart_around_midnight_without_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.sqlite3"
    before_midnight = datetime(2026, 8, 24, 23, 59, tzinfo=UTC)
    repository = OperationsRepository(path)
    alerts = make_alert_service(repository)
    metrics = OperationalMetrics(repository=repository, now=lambda: before_midnight)
    emitter_before = DailySummaryEmitter(
        metrics=metrics,
        alert_service=alerts,
        repository=OperationsRepository(path),
        hour_utc=0,
        now=lambda: before_midnight,
    )
    assert await emitter_before.maybe_emit() is True

    after_restart = datetime(2026, 8, 25, 0, 1, tzinfo=UTC)
    restored_emitter = DailySummaryEmitter(
        metrics=OperationalMetrics(
            repository=OperationsRepository(path), now=lambda: after_restart
        ),
        alert_service=make_alert_service(OperationsRepository(path)),
        repository=OperationsRepository(path),
        hour_utc=0,
        now=lambda: after_restart,
    )

    assert await restored_emitter.maybe_emit() is True
    due_old = await repository.due_alerts(
        now=after_restart + timedelta(days=2), limit=50
    )
    old_day = [
        alert
        for alert in due_old
        if alert.incident_fingerprint.endswith(":2026-08-24")
    ]
    assert len(old_day) == 1


@pytest.mark.asyncio
async def test_emitter_waits_for_configured_utc_hour(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    early = datetime(2026, 8, 24, 0, 30, tzinfo=UTC)
    emitter = DailySummaryEmitter(
        metrics=OperationalMetrics(
            repository=repository, now=lambda: early
        ),
        alert_service=make_alert_service(repository),
        repository=repository,
        hour_utc=6,
        now=lambda: early,
    )

    assert await emitter.maybe_emit() is False
    assert await repository.due_alerts(now=early + timedelta(days=1), limit=10) == []


@pytest.mark.asyncio
async def test_enqueued_summary_is_durable_and_deliverable_later(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    alerts = make_alert_service(repository)
    emitter = DailySummaryEmitter(
        metrics=OperationalMetrics(repository=repository, now=lambda: NOW),
        alert_service=alerts,
        repository=repository,
        hour_utc=0,
        now=lambda: NOW,
    )
    assert await emitter.maybe_emit() is True

    restored = await OperationsRepository(tmp_path / "bot.sqlite3").due_alerts(
        now=NOW + timedelta(days=2), limit=10
    )
    assert len(restored) == 1
    alert: OutboxAlert = restored[0]
    assert alert.severity == IncidentSeverity.INFO
    assert alert.incident_fingerprint == "ops:daily-summary:2026-08-24"
