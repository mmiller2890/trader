from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from config.schema import AppConfig
from execution.order_builder import OrderBuilder
from models.market import MarketSnapshot
from models.order import OrderTimeInForce
from models.signal import SignalSide, SignalType, TradeSignal
from models.tick import SUPPORTED_TICK_SIZES


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


def test_post_only_signal_cannot_carry_a_killing_time_in_force() -> None:
    """
    A post-only order rests by definition and FOK/FAK cancel whatever does not
    fill immediately, so the combination is contradictory and the venue refuses
    it. No strategy builds one today -- all three post_only sites leave
    time_in_force unset -- so this pins the invariant before a regression can
    reach the adapter.
    """

    with pytest.raises(ValidationError, match="post_only"):
        TradeSignal(
            signal_id=uuid4().hex,
            strategy_name="test",
            signal_type=SignalType.MAKER_QUOTE,
            side=SignalSide.BUY,
            market_id="m1",
            token_id="t1",
            reference_price=Decimal("0.50"),
            target_price=Decimal("0.49"),
            observed_move_bps=0.0,
            reason="test",
            requested_size=Decimal("5"),
            post_only=True,
            limit_price=Decimal("0.49"),
            time_in_force=OrderTimeInForce.IOC,
        )


def test_our_supported_tick_sizes_match_the_sdk_rounding_table() -> None:
    """
    The SDK indexes ROUNDING_CONFIG by tick size and raises KeyError for any
    key it does not carry, so a tick size we accept but it does not is an
    order that dies during creation. This pins the two lists together, and
    fails if an SDK upgrade changes the venue's supported grid.
    """

    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG

    assert {str(tick) for tick in SUPPORTED_TICK_SIZES} == set(ROUNDING_CONFIG)


@pytest.mark.parametrize("tick", [str(tick) for tick in SUPPORTED_TICK_SIZES])
@pytest.mark.parametrize(
    "best_bid",
    ["0.0001", "0.001", "0.01", "0.10", "0.50", "0.90", "0.99", "0.9999"],
)
def test_every_built_price_satisfies_the_venue_price_rule(
    tick: str, best_bid: str
) -> None:
    """
    Cross-check the order builder against the venue's own validator instead of
    against our reading of it.

    ``py_clob_client_v2.create_order`` refuses any price outside
    [tick, 1 - tick] with a PolyException before signing, and rejects any price
    off the tick grid. Prices built from raw best_bid/best_ask without snapping
    to that grid are the identified cause of the 86 HTTP 400s on 2026-08-24, so
    this asserts the property directly, at the extremes of the book where the
    clamp actually has to do something.
    """

    from py_clob_client_v2.utilities import price_valid

    tick_size = Decimal(tick)
    # Keep the book representable for this tick: a one-tick spread has to fit
    # inside [tick, 1 - tick], which pins the widest usable bid at 1 - 2*tick.
    bid = min(max(Decimal(best_bid), tick_size), Decimal("1") - 2 * tick_size)
    ask = bid + tick_size
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=bid,
        best_ask=ask,
        mid_price=(bid + ask) / 2,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )
    builder = OrderBuilder(AppConfig(), tick_size_provider=lambda _: tick_size)

    for side in (SignalSide.BUY, SignalSide.SELL):
        signal = TradeSignal(
            signal_id=uuid4().hex,
            strategy_name="spike",
            signal_type=(
                SignalType.PRICE_SPIKE
                if side == SignalSide.BUY
                else SignalType.POSITION_EXIT
            ),
            side=side,
            market_id="m1",
            token_id="t1",
            reference_price=Decimal("0.50"),
            target_price=Decimal("0.50"),
            observed_move_bps=100,
            reason="test",
            reduce_only=side == SignalSide.SELL,
        )
        order = builder.build(signal=signal, snapshot=snapshot)

        assert price_valid(float(order.price), tick), (
            f"{side.value} price {order.price} outside [{tick}, {1 - tick_size}]"
        )
        assert order.price % tick_size == 0, (
            f"{side.value} price {order.price} is off the {tick} grid"
        )
        # The venue rounds every size to two decimals regardless of tick size.
        assert order.size == order.size.quantize(Decimal("0.01"))


