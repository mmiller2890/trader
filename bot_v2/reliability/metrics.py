"""Restart-safe operational metrics and the daily Telegram summary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from models.events import BotEvent, EventType
from notifications.outbox import AlertService
from persistence.operations import OperationsRepository
from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class DailyOperationalSummary(BaseModel):
    """One UTC day of restart-safe operational counters and state."""

    model_config = ConfigDict(extra="forbid")

    day: str
    uptime_seconds: float = 0.0
    state: str = "unknown"
    markets_rotated: int = Field(default=0, ge=0)
    orders_submitted: int = Field(default=0, ge=0)
    fills_accounted: int = Field(default=0, ge=0)
    orders_rejected: int = Field(default=0, ge=0)
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    recoveries: int = Field(default=0, ge=0)
    degraded_seconds: float = Field(default=0.0, ge=0)
    pending_alerts: int = Field(default=0, ge=0)
    disk_percent: float | None = None
    lease_remaining_seconds: float | None = None


PnlProvider = Callable[[], Awaitable[tuple[Decimal | None, Decimal | None]]]
StateProvider = Callable[[], Awaitable[str]]
OutboxPendingProvider = Callable[[], Awaitable[int]]


class OperationalMetrics:
    """Idempotent per-UTC-day counters backed by durable idempotency keys."""

    def __init__(
        self,
        *,
        repository: OperationsRepository,
        now: Callable[[], datetime] = utc_now,
        pnl_provider: PnlProvider | None = None,
        state_provider: StateProvider | None = None,
        outbox_pending_provider: OutboxPendingProvider | None = None,
        disk_percent_provider: Callable[[], float] | None = None,
        lease_remaining_seconds_provider: Callable[[], float | None] | None = None,
    ) -> None:
        self._repository = repository
        self._now = now
        self._pnl_provider = pnl_provider
        self._state_provider = state_provider
        self._outbox_pending_provider = outbox_pending_provider
        self._disk_percent_provider = disk_percent_provider
        self._lease_remaining_seconds_provider = lease_remaining_seconds_provider

    async def record_event(self, event: BotEvent) -> None:
        """Record one journal event exactly once by its event id."""

        at = event.created_at.astimezone(UTC)
        counters: dict[str, int] = {}
        started_at: datetime | None = None
        if event.event_type == EventType.ORDER_SUBMITTED:
            counters["orders_submitted"] = 1
        elif event.event_type in (
            EventType.POSITION_UPDATED,
            EventType.POSITION_CLOSED,
        ):
            counters["fills_accounted"] = 1
        elif event.event_type == EventType.ORDER_RESULT:
            reason = (event.reason or "").lower()
            if "reject" in reason or "failed" in reason:
                counters["orders_rejected"] = 1
        elif event.event_type == EventType.BOT_STARTED:
            started_at = at
        if not counters and started_at is None:
            return
        await self._repository.bump_daily_metrics(
            day=at.date().isoformat(),
            key=f"event:{event.event_id}",
            counters=counters or None,
            started_at=started_at,
            at=self._now(),
        )

    async def record_market_rotation(self, market_id: str, *, at: datetime) -> None:
        moment = at.astimezone(UTC)
        await self._repository.bump_daily_metrics(
            day=moment.date().isoformat(),
            key=f"market:{moment.date().isoformat()}:{market_id}",
            counters={"markets_rotated": 1},
            at=self._now(),
        )

    async def record_recovery(
        self, incident_fingerprint: str, degraded_seconds: float, *, at: datetime
    ) -> None:
        moment = at.astimezone(UTC)
        await self._repository.bump_daily_metrics(
            day=moment.date().isoformat(),
            key=f"recovery:{moment.date().isoformat()}:{incident_fingerprint}",
            counters={"recoveries": 1},
            degraded_seconds=max(0.0, float(degraded_seconds)),
            at=self._now(),
        )

    async def summary(self, day: date) -> DailyOperationalSummary:
        row = await self._repository.daily_metrics_row(day.isoformat())
        values = row or {}
        started_at_raw = values.get("started_at")
        uptime_seconds = 0.0
        if started_at_raw:
            parsed = datetime.fromisoformat(started_at_raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            uptime_seconds = max(0.0, (self._now() - parsed).total_seconds())

        state = "unknown"
        if self._state_provider is not None:
            try:
                state = await self._state_provider()
            except Exception:
                state = "unknown"
        pending_alerts = 0
        if self._outbox_pending_provider is not None:
            try:
                pending_alerts = int(await self._outbox_pending_provider())
            except Exception:
                pending_alerts = 0
        disk_percent: float | None = None
        if self._disk_percent_provider is not None:
            try:
                disk_percent = float(self._disk_percent_provider())
            except Exception:
                disk_percent = None
        lease_remaining: float | None = None
        if self._lease_remaining_seconds_provider is not None:
            try:
                lease_remaining = self._lease_remaining_seconds_provider()
            except Exception:
                lease_remaining = None
        realized_pnl: Decimal | None = None
        unrealized_pnl: Decimal | None = None
        if self._pnl_provider is not None:
            try:
                realized_pnl, unrealized_pnl = await self._pnl_provider()
            except Exception:
                realized_pnl, unrealized_pnl = None, None

        return DailyOperationalSummary(
            day=day.isoformat(),
            uptime_seconds=uptime_seconds,
            state=state,
            markets_rotated=int(values.get("markets_rotated") or 0),
            orders_submitted=int(values.get("orders_submitted") or 0),
            fills_accounted=int(values.get("fills_accounted") or 0),
            orders_rejected=int(values.get("orders_rejected") or 0),
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            recoveries=int(values.get("recoveries") or 0),
            degraded_seconds=float(values.get("degraded_seconds") or 0.0),
            pending_alerts=pending_alerts,
            disk_percent=disk_percent,
            lease_remaining_seconds=lease_remaining,
        )


class DailySummaryEmitter:
    """Enqueues one durable daily summary alert per UTC day."""

    def __init__(
        self,
        *,
        metrics: OperationalMetrics,
        alert_service: AlertService,
        repository: OperationsRepository,
        hour_utc: int = 0,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._metrics = metrics
        self._alerts = alert_service
        self._repository = repository
        self._hour_utc = max(0, min(23, int(hour_utc)))
        self._now = now

    async def maybe_emit(self) -> bool:
        now = self._now().astimezone(UTC)
        day = now.date()
        if now.hour < self._hour_utc:
            return False
        key = f"daily_summary:{day.isoformat()}"
        claimed = await self._repository.claim_metric_key(key, at=now)
        if not claimed:
            return False
        summary = await self._metrics.summary(day)
        text = _format_summary(summary)
        await self._alerts.enqueue_event(
            BotEvent(
                event_id=f"daily-summary-{day.isoformat()}",
                event_type=EventType.DAILY_SUMMARY,
                component="metrics",
                mode="operator",
                message=text,
                reason=f"ops:daily-summary:{day.isoformat()}",
                created_at=now,
            )
        )
        return True


def _format_summary(summary: DailyOperationalSummary) -> str:
    parts = [
        f"daily_summary day={summary.day}",
        f"state={summary.state}",
        f"uptime_seconds={summary.uptime_seconds:.0f}",
        f"markets_rotated={summary.markets_rotated}",
        f"orders_submitted={summary.orders_submitted}",
        f"fills_accounted={summary.fills_accounted}",
        f"orders_rejected={summary.orders_rejected}",
    ]
    if summary.realized_pnl is not None:
        parts.append(f"realized_pnl={summary.realized_pnl}")
    if summary.unrealized_pnl is not None:
        parts.append(f"unrealized_pnl={summary.unrealized_pnl}")
    parts.extend(
        [
            f"recoveries={summary.recoveries}",
            f"degraded_seconds={summary.degraded_seconds:.1f}",
            f"pending_alerts={summary.pending_alerts}",
        ]
    )
    if summary.disk_percent is not None:
        parts.append(f"disk_percent={summary.disk_percent:.1f}")
    if summary.lease_remaining_seconds is not None:
        parts.append(f"lease_remaining_seconds={summary.lease_remaining_seconds:.0f}")
    return " ".join(parts)
