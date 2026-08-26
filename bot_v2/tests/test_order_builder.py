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


def _book(
    *,
    bid: str = "0.44",
    ask: str = "0.45",
    bid_size: str = "500",
    ask_size: str = "500",
) -> MarketSnapshot:
    now = datetime.now(tz=UTC)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=(Decimal(bid) + Decimal(ask)) / 2,
        top_bid_size=Decimal(bid_size),
        top_ask_size=Decimal(ask_size),
        received_ts=now,
        source_ts=now,
    )


def _maker_quote(
    *,
    side: SignalSide,
    limit_price: str,
    size: str = "100",
) -> TradeSignal:
    return TradeSignal(
        signal_id="quote123456789",
        strategy_name="market_maker",
        signal_type=SignalType.MAKER_QUOTE,
        market_id="m1",
        token_id="t1",
        side=side,
        reference_price=Decimal("0.445"),
        target_price=Decimal(limit_price),
        observed_move_bps=0,
        reason="quote",
        requested_size=Decimal(size),
        limit_price=Decimal(limit_price),
        post_only=True,
    )


def test_taker_price_is_snapped_onto_the_tick_grid() -> None:
    # A 0.01-tick market cannot accept 0.455; a marketable buy rounds up.
    builder = OrderBuilder(AppConfig())
    order = builder.build(signal=_buy_signal(), snapshot=_book(ask="0.455"))
    assert order.price == Decimal("0.46")
    assert order.tick_size == Decimal("0.01")
    assert order.post_only is False


def test_builder_uses_the_injected_tick_size_provider() -> None:
    builder = OrderBuilder(
        AppConfig(), tick_size_provider=lambda token_id: Decimal("0.001")
    )
    order = builder.build(signal=_buy_signal(), snapshot=_book(ask="0.4555"))
    assert order.tick_size == Decimal("0.001")
    assert order.price == Decimal("0.456")


def test_builder_falls_back_when_the_tick_provider_fails() -> None:
    def broken(token_id: str) -> Decimal:
        raise RuntimeError("lookup down")

    builder = OrderBuilder(AppConfig(), tick_size_provider=broken)
    order = builder.build(signal=_buy_signal(), snapshot=_book(ask="0.45"))
    assert order.tick_size == Decimal("0.01")


def _mm_config() -> AppConfig:
    return AppConfig(
        execution={
            "default_order_size": "100",
            "max_order_size": "500",
            "min_order_size": "1",
        }
    )


def test_maker_quote_rests_at_its_own_limit_price() -> None:
    builder = OrderBuilder(_mm_config())
    order = builder.build(
        signal=_maker_quote(side=SignalSide.BUY, limit_price="0.43"),
        snapshot=_book(),
    )
    assert order.price == Decimal("0.43")
    assert order.size == Decimal("100")
    assert order.post_only is True
    assert order.time_in_force == OrderTimeInForce.GTC


def test_maker_bid_is_clamped_below_the_best_ask() -> None:
    # A post-only buy at or above the ask would be rejected outright.
    builder = OrderBuilder(AppConfig())
    order = builder.build(
        signal=_maker_quote(side=SignalSide.BUY, limit_price="0.48"),
        snapshot=_book(bid="0.44", ask="0.45"),
    )
    assert order.price == Decimal("0.44")


def test_maker_ask_is_clamped_above_the_best_bid() -> None:
    builder = OrderBuilder(AppConfig())
    order = builder.build(
        signal=_maker_quote(side=SignalSide.SELL, limit_price="0.41"),
        snapshot=_book(bid="0.44", ask="0.45"),
    )
    assert order.price == Decimal("0.45")


def test_maker_quote_size_is_capped_by_live_notional() -> None:
    config = AppConfig(
        bot={"mode": "live"},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "100",
            "max_order_size": "500",
            "min_order_size": "1",
            "min_live_buy_notional": "1",
            "max_live_order_notional": "10",
        },
    )
    builder = OrderBuilder(config)
    order = builder.build(
        signal=_maker_quote(side=SignalSide.BUY, limit_price="0.40", size="100"),
        snapshot=_book(),
    )
    # $10 cap at $0.40 funds at most 25 shares.
    assert order.size == Decimal("25")


def test_maker_quote_price_off_grid_is_snapped_passively() -> None:
    builder = OrderBuilder(AppConfig())
    bid_order = builder.build(
        signal=_maker_quote(side=SignalSide.BUY, limit_price="0.4267"),
        snapshot=_book(),
    )
    assert bid_order.price == Decimal("0.42")
    ask_order = builder.build(
        signal=_maker_quote(side=SignalSide.SELL, limit_price="0.4612"),
        snapshot=_book(),
    )
    assert ask_order.price == Decimal("0.47")


def test_post_only_position_exit_rests_at_its_limit_price_not_repriced_as_taker() -> None:
    # A maker-first exit (POSITION_EXIT with post_only + limit_price) must take
    # the maker pricing path, not be repriced onto the crossing taker price.
    builder = OrderBuilder(AppConfig())
    signal = TradeSignal(
        signal_id="signal12345678",
        strategy_name="position_exit",
        signal_type=SignalType.POSITION_EXIT,
        market_id="m1",
        token_id="t1",
        side=SignalSide.SELL,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.45"),
        observed_move_bps=0,
        reason="position_exit:take_profit",
        requested_size=Decimal("2.5"),
        reduce_only=True,
        post_only=True,
        limit_price=Decimal("0.45"),
        time_in_force=OrderTimeInForce.GTC,
    )

    order = builder.build(signal=signal, snapshot=_book(bid="0.44", ask="0.45"))

    assert order.post_only is True
    assert order.price == Decimal("0.45")
    assert order.size == Decimal("2.5")
    assert order.time_in_force == OrderTimeInForce.GTC
