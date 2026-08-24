from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import Mode
from execution.tracker import OrderTracker, TrackingOutcome
from models.order import OrderResult, OrderSide, OrderStatus
from persistence.snapshots import SnapshotStore
from state.store import InMemoryStateStore, PositionAccountingError


NOW = datetime(2025, 1, 1, tzinfo=UTC)
END_AT = NOW + timedelta(minutes=15)


def filled_result(
    *,
    order_key: str = "0xorder0001",
    filled: str = "2",
    price: str = "0.40",
    side: OrderSide = OrderSide.BUY,
    status: OrderStatus = OrderStatus.FILLED,
) -> OrderResult:
    return OrderResult(
        client_order_id="client-order-0001",
        exchange_order_id=order_key,
        market_id="m1",
        token_id="t1",
        side=side,
        status=status,
        accepted=True,
        message="filled",
        requested_size=Decimal(filled),
        filled_size=Decimal(filled),
        avg_fill_price=Decimal(price),
    )


def unknown_result() -> OrderResult:
    return filled_result(status=OrderStatus.UNKNOWN)


def make_tracker(
    mode: Mode, tmp_path: object | None = None
) -> tuple[OrderTracker, InMemoryStateStore, SnapshotStore | None]:
    state = InMemoryStateStore(mode=mode)
    snapshots = SnapshotStore(tmp_path / "state.json") if tmp_path is not None else None
    tracker = OrderTracker(
        state_store=state,
        snapshots=snapshots,
        confirmation_grace_seconds=30,
    )
    return tracker, state, snapshots


@pytest.mark.asyncio
async def test_tracker_applies_fill_and_saves_immediately(tmp_path) -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    snapshots = SnapshotStore(tmp_path / "state.json")
    tracker = OrderTracker(state_store=state, snapshots=snapshots, confirmation_grace_seconds=30)
    outcome = await tracker.handle_order_result(filled_result(), market_end_at=END_AT)
    assert outcome.fill_applied is True
    saved = await snapshots.load()
    assert saved is not None
    assert saved.fill_checkpoints[0].accounted_filled_size == Decimal("2")


@pytest.mark.asyncio
async def test_tracker_never_applies_unknown() -> None:
    tracker, state, _ = make_tracker(Mode.LIVE)
    outcome = await tracker.handle_order_result(unknown_result(), market_end_at=END_AT)
    assert outcome.unknown_outcome is True
    assert await state.get_positions() == []


@pytest.mark.asyncio
async def test_tracker_duplicate_fill_is_a_no_op() -> None:
    tracker, state, _ = make_tracker(Mode.LIVE)
    result = filled_result()
    first = await tracker.handle_order_result(result, market_end_at=END_AT)
    second = await tracker.handle_order_result(result, market_end_at=END_AT)
    assert first.fill_applied is True
    assert second.fill_applied is False
    assert second.fill_application is not None
    assert second.fill_application.duplicate is True
    assert (await state.get_position("m1", "t1")).quantity == Decimal("2")


@pytest.mark.asyncio
async def test_tracker_applies_dry_run_simulated_fill() -> None:
    tracker, state, _ = make_tracker(Mode.DRY_RUN)
    result = filled_result(
        order_key="client-order-0001", status=OrderStatus.SIMULATED
    )
    outcome = await tracker.handle_order_result(result, market_end_at=END_AT)
    assert outcome.fill_applied is True
    assert (await state.get_position("m1", "t1")).quantity == Decimal("2")


@pytest.mark.asyncio
async def test_tracker_reports_close_to_zero() -> None:
    tracker, state, _ = make_tracker(Mode.LIVE)
    await tracker.handle_order_result(filled_result(), market_end_at=END_AT)
    sell = filled_result(
        order_key="0xsell0001",
        filled="2",
        price="0.60",
        side=OrderSide.SELL,
    )
    outcome = await tracker.handle_order_result(sell, market_end_at=END_AT)
    assert outcome.position_closed is True
    assert await state.get_position("m1", "t1") is None


@pytest.mark.asyncio
async def test_tracker_propagates_accounting_error() -> None:
    tracker, state, _ = make_tracker(Mode.LIVE)
    await tracker.handle_order_result(filled_result(), market_end_at=END_AT)
    sell = filled_result(
        order_key="0xsell0001",
        filled="5",
        price="0.60",
        side=OrderSide.SELL,
    )
    outcome = await tracker.handle_order_result(sell, market_end_at=END_AT)
    assert outcome.accounting_error == "sell_exceeds_inventory"
    assert (await state.get_position("m1", "t1")).quantity == Decimal("2")


@pytest.mark.asyncio
async def test_tracker_updates_order_state_and_heartbeat() -> None:
    tracker, state, _ = make_tracker(Mode.LIVE)
    await tracker.handle_order_result(filled_result(), market_end_at=END_AT)
    assert await state.get_open_orders() == []
    assert await state.get_heartbeat("execution") is not None
