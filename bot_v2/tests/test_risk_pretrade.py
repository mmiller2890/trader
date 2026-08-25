from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.position import ExitReason, Position, PositionLifecycle
from models.signal import SignalSide, SignalType, TradeSignal
from models.operations import OperationalState
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore


def ready_state_store() -> InMemoryStateStore:
    return InMemoryStateStore(mode=Mode.DRY_RUN)


def risk_engine(store: InMemoryStateStore) -> PreTradeRiskEngine:
    return PreTradeRiskEngine(config=AppConfig(), state_store=store)


def buy_signal() -> TradeSignal:
    return make_signal()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        OperationalState.DEGRADED,
        OperationalState.HALTING,
        OperationalState.HALTED,
        OperationalState.FAILED,
    ],
)
async def test_buy_is_rejected_outside_running_state(state: OperationalState) -> None:
    store = ready_state_store()
    await store.set_operational_state(state, reason="test_incident")
    decision = await risk_engine(store).evaluate(
        signal=buy_signal(), snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"), proposed_price=Decimal("0.5"),
        executable_liquidity=Decimal("100"),
    )
    assert decision.approved is False
    assert decision.reason == f"entries_paused:{state.value}:test_incident"


@pytest.mark.asyncio
async def test_sell_still_evaluated_when_entries_paused() -> None:
    store = ready_state_store()
    await store.set_operational_state(OperationalState.DEGRADED, reason="test_incident")
    await store.set_position(
        Position(market_id="m1", token_id="t1", quantity=Decimal("2"))
    )
    sell_signal = buy_signal().model_copy(update={"side": SignalSide.SELL})
    decision = await risk_engine(store).evaluate(
        signal=sell_signal, snapshot=fresh_snapshot(),
        proposed_size=Decimal("2"), proposed_price=Decimal("0.45"),
    )
    assert "entries_paused" not in decision.reason


def fresh_snapshot() -> MarketSnapshot:
    now = datetime.now(tz=UTC)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.45"),
        best_ask=Decimal("0.46"),
        mid_price=Decimal("0.455"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        received_ts=now,
        source_ts=now,
    )


def make_signal() -> TradeSignal:
    return TradeSignal(
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.46"),
        observed_move_bps=150,
        reason="test",
    )


@pytest.mark.asyncio
async def test_pretrade_rejects_kill_switch() -> None:
    config = AppConfig()
    state = InMemoryStateStore(mode=Mode.DRY_RUN, kill_switch_active=True)
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.46"),
    )

    assert not decision.approved
    assert decision.reason == "kill_switch_active"


@pytest.mark.asyncio
async def test_pretrade_rejects_position_limit() -> None:
    config = AppConfig()
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("49"),
            average_entry_price=Decimal("0.40"),
        )
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.46"),
    )

    assert not decision.approved
    assert "single_position_limit" in decision.reason


@pytest.mark.asyncio
async def test_pretrade_can_evaluate_depth_liquidity_override() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "2"})
    state = InMemoryStateStore(mode=Mode.BACKTEST)
    engine = PreTradeRiskEngine(config=config, state_store=state)
    item = fresh_snapshot().model_copy(update={"top_ask_size": Decimal("1")})
    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=item,
        proposed_size=Decimal("3"),
        proposed_price=Decimal("0.46"),
        executable_liquidity=Decimal("3"),
    )
    assert next(check for check in decision.checks if check.check_name == "top_of_book_liquidity").passed


@pytest.mark.asyncio
async def test_pretrade_rejects_stale_market_data() -> None:
    config = AppConfig()
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    engine = PreTradeRiskEngine(config=config, state_store=state)
    snapshot = fresh_snapshot().model_copy(
        update={"received_ts": datetime.now(tz=UTC) - timedelta(seconds=60)}
    )

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=snapshot,
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.46"),
    )

    assert not decision.approved
    assert decision.reason.startswith("market_data_stale")


