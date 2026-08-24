"""Strategy signal domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.order import OrderTimeInForce


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
    POSITION_EXIT = "position_exit"


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
    requested_size: Decimal | None = Field(default=None, gt=Decimal("0"))
    reduce_only: bool = False
    time_in_force: OrderTimeInForce | None = None

    @model_validator(mode="after")
    def validate_exit_intent(self) -> Self:
        if self.signal_type == SignalType.POSITION_EXIT and not self.reduce_only:
            raise ValueError("position_exit signals require reduce_only=true")
        if self.reduce_only and self.side != SignalSide.SELL:
            raise ValueError("reduce_only signals require side=sell")
        return self
