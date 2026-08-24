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
