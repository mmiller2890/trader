"""Internal bot event models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def event_id() -> str:
    """Generate unique event id."""

    return uuid4().hex


class EventType(str, Enum):
    """Core event types for journaling and notifications."""

    MARKET_UPDATE_RECEIVED = "market_update_received"
    SIGNAL_GENERATED = "signal_generated"
    RISK_DECISION = "risk_decision"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_RESULT = "order_result"
    KILL_SWITCH_TRIPPED = "kill_switch_tripped"
    BOT_STARTED = "bot_started"
    REPEATED_FAILURES = "repeated_failures"
    POSITION_UPDATED = "position_updated"
    EXIT_TRIGGERED = "exit_triggered"
    POSITION_CLOSED = "position_closed"
    POSITION_DUST = "position_dust"
    POSITION_CONFIRMATION_DEFERRED = "position_confirmation_deferred"
    RUNTIME_DEGRADED = "runtime_degraded"
    RUNTIME_RECOVERED = "runtime_recovered"
    RUNTIME_FAILED = "runtime_failed"
    LIVE_LEASE_ISSUED = "live_lease_issued"
    LIVE_LEASE_EXPIRING = "live_lease_expiring"
    LIVE_LEASE_EXPIRED = "live_lease_expired"
    AUTO_RESUME_REJECTED = "auto_resume_rejected"
    DAILY_SUMMARY = "daily_summary"
    QUOTE_PLACED = "quote_placed"
    QUOTE_CANCELLED = "quote_cancelled"
    QUOTE_CANCEL_FAILED = "quote_cancel_failed"


class BotEvent(BaseModel):
    """Typed internal event payload."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=event_id, min_length=8)
    event_type: EventType
    component: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    message: str = Field(min_length=1)
    market_id: str | None = None
    token_id: str | None = None
    strategy_name: str | None = None
    signal_id: str | None = None
    client_order_id: str | None = None
    reason: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    quantity: Decimal | None = None
    price: Decimal | None = None
    pnl: Decimal | None = None
    created_at: datetime = Field(default_factory=utc_now)
