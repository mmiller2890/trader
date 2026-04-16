"""Market data domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class OrderBookLevel(BaseModel):
    """Single orderbook level."""

    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    size: Decimal = Field(ge=Decimal("0"))


class OrderBookUpdate(BaseModel):
    """Normalized orderbook update from market data transport."""

    model_config = ConfigDict(extra="forbid")

    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    sequence_id: int | None = Field(default=None, ge=0)
    source_ts: datetime = Field(default_factory=utc_now)
    received_ts: datetime = Field(default_factory=utc_now)


class MarketSnapshot(BaseModel):
    """Best-effort market snapshot used by strategy/risk."""

    model_config = ConfigDict(extra="forbid")

    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    best_bid: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    best_ask: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    mid_price: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    top_bid_size: Decimal = Field(ge=Decimal("0"))
    top_ask_size: Decimal = Field(ge=Decimal("0"))
    last_trade_price: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("1"))
    source_ts: datetime = Field(default_factory=utc_now)
    received_ts: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_bid_ask(self) -> "MarketSnapshot":
        if self.best_bid > self.best_ask:
            raise ValueError("best_bid must be <= best_ask")
        return self
