"""Deterministic historical order-book reconstruction and paper execution."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from backtest.models import (
    BookDeltaEvent,
    BookSnapshotEvent,
    ExecutionReport,
    ExecutionStatus,
    SimulatedFill,
)
from models.fees import taker_fee
from models.market import MarketSnapshot
from models.order import OrderRequest, OrderSide, OrderTimeInForce


class OrderBookState:
    """Reconstructed book for one ``(market_id, token_id)`` pair."""

    def __init__(self, market_id: str, token_id: str, *, reject_sequence_gaps: bool = True) -> None:
        self.market_id = market_id
        self.token_id = token_id
        self.reject_sequence_gaps = reject_sequence_gaps
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.sequence_id: int | None = None
        self.source_ts: datetime | None = None
        self.received_ts: datetime | None = None

    def apply_snapshot(self, event: BookSnapshotEvent) -> None:
        self._validate_event_ids(event.market_id, event.token_id)
        if self.sequence_id is not None and event.sequence_id <= self.sequence_id:
            raise ValueError(
                f"sequence out of order: {event.sequence_id} <= {self.sequence_id}"
            )
        candidate_bids = {level.price: level.size for level in event.bids if level.size > 0}
        candidate_asks = {level.price: level.size for level in event.asks if level.size > 0}
        self._validate_not_crossed(candidate_bids, candidate_asks)
        self.bids = candidate_bids
        self.asks = candidate_asks
        self.sequence_id = event.sequence_id
        self.source_ts = event.source_ts
        self.received_ts = event.received_ts

    def apply_delta(self, event: BookDeltaEvent) -> None:
        self._validate_event_ids(event.market_id, event.token_id)
        if self.sequence_id is not None and event.sequence_id <= self.sequence_id:
            raise ValueError(
                f"sequence out of order: {event.sequence_id} <= {self.sequence_id}"
            )
        if self.sequence_id is not None and self.reject_sequence_gaps:
            if event.sequence_id != self.sequence_id + 1:
                raise ValueError(
                    f"sequence gap: expected {self.sequence_id + 1}, got {event.sequence_id}"
                )
        candidate_bids = dict(self.bids)
        candidate_asks = dict(self.asks)
        for level in event.bid_updates:
            if level.size > 0:
                candidate_bids[level.price] = level.size
            else:
                candidate_bids.pop(level.price, None)
        for level in event.ask_updates:
            if level.size > 0:
                candidate_asks[level.price] = level.size
            else:
                candidate_asks.pop(level.price, None)
        self._validate_not_crossed(candidate_bids, candidate_asks)
        self.bids = candidate_bids
        self.asks = candidate_asks
        self.sequence_id = event.sequence_id
        self.source_ts = event.source_ts
        self.received_ts = event.received_ts

    def to_market_snapshot(self) -> MarketSnapshot | None:
        if not self.bids or not self.asks:
            return None
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        return MarketSnapshot(
            market_id=self.market_id,
            token_id=self.token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=(best_bid + best_ask) / Decimal("2"),
            top_bid_size=self.bids[best_bid],
            top_ask_size=self.asks[best_ask],
            source_ts=self.source_ts or datetime.fromtimestamp(0),
            received_ts=self.received_ts or datetime.fromtimestamp(0),
        )

    def quote(
        self,
        order: OrderRequest,
        *,
        max_slippage_bps: Decimal,
        fee_rate: Decimal,
    ) -> ExecutionReport:
        """Quote depth without mutating the book."""
        slippage = max_slippage_bps / Decimal("10000")
        if order.side == OrderSide.BUY:
            levels = sorted(self.asks.items())
            limit = min(Decimal("1"), order.price * (Decimal("1") + slippage))
            eligible = ((price, size) for price, size in levels if price <= limit)
        else:
            levels = sorted(self.bids.items(), reverse=True)
            limit = max(Decimal("0"), order.price * (Decimal("1") - slippage))
            eligible = ((price, size) for price, size in levels if price >= limit)

        eligible_levels = list(eligible)
        executable_liquidity = sum(
            (size for _price, size in eligible_levels),
            start=Decimal("0"),
        )

        fills: list[SimulatedFill] = []
        remaining = order.size
        for price, size in eligible_levels:
            if remaining <= 0:
                break
            take = min(remaining, size)
            notional = price * take
            fee = taker_fee(take, price, fee_rate)
            fills.append(
                SimulatedFill(price=price, size=take, notional=notional, fee=fee)
            )
            remaining -= take

        filled_size = order.size - remaining
        if order.time_in_force == OrderTimeInForce.FOK and filled_size < order.size:
            fills = []
            filled_size = Decimal("0")
            remaining = order.size

        if filled_size == 0:
            status = ExecutionStatus.UNFILLED
        elif filled_size == order.size:
            status = ExecutionStatus.FILLED
        else:
            status = ExecutionStatus.PARTIAL

        notional_sum = sum((fill.notional for fill in fills), start=Decimal("0"))
        fee_sum = sum((fill.fee for fill in fills), start=Decimal("0"))
        average_price = (
            notional_sum / filled_size if filled_size > 0 else None
        )
        return ExecutionReport(
            order=order,
            status=status,
            fills=fills,
            requested_size=order.size,
            filled_size=filled_size,
            remaining_size=remaining,
            executable_liquidity=executable_liquidity,
            average_fill_price=average_price,
            total_notional=notional_sum,
            total_fees=fee_sum,
            reason=status.value,
        )

    def commit(self, report: ExecutionReport) -> None:
        if (
            report.order.market_id != self.market_id
            or report.order.token_id != self.token_id
        ):
            raise ValueError("execution report targets a different book")
        live_levels = self.asks if report.order.side == OrderSide.BUY else self.bids
        candidate_levels = dict(live_levels)
        for fill in report.fills:
            current = candidate_levels.get(fill.price)
            if current is None or current < fill.size:
                raise ValueError(
                    f"report consumes unavailable depth at {fill.price}"
                )
            remaining = current - fill.size
            if remaining > 0:
                candidate_levels[fill.price] = remaining
            else:
                candidate_levels.pop(fill.price)
        if report.order.side == OrderSide.BUY:
            self.asks = candidate_levels
        else:
            self.bids = candidate_levels

    def _validate_event_ids(self, market_id: str, token_id: str) -> None:
        if market_id != self.market_id or token_id != self.token_id:
            raise ValueError(
                f"event for ({market_id}, {token_id}) applied to ({self.market_id}, {self.token_id})"
            )

    def _validate_not_crossed(
        self,
        candidate_bids: dict[Decimal, Decimal],
        candidate_asks: dict[Decimal, Decimal],
    ) -> None:
        if candidate_bids and candidate_asks:
            if max(candidate_bids) > min(candidate_asks):
                raise ValueError("crossed book: best bid exceeds best ask")