@pytest.mark.asyncio
async def test_pretrade_rejects_buy_while_exit_is_pending() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "1"})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    now = datetime.now(tz=UTC)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("2"),
            average_entry_price=Decimal("0.40"),
        )
    )
    await state.restore_position_lifecycle(
        PositionLifecycle(
            market_id="m1",
            token_id="t1",
            opened_at=now,
            last_fill_at=now,
        )
    )
    await state.reserve_exit(
        "m1",
        "t1",
        client_order_id="exit-order-0001",
        reason=ExitReason.TAKE_PROFIT,
        attempted_at=now,
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    assert decision.approved is False
    assert decision.reason == "entry_blocked_by_pending_exit:exit-order-0001"


@pytest.mark.asyncio
async def test_total_exposure_is_marked_notional_not_raw_share_count() -> None:
    config = AppConfig(
        risk={
            "max_total_exposure": "51",
            "min_top_of_book_liquidity": "1",
        }
    )
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="other-market",
            token_id="other-token",
            quantity=Decimal("100"),
            mark_price=Decimal("0.50"),
        )
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("2"),
        proposed_price=Decimal("0.46"),
    )

    assert decision.approved is True
    exposure = next(
        check for check in decision.checks
        if check.check_name == "max_total_exposure"
    )
    assert exposure.reason == "total_exposure_within_limit"


@pytest.mark.asyncio
async def test_total_exposure_includes_proposed_order_notional() -> None:
    config = AppConfig(
        risk={
            "max_total_exposure": "51",
            "min_top_of_book_liquidity": "1",
        }
    )
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="other-market",
            token_id="other-token",
            quantity=Decimal("100"),
            mark_price=Decimal("0.50"),
        )
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("4"),
        proposed_price=Decimal("0.50"),
    )

    assert decision.approved is False
    assert decision.reason == "total_exposure_limit:52.00>51"


@pytest.mark.asyncio
async def test_pretrade_rejects_sell_without_token_inventory() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "1"})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    engine = PreTradeRiskEngine(config=config, state_store=state)
    sell_signal = make_signal().model_copy(update={"side": SignalSide.SELL})

    decision = await engine.evaluate(
        signal=sell_signal,
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.45"),
    )

    assert decision.approved is False
    assert decision.reason == "insufficient_position_to_sell:0<1"


@pytest.mark.asyncio
async def test_pretrade_allows_bounded_synthetic_short_in_backtest() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        risk={
            "max_total_exposure": "10",
            "max_single_position_size": "5",
            "min_top_of_book_liquidity": "1",
        },
    )
    state = InMemoryStateStore(mode=Mode.BACKTEST)
    engine = PreTradeRiskEngine(config=config, state_store=state)
    sell_signal = make_signal().model_copy(update={"side": SignalSide.SELL})

    decision = await engine.evaluate(
        signal=sell_signal,
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.45"),
    )

    assert decision.approved is True


@pytest.mark.asyncio
async def test_pretrade_sell_reduces_total_marked_exposure() -> None:
    config = AppConfig(
        risk={
            "max_total_exposure": "1",
            "max_single_position_size": "1",
            "min_top_of_book_liquidity": "1",
        }
    )
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("2"),
            mark_price=Decimal("0.50"),
        )
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)
    sell_signal = make_signal().model_copy(update={"side": SignalSide.SELL})

    decision = await engine.evaluate(
        signal=sell_signal,
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.45"),
    )

    assert decision.approved is True


def exit_signal(*, size: str) -> TradeSignal:
    return TradeSignal(
        strategy_name="position_exit",
        signal_type=SignalType.POSITION_EXIT,
        market_id="m1",
        token_id="t1",
        side=SignalSide.SELL,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.45"),
        observed_move_bps=100,
        reason="take_profit",
        requested_size=Decimal(size),
        reduce_only=True,
    )


@pytest.mark.asyncio
async def test_reduce_only_sell_requires_exact_inventory() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "1"})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("2"),
            average_entry_price=Decimal("0.40"),
        )
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=exit_signal(size="2"),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("2"),
        proposed_price=Decimal("0.45"),
    )

    assert decision.approved is True


