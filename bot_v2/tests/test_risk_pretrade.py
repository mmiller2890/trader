from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.position import Position
from models.signal import SignalSide, TradeSignal
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore


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
