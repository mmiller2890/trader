from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import Mode
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import Balance, FillCheckpoint, Position, PositionLifecycle
from persistence.snapshots import SnapshotStore
from state.store import InMemoryStateStore


NOW = datetime(2025, 1, 1, tzinfo=UTC)
END_AT = NOW + timedelta(minutes=15)


def accounted_state(mode: Mode) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=mode)
    state._fill_checkpoints["0xorder0001"] = FillCheckpoint(
        order_key="0xorder0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        accounted_filled_size=Decimal("2"),
        accounted_fill_notional=Decimal("0.8"),
        confirmed_at=NOW,
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        market_end_at=END_AT,
    )
    return state


@pytest.mark.asyncio
async def test_snapshot_round_trips_fill_checkpoints_and_lifecycle(tmp_path) -> None:
    original = accounted_state(mode=Mode.DRY_RUN)
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(original)
    restored = InMemoryStateStore(mode=Mode.DRY_RUN)
    assert await snapshots.restore_into_state(restored) is True
    assert await restored.get_fill_checkpoints() == await original.get_fill_checkpoints()
    assert await restored.get_position_lifecycles() == await original.get_position_lifecycles()


@pytest.mark.asyncio
async def test_old_snapshot_without_new_fields_still_loads(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"mode": "dry_run"}), encoding="utf-8")
    loaded = await SnapshotStore(path).load()
    assert loaded is not None
    assert loaded.fill_checkpoints == []
    assert loaded.position_lifecycles == []


@pytest.mark.asyncio
async def test_snapshot_write_is_atomic_on_replace_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"mode": "dry_run", "saved_at": "2025-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    original_content = path.read_text(encoding="utf-8")
    snapshots = SnapshotStore(path)

    import pathlib

    real_replace = pathlib.Path.replace

    def failing_replace(self, target: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(pathlib.Path, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        await snapshots.save_from_state(InMemoryStateStore(mode=Mode.DRY_RUN))

    monkeypatch.setattr(pathlib.Path, "replace", real_replace)
    assert path.read_text(encoding="utf-8") == original_content
    loaded = await SnapshotStore(path).load()
    assert loaded is not None
    assert loaded.mode == Mode.DRY_RUN


@pytest.mark.asyncio
async def test_snapshot_restores_trading_state_into_fresh_store(tmp_path) -> None:
    original = InMemoryStateStore(mode=Mode.LIVE)
    order = OrderResult(
        client_order_id="client-order-0001",
        exchange_order_id="0xexchange0001",
        status=OrderStatus.SUBMITTED,
        accepted=True,
        requested_size=Decimal("2"),
    )
    position = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("3"),
        average_entry_price=Decimal("0.4"),
    )
    balance = Balance(currency="USDC", available=Decimal("10"), total=Decimal("12"))
    await original.set_order_status(order)
    await original.set_position(position)
    await original.set_balance(balance)
    await original.update_heartbeat("app")

    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(original)
    restored = InMemoryStateStore(mode=Mode.LIVE)

    loaded = await snapshots.restore_into_state(restored)

    assert loaded is True
    assert await restored.get_open_orders() == [order]
    assert await restored.get_positions() == [position]
    assert await restored.get_balances() == [balance]
    assert await restored.get_heartbeat("app") is not None


@pytest.mark.asyncio
async def test_snapshot_from_other_mode_is_not_restored(tmp_path) -> None:
    original = InMemoryStateStore(mode=Mode.DRY_RUN)
    await original.set_position(
        Position(market_id="m1", token_id="t1", quantity=Decimal("3"))
    )
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(original)
    restored = InMemoryStateStore(mode=Mode.LIVE)

    loaded = await snapshots.restore_into_state(restored)

    assert loaded is False
    assert await restored.get_positions() == []


@pytest.mark.asyncio
async def test_runtime_restore_can_exclude_historical_heartbeats(tmp_path) -> None:
    original = InMemoryStateStore(mode=Mode.DRY_RUN)
    await original.update_heartbeat("market_data")
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(original)
    restored = InMemoryStateStore(mode=Mode.DRY_RUN)

    loaded = await snapshots.restore_into_state(
        restored, restore_heartbeats=False
    )

    assert loaded is True
    assert await restored.get_heartbeat("market_data") is None


@pytest.mark.asyncio
async def test_snapshot_preserves_latched_halt_for_operator_history(tmp_path) -> None:
    original = InMemoryStateStore(mode=Mode.LIVE)
    await original.activate_kill_switch("transport_heartbeat_stale")
    snapshots = SnapshotStore(tmp_path / "state.json")

    saved = await snapshots.save_from_state(original)

    assert saved.kill_switch_active is True
    assert saved.kill_switch_reason == "transport_heartbeat_stale"

    restored = InMemoryStateStore(mode=Mode.LIVE)
    assert await snapshots.restore_into_state(restored) is True
    assert await restored.is_kill_switch_active() is True
    assert await restored.get_kill_switch_reason() == "transport_heartbeat_stale"


@pytest.mark.asyncio
async def test_snapshot_restores_bounded_closed_position_history(tmp_path) -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    buy = OrderResult(
        client_order_id="buy-order-0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        status=OrderStatus.SIMULATED,
        accepted=True,
        requested_size=Decimal("2"),
        filled_size=Decimal("2"),
        avg_fill_price=Decimal("0.40"),
    )
    sell = buy.model_copy(
        update={
            "client_order_id": "sell-order-0001",
            "side": OrderSide.SELL,
            "avg_fill_price": Decimal("0.60"),
        }
    )
    apply_args = {
        "market_end_at": END_AT,
        "confirmed_at": NOW,
        "confirmation_grace_seconds": 30,
    }
    await state.apply_confirmed_fill(buy, **apply_args)
    await state.apply_confirmed_fill(sell, **apply_args)
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(state)

    restored = InMemoryStateStore(mode=Mode.DRY_RUN)
    assert await snapshots.restore_into_state(restored) is True

    closed = await restored.get_closed_position_lifecycles()
    assert len(closed) == 1
    assert closed[0].closed_realized_pnl == Decimal("0.40")
    assert await restored.get_daily_realized_pnl(NOW) == Decimal("0.40")


@pytest.mark.asyncio
async def test_live_restore_can_skip_historical_positions(tmp_path) -> None:
    original = InMemoryStateStore(mode=Mode.LIVE)
    await original.set_position(
        Position(
            market_id="resolved-market",
            token_id="resolved-token",
            quantity=Decimal("1000"),
            mark_price=Decimal("0"),
        )
    )
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(original)
    restored = InMemoryStateStore(mode=Mode.LIVE)

    loaded = await snapshots.restore_into_state(
        restored,
        restore_positions=False,
    )

    assert loaded is True
    assert await restored.get_positions() == []
