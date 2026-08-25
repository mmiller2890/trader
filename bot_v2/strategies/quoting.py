"""Shared vocabulary for strategies that maintain resting quotes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from models.market import MarketSnapshot
from models.order import CancelIntent, OrderSide
from models.signal import TradeSignal


@dataclass(frozen=True)
class RestingQuote:
    """One live quote this process believes is on the book."""

    client_order_id: str
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size: Decimal
    placed_at: datetime
    exchange_order_id: str | None = None

    def with_exchange_id(self, exchange_order_id: str) -> "RestingQuote":
        """Return a copy carrying the exchange identifier needed to cancel."""

        return RestingQuote(
            client_order_id=self.client_order_id,
            market_id=self.market_id,
            token_id=self.token_id,
            side=self.side,
            price=self.price,
            size=self.size,
            placed_at=self.placed_at,
            exchange_order_id=exchange_order_id,
        )


@dataclass
class QuotePlan:
    """
    One quoting decision: what to pull, then what to post.

    Cancels are always applied before quotes so a replacement never doubles
    exposure against its own stale order.
    """

    cancels: list[CancelIntent] = field(default_factory=list)
    quotes: list[TradeSignal] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        """True when there is nothing for the router to do."""

        return not self.cancels and not self.quotes

    def extend(self, other: "QuotePlan") -> None:
        """Merge another plan into this one, preserving cancel-before-quote."""

        self.cancels.extend(other.cancels)
        self.quotes.extend(other.quotes)


@runtime_checkable
class QuotingStrategy(Protocol):
    """A strategy whose output is a set of resting quotes, not single trades."""

    async def plan_quotes(
        self,
        snapshot: MarketSnapshot,
        *,
        market_end_at: datetime | None = None,
    ) -> QuotePlan:
        """Return the cancel/replace plan implied by a new book state."""

    async def plan_maintenance(self) -> QuotePlan:
        """Return the plan implied by elapsed time alone."""

    async def plan_withdrawal(self, reason: str) -> QuotePlan:
        """Return a plan that pulls every resting quote."""
