"""Shared fixtures and factories for backtest tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backtest.models import (
    BookDeltaEvent,
    BookSnapshotEvent,
    ExecutionReport,
    ExecutionStatus,
    SimulatedFill,
)
from backtest.orderbook import OrderBookState
from models.market import MarketSnapshot, OrderBookLevel
from models.order import OrderRequest, OrderSide, OrderTimeInForce


NOW = datetime(2025, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)


def levels(values: list[tuple[str, str]]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=Decimal(price), size=Decimal(size)) for price, size in values]


def snapshot_event(
    *,
    sequence: int,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    at: datetime = NOW,
) -> BookSnapshotEvent:
    return BookSnapshotEvent(
        market_id="m1",
        token_id="t1",
        bids=levels([("0.49", "8"), ("0.50", "4")] if bids is None else bids),
        asks=levels([("0.52", "6")] if asks is None else asks),
        sequence_id=sequence,
        source_ts=at,
        received_ts=at,
    )


def delta_event(
    *,
    sequence: int,
    bid_updates: list[tuple[str, str]] | None = None,
    ask_updates: list[tuple[str, str]] | None = None,
    at: datetime = LATER,
) -> BookDeltaEvent:
    return BookDeltaEvent(
        market_id="m1",
        token_id="t1",
        bid_updates=levels(bid_updates or []),
        ask_updates=levels(ask_updates or []),
        sequence_id=sequence,
        source_ts=at,
        received_ts=at,
    )


def seeded_book(*, sequence: int, reject_sequence_gaps: bool = True) -> OrderBookState:
    book = OrderBookState("m1", "t1", reject_sequence_gaps=reject_sequence_gaps)
    book.apply_snapshot(snapshot_event(sequence=sequence))
    return book


def book_with_asks(values: list[tuple[str, str]]) -> OrderBookState:
    book = OrderBookState("m1", "t1")
    book.apply_snapshot(snapshot_event(sequence=1, bids=[("0.49", "100")], asks=values))
    return book


def buy_order(*, price: str, size: str, tif: str = "IOC") -> OrderRequest:
    return OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal(price),
        size=Decimal(size),
        time_in_force=OrderTimeInForce(tif),
        strategy_name="test",
        created_at=NOW,
    )


def market_snapshot(*, mid: str, at: datetime = NOW) -> MarketSnapshot:
    price = Decimal(mid)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=price,
        best_ask=price,
        mid_price=price,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=at,
        received_ts=at,
    )


def fill_report(
    *,
    side: OrderSide,
    requested: str,
    filled: str,
    price: str,
    fee_bps: str = "0",
) -> ExecutionReport:
    requested_size = Decimal(requested)
    filled_size = Decimal(filled)
    fill_price = Decimal(price)
    notional = fill_price * filled_size
    fee = notional * Decimal(fee_bps) / Decimal("10000")
    order = OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=side,
        price=fill_price,
        size=requested_size,
        time_in_force=OrderTimeInForce.IOC,
        strategy_name="test",
        created_at=NOW,
    )
    fills = (
        [SimulatedFill(price=fill_price, size=filled_size, notional=notional, fee=fee)]
        if filled_size > 0
        else []
    )
    status = (
        ExecutionStatus.FILLED
        if filled_size == requested_size
        else ExecutionStatus.PARTIAL
        if filled_size > 0
        else ExecutionStatus.UNFILLED
    )
    return ExecutionReport(
        order=order,
        status=status,
        fills=fills,
        requested_size=requested_size,
        filled_size=filled_size,
        remaining_size=requested_size - filled_size,
        executable_liquidity=filled_size,
        average_fill_price=fill_price if filled_size > 0 else None,
        total_notional=notional,
        total_fees=fee,
        reason=status.value,
    )


def filled_buy(*, size: str, price: str, fee_bps: str = "0") -> ExecutionReport:
    return fill_report(side=OrderSide.BUY, requested=size, filled=size, price=price, fee_bps=fee_bps)


def filled_sell(*, size: str, price: str, fee_bps: str = "0") -> ExecutionReport:
    return fill_report(side=OrderSide.SELL, requested=size, filled=size, price=price, fee_bps=fee_bps)


def partial_buy(*, requested: str, filled: str, price: str) -> ExecutionReport:
    return fill_report(side=OrderSide.BUY, requested=requested, filled=filled, price=price)
