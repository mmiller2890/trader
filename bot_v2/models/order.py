"""Order intent and order outcome models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class OrderSide(str, Enum):
    """Supported order sides."""

    BUY = "buy"
    SELL = "sell"


class OrderTimeInForce(str, Enum):
    """Supported order time-in-force values."""

    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(str, Enum):
    """Internal order status values."""

    PENDING = "pending"
    SIMULATED = "simulated"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    UNKNOWN = "unknown"


class OrderRequest(BaseModel):
    """Validated order request sent to execution submitter."""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(min_length=8, max_length=64)
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    side: OrderSide
    price: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    size: Decimal = Field(gt=Decimal("0"))
    time_in_force: OrderTimeInForce = OrderTimeInForce.GTC
    post_only: bool = False
    tick_size: Decimal = Field(default=Decimal("0.01"), gt=Decimal("0"), le=Decimal("0.1"))
    signal_id: str | None = None
    strategy_name: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class OrderResult(BaseModel):
    """Result emitted after submit attempt."""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(min_length=8, max_length=64)
    exchange_order_id: str | None = None
    market_id: str | None = None
    token_id: str | None = None
    side: OrderSide | None = None
    status: OrderStatus
    accepted: bool
    message: str | None = None
    signal_id: str | None = None
    strategy_name: str | None = None
    requested_size: Decimal = Field(gt=Decimal("0"))
    filled_size: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    avg_fill_price: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    latency_ms: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class CancelIntent(BaseModel):
    """Request to cancel exactly one resting order."""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(min_length=8, max_length=64)
    exchange_order_id: str | None = None
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    side: OrderSide
    reason: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class CancelOutcome(str, Enum):
    """Result of a single-order cancellation attempt."""

    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    SIMULATED = "simulated"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CancelResult(BaseModel):
    """Typed result of one cancellation attempt."""

    model_config = ConfigDict(extra="forbid")

    client_order_id: str = Field(min_length=8, max_length=64)
    exchange_order_id: str | None = None
    outcome: CancelOutcome
    message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def terminal(self) -> bool:
        """True when the resting order is known to be gone from the book."""

        return self.outcome in {
            CancelOutcome.CANCELLED,
            CancelOutcome.NOT_FOUND,
            CancelOutcome.SIMULATED,
        }
