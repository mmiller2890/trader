"""Historical event, execution, and portfolio contracts for the paper exchange."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from models.market import OrderBookLevel
from models.order import OrderRequest
from models.position import Position


class _HistoricalEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    sequence_id: int = Field(ge=0)
    source_ts: datetime
    received_ts: datetime


class BookSnapshotEvent(_HistoricalEventBase):
    event_type: Literal["book_snapshot"] = "book_snapshot"
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]


class BookDeltaEvent(_HistoricalEventBase):
    event_type: Literal["book_delta"] = "book_delta"
    bid_updates: list[OrderBookLevel] = Field(default_factory=list)
    ask_updates: list[OrderBookLevel] = Field(default_factory=list)


HistoricalBookEvent = Annotated[
    BookSnapshotEvent | BookDeltaEvent,
    Field(discriminator="event_type"),
]


class ExecutionStatus(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    UNFILLED = "unfilled"
    REJECTED = "rejected"


class SimulatedFill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    size: Decimal = Field(gt=Decimal("0"))
    notional: Decimal = Field(gt=Decimal("0"))
    fee: Decimal = Field(ge=Decimal("0"))


class ExecutionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: OrderRequest
    status: ExecutionStatus
    fills: list[SimulatedFill] = Field(default_factory=list)
    requested_size: Decimal = Field(gt=Decimal("0"))
    filled_size: Decimal = Field(ge=Decimal("0"))
    remaining_size: Decimal = Field(ge=Decimal("0"))
    executable_liquidity: Decimal = Field(ge=Decimal("0"))
    average_fill_price: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    total_notional: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    total_fees: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    reason: str = Field(min_length=1)


class PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: datetime
    cash: Decimal
    reserved_cash: Decimal = Field(ge=Decimal("0"))
    available_cash: Decimal
    position_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_pnl: Decimal
    fees_paid: Decimal = Field(ge=Decimal("0"))
    net_pnl: Decimal
    positions: list[Position] = Field(default_factory=list)
