"""Internal bot event models."""

from __future__ import annotations

from datetime import UTC, datetime
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
    created_at: datetime = Field(default_factory=utc_now)
