from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from config.schema import Mode
from models.market import MarketSnapshot, OrderBookLevel, OrderBookUpdate
from models.order import OrderResult, OrderSide, OrderStatus
from state.store import InMemoryStateStore


@pytest.mark.asyncio
async def test_state_store_updates_and_removes_open_orders() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    now = datetime.now(tz=UTC)

    update = OrderBookUpdate(
        market_id="m1",
        token_id="t1",
        bids=[OrderBookLevel(price=Decimal("0.44"), size=Decimal("10"))],
        asks=[OrderBookLevel(price=Decimal("0.45"), size=Decimal("10"))],
        received_ts=now,
        source_ts=now,
    )
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.44"),
        best_ask=Decimal("0.45"),
        mid_price=Decimal("0.445"),
        top_bid_size=Decimal("10"),
        top_ask_size=Decimal("10"),
        received_ts=now,
        source_ts=now,
    )

    await state.update_orderbook(update)
    await state.update_market_snapshot(snapshot)
    fetched_snapshot = await state.get_market_snapshot("m1", "t1")
    assert fetched_snapshot is not None
    assert fetched_snapshot.mid_price == Decimal("0.445")

    pending = OrderResult(
        client_order_id="pm-bot-order1",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        status=OrderStatus.SUBMITTED,
        accepted=True,
        requested_size=Decimal("5"),
    )
    await state.set_order_status(pending)
    assert len(await state.get_open_orders()) == 1

    simulated = pending.model_copy(update={"status": OrderStatus.SIMULATED})
    await state.set_order_status(simulated)
    assert await state.get_open_orders() == []


@pytest.mark.asyncio
async def test_state_store_tracks_kill_switch_and_heartbeat() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_kill_switch(True)
    await state.update_heartbeat("market_data")

    assert await state.is_kill_switch_active() is True
    assert await state.is_heartbeat_stale("market_data", max_age_seconds=60) is False