@pytest.mark.asyncio
async def test_reduce_only_sell_above_inventory_is_rejected() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "1"})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("1"),
            average_entry_price=Decimal("0.40"),
        )
    )
    engine = PreTradeRiskEngine(config=config, state_store=state)

    decision = await engine.evaluate(
        signal=exit_signal(size="2"),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("2"),
        proposed_price=Decimal("0.45"),
    )

    assert decision.approved is False
    assert "insufficient_position_to_sell" in decision.reason


@pytest.mark.asyncio
async def test_reduce_only_buy_is_rejected() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "1"})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    engine = PreTradeRiskEngine(config=config, state_store=state)
    buy_signal = TradeSignal.model_construct(
        strategy_name="spike",
        signal_type=SignalType.PRICE_SPIKE,
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.46"),
        observed_move_bps=100,
        reason="test",
        reduce_only=True,
    )

    decision = await engine.evaluate(
        signal=buy_signal,
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    assert decision.approved is False
    assert "reduce_only_requires_sell" in decision.reason


def maker_quote(
    *,
    side: SignalSide = SignalSide.BUY,
    limit_price: str = "0.44",
    size: str = "100",
) -> TradeSignal:
    return TradeSignal(
        strategy_name="market_maker",
        signal_type=SignalType.MAKER_QUOTE,
        market_id="m1",
        token_id="t1",
        side=side,
        reference_price=Decimal("0.455"),
        target_price=Decimal(limit_price),
        observed_move_bps=0,
        reason="quote_refresh",
        requested_size=Decimal(size),
        limit_price=Decimal(limit_price),
        post_only=True,
    )


@pytest.mark.asyncio
async def test_maker_quote_is_not_blocked_by_its_own_previous_quote() -> None:
    store = ready_state_store()
    engine = risk_engine(store)
    first = maker_quote()
    await store.add_signal(first)

    decision = await engine.evaluate(
        signal=maker_quote(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.44"),
    )

    assert decision.approved is True
    guard = next(
        check for check in decision.checks if check.check_name == "duplicate_guard"
    )
    assert guard.reason == "maker_quote_exempt"


@pytest.mark.asyncio
async def test_maker_quote_does_not_require_opposing_liquidity() -> None:
    store = ready_state_store()
    engine = risk_engine(store)
    empty_book = fresh_snapshot().model_copy(
        update={"top_ask_size": Decimal("0"), "top_bid_size": Decimal("0")}
    )

    decision = await engine.evaluate(
        signal=maker_quote(),
        snapshot=empty_book,
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.44"),
    )

    assert decision.approved is True


@pytest.mark.asyncio
async def test_maker_quote_is_exempt_from_taker_slippage() -> None:
    store = ready_state_store()
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=maker_quote(limit_price="0.30"),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.30"),
    )

    assert decision.approved is True


@pytest.mark.asyncio
async def test_maker_quote_still_obeys_the_kill_switch() -> None:
    store = ready_state_store()
    await store.activate_kill_switch("manual halt")
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=maker_quote(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.44"),
    )

    assert decision.approved is False


@pytest.mark.asyncio
async def test_maker_quote_still_obeys_position_and_exposure_limits() -> None:
    store = ready_state_store()
    await store.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("49"),
            average_entry_price=Decimal("0.45"),
            mark_price=Decimal("0.45"),
        )
    )
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=maker_quote(size="100"),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("100"),
        proposed_price=Decimal("0.44"),
    )

    assert decision.approved is False
    assert "single_position_limit" in decision.reason


@pytest.mark.asyncio
async def test_maker_ask_still_requires_inventory_to_sell() -> None:
    store = ready_state_store()
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=maker_quote(side=SignalSide.SELL, limit_price="0.47"),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("100"),
        proposed_price=Decimal("0.47"),
    )

    assert decision.approved is False
    assert "insufficient_position_to_sell" in decision.reason


def exit_signal(*, size: str = "5") -> TradeSignal:
    return TradeSignal(
        strategy_name="position_exit",
        signal_type=SignalType.POSITION_EXIT,
        market_id="m1",
        token_id="t1",
        side=SignalSide.SELL,
        reference_price=Decimal("0.50"),
        target_price=Decimal("0.45"),
        observed_move_bps=0,
        reason="position_exit:stop_loss",
        requested_size=Decimal(size),
        reduce_only=True,
    )