@pytest.mark.parametrize(
    ("best_bid", "best_ask", "side"),
    [
        # Aggressive BUY rounds the ask UP; off-grid near the ceiling that
        # lands on 1.00, which pays out and which the venue refuses.
        ("0.9950", "0.9990", SignalSide.BUY),
        # Aggressive SELL rounds the bid DOWN; off-grid near the floor that
        # lands on 0.00.
        ("0.0050", "0.0090", SignalSide.SELL),
    ],
)
def test_prices_at_the_payout_bounds_are_clamped_into_venue_range(
    best_bid: str, best_ask: str, side: SignalSide
) -> None:
    """
    Tick snapping alone can push a price past the payout bounds.

    Rounding a marketable order toward the market is what keeps it fillable,
    but at the edge of the book that rounding lands on 1.00 or 0.00 -- prices
    the venue refuses, since ``price_valid`` requires [tick, 1 - tick]. The
    clamp in quantize_price is what stops it, and only an off-grid book at the
    boundary exercises it.
    """

    from py_clob_client_v2.utilities import price_valid

    tick_size = Decimal("0.01")
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal(best_bid),
        best_ask=Decimal(best_ask),
        mid_price=(Decimal(best_bid) + Decimal(best_ask)) / 2,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )
    builder = OrderBuilder(AppConfig(), tick_size_provider=lambda _: tick_size)
    signal = TradeSignal(
        signal_id=uuid4().hex,
        strategy_name="spike",
        signal_type=(
            SignalType.PRICE_SPIKE
            if side == SignalSide.BUY
            else SignalType.POSITION_EXIT
        ),
        side=side,
        market_id="m1",
        token_id="t1",
        reference_price=Decimal("0.50"),
        target_price=Decimal("0.50"),
        observed_move_bps=100,
        reason="test",
        reduce_only=side == SignalSide.SELL,
    )

    order = builder.build(signal=signal, snapshot=snapshot)

    assert price_valid(float(order.price), "0.01"), (
        f"{side.value} price {order.price} is outside the venue's [0.01, 0.99]"
    )
    assert Decimal("0.01") <= order.price <= Decimal("0.99")


def _live_config(**execution: object) -> AppConfig:
    base = {
        "allow_live_trading": True,
        "dry_run_force": False,
        "default_order_size": "5",
        "min_order_size": "1",
        "max_order_size": "25",
        "max_live_order_notional": "2",
        "min_live_buy_notional": "1",
    }
    base.update(execution)
    return AppConfig(bot={"mode": "live"}, execution=base)


def test_live_order_refuses_locally_when_venue_minimum_is_unaffordable() -> None:
    """
    The 2026-08-26 live session was rejected with
    `order is invalid. size (3.22) lower than the minimum: 5`.

    max_live_order_notional was 2, so at 0.62 the builder could afford 3.22
    shares against a venue floor of 5. It knew only its own min_order_size of
    1, so it built the order and let the venue refuse it. Refusing here names
    the real constraint and costs no round trip.
    """

    builder = OrderBuilder(
        _live_config(),
        tick_size_provider=lambda _: Decimal("0.01"),
        min_size_provider=lambda _: Decimal("5"),
    )
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.61"),
        best_ask=Decimal("0.62"),
        mid_price=Decimal("0.615"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )

    with pytest.raises(ValueError, match="venue minimum order size"):
        builder.build(signal=_buy_signal(), snapshot=snapshot)


def test_live_order_size_is_raised_to_the_venue_minimum() -> None:
    """With enough notional headroom, the venue floor sets the size."""

    builder = OrderBuilder(
        _live_config(max_live_order_notional="5"),
        tick_size_provider=lambda _: Decimal("0.01"),
        min_size_provider=lambda _: Decimal("5"),
    )
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.61"),
        best_ask=Decimal("0.62"),
        mid_price=Decimal("0.615"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )

    order = builder.build(signal=_buy_signal(), snapshot=snapshot)

    assert order.size >= Decimal("5")
    assert order.price * order.size <= Decimal("5")


def test_maker_quote_respects_the_venue_minimum_order_size() -> None:
    """
    Maker quotes go to the same venue and hit the same floor. Sizing them by
    the configured minimum alone would rest a quote the venue refuses.
    """

    builder = OrderBuilder(
        _live_config(max_live_order_notional="2"),
        tick_size_provider=lambda _: Decimal("0.01"),
        min_size_provider=lambda _: Decimal("5"),
    )
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.61"),
        best_ask=Decimal("0.62"),
        mid_price=Decimal("0.615"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
    )
    signal = TradeSignal(
        signal_id=uuid4().hex,
        strategy_name="mm",
        signal_type=SignalType.MAKER_QUOTE,
        side=SignalSide.BUY,
        market_id="m1",
        token_id="t1",
        reference_price=Decimal("0.61"),
        target_price=Decimal("0.61"),
        observed_move_bps=0.0,
        reason="test",
        requested_size=Decimal("5"),
        post_only=True,
        limit_price=Decimal("0.61"),
    )

    with pytest.raises(ValueError, match="venue minimum order size"):
        builder.build(signal=signal, snapshot=snapshot)
