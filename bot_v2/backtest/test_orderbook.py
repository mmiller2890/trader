from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.conftest import (
    book_with_asks,
    buy_order,
    delta_event,
    seeded_book,
    snapshot_event,
)
from backtest.models import ExecutionStatus
from backtest.orderbook import OrderBookState


def test_snapshot_replaces_book_and_selects_true_best_levels() -> None:
    book = OrderBookState("m1", "t1")
    book.apply_snapshot(snapshot_event(
        sequence=10,
        bids=[("0.48", "10"), ("0.50", "4"), ("0.49", "8")],
        asks=[("0.54", "9"), ("0.52", "6"), ("0.53", "7")],
    ))
    current = book.to_market_snapshot()
    assert current is not None
    assert current.best_bid == Decimal("0.50")
    assert current.best_ask == Decimal("0.52")
    assert current.top_bid_size == Decimal("4")
    assert current.top_ask_size == Decimal("6")


def test_delta_upserts_and_deletes_levels() -> None:
    book = seeded_book(sequence=10)
    book.apply_delta(delta_event(
        sequence=11,
        bid_updates=[("0.50", "0"), ("0.495", "12")],
        ask_updates=[("0.52", "3")],
    ))
    assert book.bids == {Decimal("0.49"): Decimal("8"), Decimal("0.495"): Decimal("12")}
    assert book.asks[Decimal("0.52")] == Decimal("3")


def test_out_of_order_and_gapped_deltas_are_rejected() -> None:
    book = seeded_book(sequence=10, reject_sequence_gaps=True)
    with pytest.raises(ValueError, match="sequence gap"):
        book.apply_delta(delta_event(sequence=12))
    with pytest.raises(ValueError, match="out of order"):
        book.apply_delta(delta_event(sequence=9))


def test_crossed_book_is_rejected_without_changing_previous_state() -> None:
    book = seeded_book(sequence=10)
    before = (book.bids.copy(), book.asks.copy(), book.sequence_id)
    with pytest.raises(ValueError, match="crossed book"):
        book.apply_delta(delta_event(sequence=11, bid_updates=[("0.60", "1")]))
    assert (book.bids, book.asks, book.sequence_id) == before


def test_buy_quote_walks_asks_and_calculates_vwap_and_fees() -> None:
    book = book_with_asks([("0.50", "2"), ("0.51", "3"), ("0.55", "10")])
    order = buy_order(price="0.50", size="5", tif="IOC")
    report = book.quote(order, max_slippage_bps=Decimal("300"), fee_rate=Decimal("0.07"))
    assert [fill.size for fill in report.fills] == [Decimal("2"), Decimal("3")]
    assert report.total_notional == Decimal("2.53")
    assert report.average_fill_price == Decimal("0.506")
    # fee = shares * 0.07 * price * (1 - price), summed per fill:
    # 2 * 0.07 * 0.50 * 0.50 = 0.0350; 3 * 0.07 * 0.51 * 0.49 = 0.052479
    assert report.total_fees == Decimal("0.087479")
    assert report.executable_liquidity == Decimal("5")
    assert report.status == ExecutionStatus.FILLED


def test_executable_liquidity_excludes_levels_beyond_price_limit() -> None:
    book = book_with_asks([("0.50", "2"), ("0.51", "3"), ("0.55", "10")])
    report = book.quote(
        buy_order(price="0.50", size="1", tif="IOC"),
        max_slippage_bps=Decimal("300"),
        fee_rate=Decimal("0"),
    )
    assert report.executable_liquidity == Decimal("5")


def test_commit_validation_failure_leaves_every_level_unchanged() -> None:
    book = book_with_asks([("0.50", "2"), ("0.51", "2")])
    report = book.quote(
        buy_order(price="0.50", size="4", tif="IOC"),
        max_slippage_bps=Decimal("300"),
        fee_rate=Decimal("0"),
    )
    invalid = report.model_copy(update={
        "fills": [
            report.fills[0].model_copy(update={"size": Decimal("1")}),
            report.fills[1].model_copy(update={"size": Decimal("3")}),
        ]
    })
    before = book.asks.copy()
    with pytest.raises(ValueError, match="unavailable depth"):
        book.commit(invalid)
    assert book.asks == before


def test_partial_quote_does_not_mutate_until_commit() -> None:
    book = book_with_asks([("0.50", "2")])
    report = book.quote(buy_order(price="0.50", size="5", tif="IOC"), max_slippage_bps=Decimal("0"), fee_rate=Decimal("0"))
    assert report.status == ExecutionStatus.PARTIAL
    assert report.filled_size == Decimal("2")
    assert book.asks[Decimal("0.50")] == Decimal("2")
    book.commit(report)
    assert Decimal("0.50") not in book.asks


def test_fok_insufficient_depth_has_no_fills_and_cannot_consume_book() -> None:
    book = book_with_asks([("0.50", "2")])
    report = book.quote(buy_order(price="0.50", size="5", tif="FOK"), max_slippage_bps=Decimal("0"), fee_rate=Decimal("0"))
    assert report.status == ExecutionStatus.UNFILLED
    assert report.fills == []
    assert book.asks[Decimal("0.50")] == Decimal("2")


def test_quote_charges_the_price_dependent_taker_fee() -> None:
    from decimal import Decimal

    from backtest.orderbook import OrderBookState
    from models.order import OrderRequest, OrderSide, OrderTimeInForce

    book = OrderBookState(market_id="m1", token_id="t1")
    book.asks[Decimal("0.50")] = Decimal("100")
    order = OrderRequest(
        client_order_id="fee-test-00001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.50"),
        size=Decimal("100"),
        time_in_force=OrderTimeInForce.GTC,
    )

    report = book.quote(
        order, max_slippage_bps=Decimal("0"), fee_rate=Decimal("0.07")
    )

    # 100 shares * 0.07 * 0.50 * 0.50 = 1.75, not notional * bps.
    assert report.total_fees == Decimal("1.7500")


def test_quote_fee_falls_toward_the_price_extremes() -> None:
    from decimal import Decimal

    from backtest.orderbook import OrderBookState
    from models.order import OrderRequest, OrderSide, OrderTimeInForce

    def fee_at(price: str) -> Decimal:
        book = OrderBookState(market_id="m1", token_id="t1")
        book.asks[Decimal(price)] = Decimal("100")
        order = OrderRequest(
            client_order_id="fee-test-00002",
            market_id="m1",
            token_id="t1",
            side=OrderSide.BUY,
            price=Decimal(price),
            size=Decimal("100"),
            time_in_force=OrderTimeInForce.GTC,
        )
        return book.quote(
            order, max_slippage_bps=Decimal("0"), fee_rate=Decimal("0.07")
        ).total_fees

    assert fee_at("0.90") < fee_at("0.50")
    assert fee_at("0.10") < fee_at("0.50")
