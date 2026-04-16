"""Position and balance domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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