@pytest.mark.asyncio
async def test_exit_retry_is_not_blocked_as_a_duplicate() -> None:
    """
    Regression: the exit budget exhausted without a single retry being sent.

    Retries fire every 2s while the duplicate window is 15s, so every retry
    after the first was rejected as a duplicate, the budget ran out, and the
    kill switch latched on a position the bot never actually tried to exit
    more than once.
    """

    store = ready_state_store()
    await store.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("5"),
            average_entry_price=Decimal("0.50"),
            mark_price=Decimal("0.45"),
        )
    )
    engine = risk_engine(store)
    first = exit_signal()
    await store.add_signal(first)

    decision = await engine.evaluate(
        signal=exit_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.45"),
    )

    guard = next(
        check for check in decision.checks if check.check_name == "duplicate_guard"
    )
    assert guard.passed is True
    assert guard.reason == "exit_retry_exempt"


@pytest.mark.asyncio
async def test_entry_duplicates_are_still_blocked() -> None:
    store = ready_state_store()
    engine = risk_engine(store)
    await store.add_signal(make_signal())

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    assert decision.approved is False
    assert "duplicate_signal" in decision.reason


@pytest.mark.asyncio
async def test_exit_still_requires_inventory_to_sell() -> None:
    store = ready_state_store()
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=exit_signal(size="5"),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.45"),
    )

    assert decision.approved is False
    assert "insufficient_position_to_sell" in decision.reason


def wide_book(*, bid: str, ask: str) -> MarketSnapshot:
    now = datetime.now(tz=UTC)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=(Decimal(bid) + Decimal(ask)) / 2,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        received_ts=now,
        source_ts=now,
    )


@pytest.mark.asyncio
async def test_a_book_too_wide_to_cross_is_refused() -> None:
    """
    Regression: 38 of 61 trades were sub-second round trips losing the spread.

    The book quoted 0.09 / 0.91 with plenty of size on both sides, so the
    liquidity check passed. Buying the ask and marking against the bid lost 82
    cents a share at the instant of entry, before any market move.
    """

    store = ready_state_store()
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=wide_book(bid="0.09", ask="0.91"),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.91"),
    )

    assert decision.approved is False
    assert "entry_spread_too_wide" in decision.reason


@pytest.mark.asyncio
async def test_a_normal_book_still_passes() -> None:
    store = ready_state_store()
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=wide_book(bid="0.45", ask="0.46"),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    spread = next(
        check for check in decision.checks if check.check_name == "entry_spread"
    )
    assert spread.passed is True


@pytest.mark.asyncio
async def test_exits_are_never_blocked_by_a_wide_book() -> None:
    # Being stuck in a position is worse than crossing a wide book to leave it.
    store = ready_state_store()
    await store.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("5"),
            average_entry_price=Decimal("0.50"),
            mark_price=Decimal("0.09"),
        )
    )
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=exit_signal(),
        snapshot=wide_book(bid="0.09", ask="0.91"),
        proposed_size=Decimal("5"),
        proposed_price=Decimal("0.09"),
    )

    spread = next(
        check for check in decision.checks if check.check_name == "entry_spread"
    )
    assert spread.passed is True


@pytest.mark.asyncio
async def test_maker_quotes_are_exempt_from_the_spread_guard() -> None:
    # A wide book is exactly where resting a quote is worth most.
    store = ready_state_store()
    engine = risk_engine(store)

    decision = await engine.evaluate(
        signal=maker_quote(limit_price="0.40"),
        snapshot=wide_book(bid="0.09", ask="0.91"),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.40"),
    )

    assert decision.approved is True


@pytest.mark.asyncio
async def test_spread_guard_can_be_disabled() -> None:
    store = ready_state_store()
    engine = PreTradeRiskEngine(
        config=AppConfig(risk={"max_entry_spread_bps": 0}), state_store=store
    )

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=wide_book(bid="0.09", ask="0.91"),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.91"),
    )

    spread = next(
        check for check in decision.checks if check.check_name == "entry_spread"
    )
    assert spread.passed is True
