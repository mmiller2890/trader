from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.order import OrderTimeInForce
from models.position import ExitReason, Position, PositionLifecycle
from models.signal import SignalSide, SignalType, TradeSignal
from persistence.snapshots import SnapshotStore
from portfolio.exit_manager import PositionExitManager
from portfolio.exit_policy import PositionExitPolicy
from state.store import InMemoryStateStore


NOW = datetime(2025, 1, 1, tzinfo=UTC)
END_AT = NOW + timedelta(minutes=15)


def snapshot(*, best_bid: str = "0.42", mid: str = "0.42") -> MarketSnapshot:
    bid = Decimal(best_bid)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=bid,
        best_ask=bid + Decimal("0.01"),
        mid_price=Decimal(mid),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=NOW,
        received_ts=NOW,
    )


def state_with_position(*, quantity: str, average: str) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    state._positions[("m1", "t1")] = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal(quantity),
        average_entry_price=Decimal(average),
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW - timedelta(seconds=30),
        last_fill_at=NOW - timedelta(seconds=30),
    )
    return state


def make_exit_manager(
    state: InMemoryStateStore,
    *,
    now: object = lambda: NOW,
    tmp_path: object | None = None,
) -> PositionExitManager:
    config = AppConfig(
        bot={"mode": Mode.DRY_RUN},
        position_management={
            "take_profit_bps": "300",
            "stop_loss_bps": "200",
            "max_hold_seconds": 180,
            "exit_retry_interval_seconds": 2,
            "max_exit_attempts": 3,
        },
    )
    policy = PositionExitPolicy(
        config.position_management,
        min_order_size=config.execution.min_order_size,
        max_data_age_seconds=config.risk.max_data_staleness_seconds,
    )
    snapshots = SnapshotStore(tmp_path / "state.json") if tmp_path is not None else None
    return PositionExitManager(
        config=config,
        state_store=state,
        snapshots=snapshots,
        policy=policy,
        now=now,
    )


@pytest.mark.asyncio
async def test_take_profit_emits_one_reserved_full_position_exit() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    manager = make_exit_manager(state=state, now=lambda: NOW)
    first = await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)
    second = await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)
    assert len(first) == 1 and second == []
    assert first[0].requested_size == Decimal("2.5")
    assert first[0].reduce_only is True
    assert first[0].time_in_force == OrderTimeInForce.IOC


@pytest.mark.asyncio
async def test_rejected_exit_waits_two_seconds_before_retry() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    manager = make_exit_manager(state=state, now=lambda: NOW)
    await state.reserve_exit(
        "m1", "t1", client_order_id="exit-order-0001",
        reason=ExitReason.TAKE_PROFIT, attempted_at=NOW,
    )
    await state.release_exit("m1", "t1", client_order_id="exit-order-0001")
    assert await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT) == []
    manager.set_clock(lambda: NOW + timedelta(seconds=2))
    assert len(await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)) == 1


@pytest.mark.asyncio
async def test_three_attempts_without_reduction_exhausts_exits() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    clock = {"now": NOW}

    def now() -> datetime:
        return clock["now"]

    manager = make_exit_manager(state=state, now=now)
    for _ in range(3):
        signals = await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)
        assert len(signals) == 1
        client_order_id = f"pm-bot-{signals[0].signal_id[:18]}"
        await state.release_exit("m1", "t1", client_order_id=client_order_id)
        clock["now"] = clock["now"] + timedelta(seconds=3)
    assert await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT) == []
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.exit_attempt_count == 3


@pytest.mark.asyncio
async def test_strategy_sell_converts_only_with_inventory() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    manager = make_exit_manager(state=state, now=lambda: NOW)
    sell = TradeSignal(
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.SELL,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.42"),
        observed_move_bps=100,
        reason="spike_down",
    )
    converted = await manager.from_strategy_signal(sell, snapshot=snapshot(), market_end_at=END_AT)
    assert converted is not None
    assert converted.signal_type == SignalType.POSITION_EXIT
    assert converted.reduce_only is True
    assert converted.requested_size == Decimal("2.5")

    empty = InMemoryStateStore(mode=Mode.DRY_RUN)
    empty_manager = make_exit_manager(state=empty, now=lambda: NOW)
    assert await empty_manager.from_strategy_signal(sell, snapshot=snapshot(), market_end_at=END_AT) is None


@pytest.mark.asyncio
async def test_no_duplicate_reservation_across_concurrent_updates() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    manager = make_exit_manager(state=state, now=lambda: NOW)
    results = await asyncio.gather(
        manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT),
        manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT),
        manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT),
    )
    emitted = [signal for batch in results for signal in batch]
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_dust_position_emits_no_exit() -> None:
    state = state_with_position(quantity="0.5", average="0.40")
    manager = make_exit_manager(state=state, now=lambda: NOW)
    assert await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT) == []


@pytest.mark.asyncio
async def test_dust_event_emits_once_not_per_tick() -> None:
    state = state_with_position(quantity="0.5", average="0.40")
    events: list[object] = []

    async def on_event(event: object) -> None:
        events.append(event)

    config = AppConfig(
        bot={"mode": Mode.DRY_RUN},
        position_management={
            "take_profit_bps": "300",
            "stop_loss_bps": "200",
            "max_hold_seconds": 180,
        },
    )
    policy = PositionExitPolicy(
        config.position_management,
        min_order_size=config.execution.min_order_size,
        max_data_age_seconds=config.risk.max_data_staleness_seconds,
    )
    manager = PositionExitManager(
        config=config,
        state_store=state,
        snapshots=None,
        policy=policy,
        now=lambda: NOW,
        on_event=on_event,
    )
    for _ in range(3):
        await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)
    dust_events = [event for event in events if event.event_type.value == "position_dust"]
    assert len(dust_events) == 1


@pytest.mark.asyncio
async def test_position_is_not_evaluated_against_another_tokens_snapshot() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    other_token_snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t2",
        best_bid=Decimal("0.10"),
        best_ask=Decimal("0.11"),
        mid_price=Decimal("0.105"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=NOW,
        received_ts=NOW,
    )
    manager = make_exit_manager(state=state, now=lambda: NOW)
    signals = await manager.on_market_update(
        other_token_snapshot, market_end_at=END_AT
    )
    assert signals == []
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.pending_exit_client_order_id is None


@pytest.mark.asyncio
async def test_timer_exit_uses_market_end_lookup() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    await state.update_market_snapshot(snapshot(best_bid="0.42"))
    manager = make_exit_manager(state=state, now=lambda: NOW)
    signals = await manager.on_timer(
        market_end_lookup=lambda market_id: NOW + timedelta(seconds=30)
    )
    assert len(signals) == 1
    assert signals[0].reason == "position_exit:market_expiry"
