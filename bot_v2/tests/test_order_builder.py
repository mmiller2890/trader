from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from config.schema import AppConfig
from execution.order_builder import OrderBuilder
from models.market import MarketSnapshot
from models.order import OrderTimeInForce
from models.signal import SignalSide, SignalType, TradeSignal


def test_order_builder_creates_deterministic_request() -> None:
    builder = OrderBuilder(AppConfig())
    now = datetime.now(tz=UTC)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.44"),
        best_ask=Decimal("0.45"),
        mid_price=Decimal("0.445"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        received_ts=now,
        source_ts=now,
    )
    signal = TradeSignal(
        signal_id="signal12345678",
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.45"),
        observed_move_bps=100,
        reason="test",
    )

    order = builder.build(signal=signal, snapshot=snapshot)

    assert order.client_order_id == "pm-bot-signal12345678"
    assert order.price == Decimal("0.45")
    assert order.size == Decimal("5")
    assert order.side.value == "buy"
    assert order.time_in_force == OrderTimeInForce.GTC


def test_live_order_size_is_reduced_to_notional_cap() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "5",
            "max_live_order_notional": "1",
            "time_in_force": "FOK",
        },
    )
    builder = OrderBuilder(config)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.50"),
        mid_price=Decimal("0.495"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )

    order = builder.build(signal=_buy_signal(), snapshot=snapshot)

    assert order.size == Decimal("2.000000")
    assert order.price * order.size == Decimal("1.00000000")


def test_live_buy_size_is_raised_to_exchange_minimum_notional() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "1",
            "max_live_order_notional": "1.01",
            "min_live_buy_notional": "1",
            "time_in_force": "FOK",
        },
    )
    builder = OrderBuilder(config)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.17"),
        best_ask=Decimal("0.18"),
        mid_price=Decimal("0.175"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )

    order = builder.build(signal=_buy_signal(), snapshot=snapshot)

    assert order.size == Decimal("5.56")
    assert order.price * order.size >= Decimal("1")
    assert order.price * order.size <= Decimal("1.01")


def test_live_buy_is_rejected_when_exchange_minimum_exceeds_cap() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "1",
            "max_live_order_notional": "1",
            "min_live_buy_notional": "1",
            "time_in_force": "FOK",
        },
    )
    builder = OrderBuilder(config)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.17"),
        best_ask=Decimal("0.18"),
        mid_price=Decimal("0.175"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )

    with pytest.raises(ValueError, match="minimum live BUY notional"):
        builder.build(signal=_buy_signal(), snapshot=snapshot)


def test_live_fok_size_does_not_exceed_visible_top_level() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "5",
            "min_order_size": "0.1",
            "max_live_order_notional": "10",
            "time_in_force": "FOK",
        },
    )
    builder = OrderBuilder(config)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.50"),
        mid_price=Decimal("0.495"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("2.25"),
    )

    order = builder.build(signal=_buy_signal(), snapshot=snapshot)

    assert order.size == Decimal("2.25")


def test_live_order_is_rejected_when_cap_cannot_fund_minimum_size() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "1",
            "min_order_size": "1",
            "min_live_buy_notional": "0.5",
            "max_live_order_notional": "0.5",
            "time_in_force": "FOK",
        },
    )
    builder = OrderBuilder(config)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.89"),
        best_ask=Decimal("0.90"),
        mid_price=Decimal("0.895"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )

    with pytest.raises(ValueError, match="minimum live BUY notional"):
        builder.build(signal=_buy_signal(), snapshot=snapshot)


def test_exit_signal_overrides_entry_fok_with_ioc() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "5",
            "max_live_order_notional": "10",
            "time_in_force": "FOK",
        },
    )
    builder = OrderBuilder(config)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.50"),
        mid_price=Decimal("0.495"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )
    signal = TradeSignal(
        signal_id="signal12345678",
        strategy_name="position_exit",
        signal_type=SignalType.POSITION_EXIT,
        market_id="m1",
        token_id="t1",
        side=SignalSide.SELL,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.50"),
        observed_move_bps=100,
        reason="take_profit",
        requested_size=Decimal("2"),
        reduce_only=True,
        time_in_force=OrderTimeInForce.IOC,
    )

    order = builder.build(signal=signal, snapshot=snapshot)

    assert order.time_in_force == OrderTimeInForce.IOC
    assert order.size == Decimal("2")


def _buy_signal() -> TradeSignal:
    return TradeSignal(
        signal_id="signal12345678",
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.50"),
        observed_move_bps=100,
        reason="test",
    )
