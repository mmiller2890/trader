"""Durable Telegram alert pipeline: outbox service, transport, and worker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx

from config.schema import AppConfig, ReliabilityConfig
from models.events import BotEvent, EventType
from models.operations import (
    IncidentSeverity,
    OperationalIncident,
    OutboxAlert,
)
from persistence.operations import OperationsRepository
from reliability.backoff import BackoffSchedule


logger = logging.getLogger(__name__)


class NotificationDeliveryError(RuntimeError):
    """Raised when Telegram delivery fails; never carries raw upstream text."""

    def __init__(self, error_type: str) -> None:
        super().__init__(f"telegram_delivery_failed:{error_type}")


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _sanitize(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ")[:512]


def _redact(text: str, config: AppConfig) -> str:
    redactions: list[str] = []
    secrets = config.secrets
    for secret in (
        secrets.private_key,
        secrets.clob_api_key,
        secrets.clob_secret,
        secrets.clob_passphrase,
        secrets.telegram_bot_token,
    ):
        if secret is not None:
            value = (
                secret.get_secret_value()
                if hasattr(secret, "get_secret_value")
                else str(secret)
            )
            if value:
                redactions.append(value)
    if secrets.polymarket_proxy_address:
        redactions.append(secrets.polymarket_proxy_address)
    if secrets.rpc_url:
        redactions.append(secrets.rpc_url)
    redacted = text
    for secret in redactions:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


_DURABLE_EVENT_TYPES = {
    EventType.KILL_SWITCH_TRIPPED,
    EventType.REPEATED_FAILURES,
    EventType.RUNTIME_DEGRADED,
    EventType.RUNTIME_RECOVERED,
    EventType.RUNTIME_FAILED,
    EventType.LIVE_LEASE_ISSUED,
    EventType.LIVE_LEASE_EXPIRING,
    EventType.LIVE_LEASE_EXPIRED,
    EventType.AUTO_RESUME_REJECTED,
    EventType.DAILY_SUMMARY,
}

_URGENT_EVENT_TYPES = {
    EventType.KILL_SWITCH_TRIPPED,
    EventType.REPEATED_FAILURES,
    EventType.RUNTIME_FAILED,
}


class AlertService:
    """Durably queues alerts before any delivery is attempted."""

    def __init__(
        self,
        repository: OperationsRepository,
        config: AppConfig,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._config = config
        self._now = now

    async def enqueue_incident(self, incident: OperationalIncident) -> OutboxAlert:
        text = _sanitize(
            f"[{incident.severity.value.upper()}] {incident.component}: {incident.reason}"
            + (f" market={incident.market_id}" if incident.market_id else "")
            + (f" token={incident.token_id}" if incident.token_id else "")
        )
        text = _redact(text, self._config)
        now = self._now()
        return await self._repository.enqueue_alert(
            OutboxAlert(
                alert_id=f"alert-{incident.incident_id}",
                incident_fingerprint=incident.fingerprint,
                severity=incident.severity,
                text=text,
                created_at=now,
                next_attempt_at=now,
            ),
            dedupe_after=(
                now
                - timedelta(
                    seconds=self._config.notifications.telegram_deduplication_seconds
                )
            ),
        )

    async def enqueue_event(self, event: BotEvent) -> OutboxAlert | None:
        large_simulated = "large_order_simulated" in (event.reason or "")
        if event.event_type not in _DURABLE_EVENT_TYPES and not large_simulated:
            return None
        now = self._now()
        severity = (
            IncidentSeverity.URGENT
            if event.event_type in _URGENT_EVENT_TYPES
            else IncidentSeverity.INFO
        )
        fingerprint = f"event:{event.event_type.value}:{event.component}"
        if event.event_type == EventType.DAILY_SUMMARY and event.reason:
            fingerprint = event.reason.strip()
        raw = event.message + (f" reason={event.reason}" if event.reason else "")
        text = _redact(_sanitize(raw), self._config)
        return await self._repository.enqueue_alert(
            OutboxAlert(
                alert_id=f"alert-event-{event.event_id}",
                incident_fingerprint=fingerprint,
                severity=severity,
                text=text,
                created_at=event.created_at,
                next_attempt_at=now,
            ),
            dedupe_after=(
                now
                - timedelta(
                    seconds=self._config.notifications.telegram_deduplication_seconds
                )
            ),
        )

    async def enqueue_test(self, *, now: datetime) -> OutboxAlert:
        return await self._repository.enqueue_alert(
            OutboxAlert(
                alert_id="alert-telegram-test",
                incident_fingerprint="telegram:test",
                severity=IncidentSeverity.INFO,
                text="Telegram test alert. If you can read this, delivery works.",
                created_at=now,
                next_attempt_at=now,
            ),
            dedupe_after=now - timedelta(seconds=1),
        )


class TelegramTransport:
    """Owns the long-lived HTTP client used to deliver alerts."""

    def __init__(
        self, config: AppConfig, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def send(self, alert: OutboxAlert) -> None:
        token = self._config.secrets.telegram_bot_token
        chat_id = self._config.secrets.telegram_chat_id
        if token is None or chat_id is None:
            raise NotificationDeliveryError("telegram_not_configured")
        token_value = (
            token.get_secret_value() if hasattr(token, "get_secret_value") else str(token)
        )
        chat_value = (
            chat_id.get_secret_value()
            if hasattr(chat_id, "get_secret_value")
            else str(chat_id)
        )
        url = f"https://api.telegram.org/bot{token_value}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json={"chat_id": chat_value, "text": alert.text},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotificationDeliveryError(type(exc).__name__) from exc

    async def close(self) -> None:
        await self._client.aclose()


class NotificationWorker:
    """Delivers due outbox rows with capped backoff and durable reschedules."""

    def __init__(
        self,
        repository: OperationsRepository,
        transport: TelegramTransport,
        config: AppConfig,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._transport = transport
        self._config = config
        self._now = now
        notifications = config.notifications
        self._initial_seconds = notifications.alert_retry_initial_seconds
        self._max_seconds = notifications.alert_retry_max_seconds

    def _retry_delay(self, attempt_count: int) -> float:
        attempt = attempt_count + 1
        base = min(self._max_seconds, self._initial_seconds * (2 ** max(0, attempt - 1)))
        return min(self._max_seconds, base)

    async def deliver_due_once(self) -> int:
        due = await self._repository.due_alerts(now=self._now(), limit=20)
        delivered = 0
        for alert_row in due:
            if await self.deliver_alert_now(alert_row.alert_id):
                delivered += 1
        return delivered

    async def deliver_alert_now(self, alert_id: str) -> bool:
        due = await self._repository.due_alerts(now=self._now(), limit=100)
        target = next((row for row in due if row.alert_id == alert_id), None)
        if target is None:
            return False
        try:
            await self._transport.send(target)
        except NotificationDeliveryError as exc:
            delay = self._retry_delay(target.attempt_count)
            await self._repository.reschedule_alert(
                alert_id,
                next_attempt_at=self._now() + timedelta(seconds=delay),
                error=str(exc),
            )
            logger.warning(
                "alert delivery failed",
                extra={
                    "component": "notification_worker",
                    "event_type": "alert_delivery_failed",
                    "reason": str(exc),
                },
            )
            return False
        await self._repository.mark_alert_delivered(alert_id, delivered_at=self._now())
        return True

    async def run(
        self,
        stop_event: asyncio.Event,
        heartbeat: Callable[[], Awaitable[None]],
    ) -> None:
        while not stop_event.is_set():
            try:
                await self.deliver_due_once()
            except Exception as exc:
                logger.warning(
                    "notification cycle failed",
                    extra={
                        "component": "notification_worker",
                        "event_type": "cycle_failed",
                        "reason": type(exc).__name__,
                    },
                )
            await heartbeat()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass
