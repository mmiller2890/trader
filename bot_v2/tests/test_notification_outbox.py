from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.schema import AppConfig
from models.events import BotEvent, EventType
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)
from notifications.outbox import (
    AlertService,
    NotificationDeliveryError,
    NotificationWorker,
    TelegramTransport,
    _redact,
)
from persistence.operations import OperationsRepository


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def config() -> AppConfig:
    return AppConfig(
        notifications={
            "durable_outbox_enabled": True,
            "telegram_deduplication_seconds": 900,
            "alert_retry_initial_seconds": 2,
            "alert_retry_max_seconds": 300,
        },
        secrets={
            "private_key": "never-send-private-key",
            "clob_api_key": "never-send-api-key",
            "telegram_bot_token": "never-send-bot-token",
            "polymarket_proxy_address": "0xfunder0000000000000000000000000000000000",
        },
    )


def warning_incident() -> OperationalIncident:
    return OperationalIncident(
        incident_id="incident-12345678",
        fingerprint="reconciliation:data_api_timeout",
        component="reconciliation",
        category=IncidentCategory.TRANSIENT_TRANSPORT,
        severity=IncidentSeverity.WARNING,
        reason="data_api_timeout",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


class FakeTelegram:
    def __init__(self, outcomes: list[Exception | None]) -> None:
        self._outcomes = list(outcomes)
        self.sent: list[object] = []

    async def send(self, alert: object) -> None:
        self.sent.append(alert)
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_enqueue_incident_persists_before_return(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config(), now=lambda: NOW)
    alert_row = await service.enqueue_incident(warning_incident())

    reread = OperationsRepository(tmp_path / "bot.sqlite3")
    due = await reread.due_alerts(now=NOW + timedelta(seconds=1), limit=10)
    assert len(due) == 1
    assert due[0].alert_id == alert_row.alert_id
    assert due[0].incident_fingerprint == warning_incident().fingerprint


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_after_restart(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    await AlertService(repository, config(), now=lambda: NOW).enqueue_incident(
        warning_incident()
    )
    failing = FakeTelegram([NotificationDeliveryError("telegram_not_configured")])
    first = NotificationWorker(repository, failing, config(), now=lambda: NOW)
    assert await first.deliver_due_once() == 0

    succeeding = FakeTelegram([None])
    restored = NotificationWorker(
        OperationsRepository(tmp_path / "bot.sqlite3"),
        succeeding,
        config(),
        now=lambda: NOW + timedelta(minutes=10),
    )
    assert await restored.deliver_due_once() == 1
    assert succeeding.sent[0].incident_fingerprint == warning_incident().fingerprint


@pytest.mark.asyncio
async def test_identical_incidents_dedupe_with_occurrence_count(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config(), now=lambda: NOW)
    await service.enqueue_incident(warning_incident())
    await service.enqueue_incident(warning_incident())

    due = await repository.due_alerts(now=NOW, limit=20)
    assert len(due) == 1
    assert due[0].occurrence_count >= 2


@pytest.mark.asyncio
async def test_enqueue_event_survives_delivery_failure(tmp_path: Path) -> None:
    class ExplodingTransport(TelegramTransport):
        async def send(self, alert: object) -> None:
            raise NotificationDeliveryError("ConnectError")

    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config())
    transport = ExplodingTransport(config())
    worker = NotificationWorker(repository, transport, config(), now=lambda: NOW)

    event = BotEvent(
        event_type=EventType.KILL_SWITCH_TRIPPED,
        component="router",
        mode="live",
        message="halt latched",
        reason="accounting_invariant",
    )
    queued = await service.enqueue_event(event)
    assert queued is not None

    delivered = await worker.deliver_due_once()
    assert delivered == 0


@pytest.mark.asyncio
async def test_alert_text_never_contains_configured_secrets(tmp_path: Path) -> None:
    incident = warning_incident().model_copy(
        update={
            "reason": "leak never-send-private-key never-send-api-key "
            "never-send-bot-token 0xfunder0000000000000000000000000000000000"
        }
    )
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config())
    alert_row = await service.enqueue_incident(incident)

    payload = json.dumps(alert_row.model_dump(mode="json"))
    for secret in (
        "never-send-private-key",
        "never-send-api-key",
        "never-send-bot-token",
        "0xfunder0000000000000000000000000000000000",
    ):
        assert secret not in payload


@pytest.mark.asyncio
async def test_recovery_generates_one_recovery_notice(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config(), now=lambda: NOW)
    await service.enqueue_incident(warning_incident())
    recovery_event = BotEvent(
        event_type=EventType.RUNTIME_RECOVERED,
        component="supervisor",
        mode="dry_run",
        message="recovered from reconciliation:data_api_timeout",
    )
    notice = await service.enqueue_event(recovery_event)
    assert notice is not None

    due = await repository.due_alerts(now=NOW + timedelta(seconds=1), limit=20)
    fingerprints = [row.incident_fingerprint for row in due]
    assert any(fp.startswith("event:runtime_recovered") for fp in fingerprints)


def test_redact_removes_every_configured_secret() -> None:
    text = (
        "pk=never-send-private-key key=never-send-api-key "
        "tok=never-send-bot-token funder=0xfunder0000000000000000000000000000000000"
    )
    redacted = _redact(text, config())
    for secret in (
        "never-send-private-key",
        "never-send-api-key",
        "never-send-bot-token",
        "0xfunder0000000000000000000000000000000000",
    ):
        assert secret not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.asyncio
async def test_telegram_test_alert_records_success_or_stays_queued(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config(), now=lambda: NOW)
    succeeded = FakeTelegram([None])
    worker = NotificationWorker(repository, succeeded, config(), now=lambda: NOW)

    test_alert = await service.enqueue_test(now=NOW)
    delivered = await worker.deliver_alert_now(test_alert.alert_id)
    assert delivered is True
    stats = await repository.outbox_stats(now=NOW)
    assert stats[0] == 0

    failed_repo = OperationsRepository(tmp_path / "bot2.sqlite3")
    failed_service = AlertService(failed_repo, config(), now=lambda: NOW)
    failing_worker = NotificationWorker(
        failed_repo,
        FakeTelegram([NotificationDeliveryError("ConnectError")]),
        config(),
        now=lambda: NOW,
    )
    failing_alert = await failed_service.enqueue_test(now=NOW)
    not_delivered = await failing_worker.deliver_alert_now(failing_alert.alert_id)
    assert not_delivered is False
    still_queued = await failed_repo.due_alerts(
        now=NOW + timedelta(minutes=30), limit=20
    )
    assert len(still_queued) == 1


@pytest.mark.asyncio
async def test_non_durables_events_are_not_enqueued(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    service = AlertService(repository, config())
    market_event = BotEvent(
        event_type=EventType.MARKET_UPDATE_RECEIVED,
        component="market_data",
        mode="dry_run",
        message="tick",
    )
    assert await service.enqueue_event(market_event) is None
