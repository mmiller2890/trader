"""Position and balance domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from models.order import OrderSide


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class Position(BaseModel):
    """Current position state for a market token."""

    model_config = ConfigDict(extra="forbid")

    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    quantity: Decimal = Field(default=Decimal("0"))
    average_entry_price: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("1"))
    mark_price: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    updated_at: datetime = Field(default_factory=utc_now)


class Balance(BaseModel):
    """Simplified cash balance tracking."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(default="USDC", min_length=1)
    available: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    total: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    updated_at: datetime = Field(default_factory=utc_now)


class ExitReason(str, Enum):
    """Why a position exit was triggered."""

    STRATEGY_SIGNAL = "strategy_signal"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    MAX_HOLD = "max_hold"
    MARKET_EXPIRY = "market_expiry"


class FillCheckpoint(BaseModel):
    """Cumulative accounted fill state for one exchange order identity."""

    model_config = ConfigDict(extra="forbid")

    order_key: str = Field(min_length=8)
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    side: OrderSide
    accounted_filled_size: Decimal = Field(ge=0)
    accounted_fill_notional: Decimal = Field(ge=0)
    confirmed_at: datetime = Field(default_factory=utc_now)


class PositionLifecycle(BaseModel):
    """Durable lifecycle metadata for one market token position."""

    model_config = ConfigDict(extra="forbid")

    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    opened_at: datetime
    last_fill_at: datetime
    market_end_at: datetime | None = None
    last_exit_reason: ExitReason | None = None
    pending_exit_client_order_id: str | None = None
    last_exit_attempt_at: datetime | None = None
    exit_attempt_count: int = Field(default=0, ge=0)
    confirmation_deadline: datetime | None = None
    closed_at: datetime | None = None
    closed_exit_price: Decimal | None = Field(default=None, gt=0, le=1)
    closed_realized_pnl: Decimal | None = None


class FillApplication(BaseModel):
    """Result of applying one confirmed fill delta."""

    model_config = ConfigDict(extra="forbid")

    order_key: str
    delta_size: Decimal = Field(ge=0)
    delta_notional: Decimal = Field(ge=0)
    duplicate: bool
    position: Position | None = None


class PositionMergeResult(BaseModel):
    """Outcome of merging remote positions into local confirmed state."""

    model_config = ConfigDict(extra="forbid")

    deferred_keys: list[str] = Field(default_factory=list)
    expired_keys: list[str] = Field(default_factory=list)
