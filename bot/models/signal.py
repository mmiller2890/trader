"""Strategy signal domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def signal_id() -> str:
    """Generate a stable unique signal id."""

    return uuid4().hex


class SignalSide(str, Enum):
    """Trade direction emitted by strategies."""

    BUY = "buy"
    SELL = "sell"


class SignalType(str, Enum):
    """Signal class for bookkeeping and analytics."""

    PRICE_SPIKE = "price_spike"


class TradeSignal(BaseModel):
    """Typed strategy output consumed by risk/execution layers."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(default_factory=signal_id, min_length=8)
    strategy_name: str = Field(min_length=1)
    signal_type: SignalType = SignalType.PRICE_SPIKE
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    side: SignalSide
    reference_price: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    target_price: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    observed_move_bps: float = Field(ge=0.0)
    created_at: datetime = Field(default_factory=utc_now)
    reason: str = Field(min_length=1)
