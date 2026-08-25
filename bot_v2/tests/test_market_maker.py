"""Inventory-skewed two-sided quoting behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import MarketMakerConfig
from models.market import MarketSnapshot
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import Position
from models.signal import SignalSide, SignalType
from models.tick import DEFAULT_TICK_SIZE
from strategies.market_maker import MarketMakerStrategy

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


def book(
    *,
    bid: str = "0.49",
    ask: str = "0.51",
    bid_size: str = "500",
    ask_size: str = "500",
    token_id: str = "t1",
) -> MarketSnapshot:
    return MarketSnapshot(
        market_id="m1",
        token_id=token_id,
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=(Decimal(bid) + Decimal(ask)) / 2,
        top_bid_size=Decimal(bid_size),
        top_ask_size=Decimal(ask_size),
        received_ts=NOW,
        source_ts=NOW,
    )


def config(**overrides: object) -> MarketMakerConfig:
    base: dict[str, object] = {
        "enabled": True,
        "quote_spread_ticks": 2,
        "unwind_spread_ticks": 1,
        "max_skew_ticks": "2",
        "base_quote_size": "100",
        "min_quote_size": "5",
        "refresh_move_ticks": "1",
        "inventory_unwind_ratio": 0.8,
        "max_position_size": "200",
        "quote_ttl_seconds": 30.0,
    }
    base.update(overrides)
    return MarketMakerConfig(**base)


def strategy(
    *,
    inventory: str = "0",
    cfg: MarketMakerConfig | None = None,
    clock: dict[str, datetime] | None = None,
) -> MarketMakerStrategy:
    holdings = Decimal(inventory)

    async def read_position(market_id: str, token_id: str) -> Position | None:
        if holdings == 0:
            return None
        return Position(
            market_id=market_id,
            token_id=token_id,
            quantity=holdings,
            average_entry_price=Decimal("0.50"),
            mark_price=Decimal("0.50"),
        )

    ticker = clock or {"now": NOW}
    return MarketMakerStrategy(
        cfg or config(),
        position_reader=read_position,
        tick_size_provider=lambda token_id: DEFAULT_TICK_SIZE,
        now=lambda: ticker["now"],
    )


def by_side(plan) -> dict[SignalSide, object]:
    return {quote.side: quote for quote in plan.quotes}


@pytest.mark.asyncio
async def test_flat_inventory_quotes_a_bid_one_tick_below_the_mid() -> None:
    plan = await strategy().plan_quotes(book())
    quotes = by_side(plan)

    # Mid 0.50, 2-tick spread => one tick either side.
    assert quotes[SignalSide.BUY].limit_price == Decimal("0.49")
    assert quotes[SignalSide.BUY].requested_size == Decimal("100")


@pytest.mark.asyncio
async def test_flat_inventory_emits_no_ask_because_shares_are_not_owned() -> None:
    # Polymarket has no borrow: an ask is only real once the shares exist.
    # Two-sided coverage comes from quoting a bid on each outcome token of the
    # pair -- a bid on NO at (1 - fair) is the economic ask on YES.
    plan = await strategy().plan_quotes(book())
    assert SignalSide.SELL not in by_side(plan)


@pytest.mark.asyncio
async def test_complementary_token_bid_brackets_fair_value() -> None:
    engine = strategy()
    yes_plan = await engine.plan_quotes(book(bid="0.49", ask="0.51"))
    no_plan = await engine.plan_quotes(
        book(bid="0.49", ask="0.51", token_id="t2")
    )

    yes_bid = by_side(yes_plan)[SignalSide.BUY].limit_price
    no_bid = by_side(no_plan)[SignalSide.BUY].limit_price
    # Buying NO at 0.49 is selling YES at 0.51, so the pair straddles 0.50.
    assert yes_bid == Decimal("0.49")
    assert Decimal("1") - no_bid == Decimal("0.51")


@pytest.mark.asyncio
async def test_every_quote_is_a_post_only_maker_signal() -> None:
    plan = await strategy(inventory="50").plan_quotes(book())
    assert plan.quotes
    for quote in plan.quotes:
        assert quote.signal_type == SignalType.MAKER_QUOTE
        assert quote.post_only is True
        assert quote.limit_price is not None
        assert quote.requested_size is not None


@pytest.mark.asyncio
async def test_long_inventory_skews_fair_value_down() -> None:
    # Half the cap long => ratio 0.5 => skew 0.5 * 2 ticks = 1 tick.
    plan = await strategy(inventory="100").plan_quotes(book())
    quotes = by_side(plan)

    assert quotes[SignalSide.BUY].limit_price == Decimal("0.48")
    assert quotes[SignalSide.SELL].limit_price == Decimal("0.50")


@pytest.mark.asyncio
async def test_long_inventory_shrinks_the_bid_and_grows_the_ask() -> None:
    plan = await strategy(inventory="100").plan_quotes(book())
    quotes = by_side(plan)

    # ratio 0.5 => bid scaled by 0.5, ask by 1.5 (bounded by inventory held).
    assert quotes[SignalSide.BUY].requested_size == Decimal("50")
    assert quotes[SignalSide.SELL].requested_size == Decimal("100")


@pytest.mark.asyncio
async def test_ask_size_never_exceeds_inventory_on_hand() -> None:
    plan = await strategy(inventory="30").plan_quotes(book())
    quotes = by_side(plan)
    assert quotes[SignalSide.SELL].requested_size == Decimal("30")


@pytest.mark.asyncio
async def test_bid_size_never_breaches_the_position_cap() -> None:
    plan = await strategy(
        inventory="150", cfg=config(inventory_unwind_ratio=1.0)
    ).plan_quotes(book())
    quotes = by_side(plan)
    # ratio 0.75 already shrinks the bid to 25, inside the 50 of headroom.
    assert quotes[SignalSide.BUY].requested_size == Decimal("25")
    assert quotes[SignalSide.BUY].requested_size <= Decimal("200") - Decimal("150")


@pytest.mark.asyncio
async def test_unwind_stops_quoting_the_accumulating_side() -> None:
    # 170/200 = 0.85 ratio, past the 0.8 unwind threshold.
    plan = await strategy(inventory="170").plan_quotes(book())
    quotes = by_side(plan)

    assert SignalSide.BUY not in quotes
    assert SignalSide.SELL in quotes


@pytest.mark.asyncio
async def test_unwind_tightens_the_reducing_side() -> None:
    normal = by_side(await strategy(inventory="100").plan_quotes(book()))
    unwinding = by_side(await strategy(inventory="170").plan_quotes(book()))

    def fair_for(inventory: str) -> Decimal:
        ratio = Decimal(inventory) / Decimal("200")
        return Decimal("0.50") - ratio * Decimal("2") * DEFAULT_TICK_SIZE

    # Unwinding uses a 1-tick spread instead of 2, so the ask sits closer to
    # its own fair value and is more likely to trade.
    normal_edge = normal[SignalSide.SELL].limit_price - fair_for("100")
    unwind_edge = unwinding[SignalSide.SELL].limit_price - fair_for("170")
    assert unwind_edge < normal_edge


@pytest.mark.asyncio
async def test_short_inventory_mirrors_the_skew() -> None:
    plan = await strategy(inventory="-100").plan_quotes(book())
    quotes = by_side(plan)
    # Negative inventory pulls fair value up, so the bid rises to buy it back.
    assert quotes[SignalSide.BUY].limit_price == Decimal("0.50")
    # There is nothing to sell, so no ask is offered.
    assert SignalSide.SELL not in quotes


@pytest.mark.asyncio
async def test_disabled_strategy_emits_nothing() -> None:
    plan = await strategy(cfg=config(enabled=False)).plan_quotes(book())
    assert plan.empty


@pytest.mark.asyncio
async def test_crossed_or_empty_book_is_not_quoted() -> None:
    engine = strategy()
    assert (await engine.plan_quotes(book(bid="0.50", ask="0.50"))).empty
    assert (await engine.plan_quotes(book(bid="0", ask="0.51"))).empty


@pytest.mark.asyncio
async def test_thin_book_below_minimum_liquidity_is_not_quoted() -> None:
    engine = strategy(cfg=config(min_book_liquidity="100"))
    assert (await engine.plan_quotes(book(bid_size="10", ask_size="10"))).empty


@pytest.mark.asyncio
async def test_targets_restrict_quoting_to_configured_tokens() -> None:
    engine = strategy(cfg=config(target_token_ids=["other"]))
    assert (await engine.plan_quotes(book())).empty


@pytest.mark.asyncio
async def test_quoting_stops_before_market_close() -> None:
    engine = strategy()
    plan = await engine.plan_quotes(
        book(), market_end_at=NOW + timedelta(seconds=30)
    )
    assert plan.quotes == []


def submitted(engine: MarketMakerStrategy, quote, *, client_order_id: str) -> None:
    engine.register_submission(
        client_order_id=client_order_id,
        signal=quote,
        price=quote.limit_price,
        size=quote.requested_size,
    )


def accepted(client_order_id: str, *, exchange_order_id: str = "0xrest1") -> OrderResult:
    return OrderResult(
        client_order_id=client_order_id,
        exchange_order_id=exchange_order_id,
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        status=OrderStatus.SUBMITTED,
        accepted=True,
        requested_size=Decimal("100"),
    )


async def rest_a_bid(engine: MarketMakerStrategy, snapshot=None) -> None:
    plan = await engine.plan_quotes(snapshot or book())
    quote = by_side(plan)[SignalSide.BUY]
    submitted(engine, quote, client_order_id="pm-bot-quote000000001")
    engine.record_order_result(accepted("pm-bot-quote000000001"))


@pytest.mark.asyncio
async def test_a_quiet_book_does_not_churn_the_resting_quote() -> None:
    engine = strategy()
    await rest_a_bid(engine)

    plan = await engine.plan_quotes(book())

    assert plan.empty
    assert len(engine.resting_quotes()) == 1


@pytest.mark.asyncio
async def test_small_move_inside_the_refresh_band_is_ignored() -> None:
    engine = strategy()
    await rest_a_bid(engine)

    # Mid moves one tick; refresh_move_ticks is 1, so this is not "more than".
    plan = await engine.plan_quotes(book(bid="0.50", ask="0.52"))

    assert plan.empty


@pytest.mark.asyncio
async def test_large_move_cancels_before_it_replaces() -> None:
    engine = strategy()
    await rest_a_bid(engine)

    plan = await engine.plan_quotes(book(bid="0.53", ask="0.55"))

    assert len(plan.cancels) == 1
    assert plan.cancels[0].client_order_id == "pm-bot-quote000000001"
    assert plan.cancels[0].exchange_order_id == "0xrest1"
    assert plan.cancels[0].reason == "quote_stale"
    assert len(plan.quotes) == 1
    assert plan.quotes[0].limit_price == Decimal("0.53")


@pytest.mark.asyncio
async def test_replacement_never_leaves_two_quotes_on_one_side() -> None:
    engine = strategy()
    await rest_a_bid(engine)
    plan = await engine.plan_quotes(book(bid="0.53", ask="0.55"))
    submitted(engine, plan.quotes[0], client_order_id="pm-bot-quote000000002")

    bids = [q for q in engine.resting_quotes() if q.side == OrderSide.BUY]
    assert len(bids) == 1
    assert bids[0].client_order_id == "pm-bot-quote000000002"


@pytest.mark.asyncio
async def test_expired_quote_is_pulled_by_the_maintenance_pass() -> None:
    clock = {"now": NOW}
    engine = strategy(clock=clock)
    await rest_a_bid(engine)

    clock["now"] = NOW + timedelta(seconds=31)
    plan = await engine.plan_maintenance()

    assert len(plan.cancels) == 1
    assert plan.cancels[0].reason == "quote_ttl_expired"
    # Planning is pure: the quote stays tracked until the router confirms.
    assert len(engine.resting_quotes()) == 1
    engine.forget_quote(plan.cancels[0].client_order_id)
    assert engine.resting_quotes() == []


@pytest.mark.asyncio
async def test_fresh_quote_survives_the_maintenance_pass() -> None:
    clock = {"now": NOW}
    engine = strategy(clock=clock)
    await rest_a_bid(engine)

    clock["now"] = NOW + timedelta(seconds=5)

    assert (await engine.plan_maintenance()).empty


@pytest.mark.asyncio
async def test_withdrawal_pulls_every_resting_quote() -> None:
    engine = strategy(inventory="50")
    plan = await engine.plan_quotes(book())
    for index, quote in enumerate(plan.quotes):
        submitted(engine, quote, client_order_id=f"pm-bot-quote00000000{index}")

    withdrawal = await engine.plan_withdrawal("halting")

    assert len(withdrawal.cancels) == len(plan.quotes)
    assert all(c.reason == "halting" for c in withdrawal.cancels)
    for cancel in withdrawal.cancels:
        engine.forget_quote(cancel.client_order_id)
    assert engine.resting_quotes() == []


@pytest.mark.asyncio
async def test_market_close_withdraws_instead_of_requoting() -> None:
    engine = strategy()
    await rest_a_bid(engine)

    plan = await engine.plan_quotes(
        book(), market_end_at=NOW + timedelta(seconds=10)
    )

    assert len(plan.cancels) == 1
    assert plan.cancels[0].reason == "market_closing"
    assert plan.quotes == []


@pytest.mark.asyncio
async def test_rejected_quote_is_forgotten_so_the_side_can_requote() -> None:
    engine = strategy()
    plan = await engine.plan_quotes(book())
    submitted(engine, by_side(plan)[SignalSide.BUY], client_order_id="pm-bot-quote000000001")
    engine.record_order_result(
        accepted("pm-bot-quote000000001").model_copy(
            update={"status": OrderStatus.REJECTED, "accepted": False}
        )
    )

    assert engine.resting_quotes() == []
    assert (await engine.plan_quotes(book())).quotes


@pytest.mark.asyncio
async def test_unknown_submission_outcome_drops_the_quote_from_local_state() -> None:
    engine = strategy()
    plan = await engine.plan_quotes(book())
    submitted(engine, by_side(plan)[SignalSide.BUY], client_order_id="pm-bot-quote000000001")
    engine.record_order_result(
        accepted("pm-bot-quote000000001").model_copy(
            update={"status": OrderStatus.UNKNOWN, "accepted": False}
        )
    )

    # Reconciliation, not this strategy, is the authority on an unknown order.
    assert engine.resting_quotes() == []


@pytest.mark.asyncio
async def test_filled_quote_is_forgotten() -> None:
    engine = strategy()
    await rest_a_bid(engine)
    engine.record_order_result(
        accepted("pm-bot-quote000000001").model_copy(
            update={"status": OrderStatus.FILLED, "filled_size": Decimal("100")}
        )
    )
    assert engine.resting_quotes() == []


@pytest.mark.asyncio
async def test_an_unconfirmed_cancel_leaves_the_quote_tracked() -> None:
    engine = strategy()
    await rest_a_bid(engine)

    plan = await engine.plan_quotes(book(bid="0.53", ask="0.55"))
    assert plan.cancels
    # The router did not confirm anything, so the order is still ours.
    assert engine.quote_for("pm-bot-quote000000001") is not None


@pytest.mark.asyncio
async def test_restore_puts_a_quote_back_after_a_failed_cancel() -> None:
    engine = strategy()
    await rest_a_bid(engine)
    quote = engine.quote_for("pm-bot-quote000000001")
    assert quote is not None

    engine.forget_quote("pm-bot-quote000000001")
    assert engine.resting_quotes() == []

    engine.restore_quote(quote)
    assert engine.quote_for("pm-bot-quote000000001") is not None


@pytest.mark.asyncio
async def test_forget_quote_clears_tracking_after_a_confirmed_cancel() -> None:
    engine = strategy()
    await rest_a_bid(engine)
    engine.forget_quote("pm-bot-quote000000001")
    assert engine.resting_quotes() == []
