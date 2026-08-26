from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import Mode
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import ExitReason, Position, PositionLifecycle
from state.store import InMemoryStateStore, PositionAccountingError


NOW = datetime(2025, 1, 1, tzinfo=UTC)
END_AT = NOW + timedelta(minutes=15)
APPLY_ARGS = dict(
    market_end_at=END_AT,
    confirmed_at=NOW,
    confirmation_grace_seconds=30,
)


def filled_result(
    order_key: str,
    *,
    filled: str,
    price: str,
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


def partial_result(filled: str, price: str) -> OrderResult:
    return filled_result(
        "0xorder0001",
        filled=filled,
        price=price,
        status=OrderStatus.PARTIALLY_FILLED,
    )


def sell_result(filled: str, price: str) -> OrderResult:
    return filled_result("0xsell0001", filled=filled, price=price, side=OrderSide.SELL)


def state_with_position(*, quantity: str, average: str) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=Mode.LIVE)
    state._positions[("m1", "t1")] = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal(quantity),
        average_entry_price=Decimal(average),
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
    )
    return state


@pytest.mark.asyncio
async def test_confirmed_buy_creates_weighted_position_once() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result("0xorder0001", filled="2", price="0.40")
    applied = await state.apply_confirmed_fill(result, **APPLY_ARGS)
    replay = await state.apply_confirmed_fill(result, **APPLY_ARGS)
    position = await state.get_position("m1", "t1")
    assert applied.delta_size == Decimal("2")
    assert replay.duplicate is True
    assert position is not None and position.quantity == Decimal("2")


@pytest.mark.asyncio
async def test_cumulative_partial_applies_only_new_delta() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.apply_confirmed_fill(partial_result("1", "0.40"), **APPLY_ARGS)
    second = await state.apply_confirmed_fill(partial_result("3", "0.50"), **APPLY_ARGS)
    assert second.delta_size == Decimal("2")
    assert second.delta_notional == Decimal("1.10")
    assert (await state.get_position("m1", "t1")).average_entry_price == Decimal("0.50")


@pytest.mark.asyncio
async def test_sell_reduces_inventory_and_realizes_pnl() -> None:
    state = state_with_position(quantity="3", average="0.40")
    applied = await state.apply_confirmed_fill(sell_result("2", "0.55"), **APPLY_ARGS)
    assert applied.position.quantity == Decimal("1")
    assert applied.position.realized_pnl == Decimal("0.30")


@pytest.mark.asyncio
async def test_sell_cannot_exceed_inventory() -> None:
    state = state_with_position(quantity="1", average="0.40")
    with pytest.raises(PositionAccountingError, match="sell_exceeds_inventory"):
        await state.apply_confirmed_fill(sell_result("2", "0.50"), **APPLY_ARGS)


@pytest.mark.asyncio
async def test_sell_to_zero_closes_position_and_retains_close_record() -> None:
    state = state_with_position(quantity="2", average="0.40")
    applied = await state.apply_confirmed_fill(sell_result("2", "0.60"), **APPLY_ARGS)
    assert applied.position.quantity == Decimal("0")
    assert await state.get_position("m1", "t1") is None
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.closed_at == NOW
    assert lifecycle.closed_exit_price == Decimal("0.60")
    assert lifecycle.closed_realized_pnl == Decimal("0.40")
    assert lifecycle.confirmation_deadline == NOW + timedelta(seconds=30)


@pytest.mark.asyncio
async def test_buy_after_close_creates_fresh_active_lifecycle() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.apply_confirmed_fill(
        filled_result("0xbuyorder001", filled="2", price="0.40"),
        **APPLY_ARGS,
    )
    await state.apply_confirmed_fill(
        filled_result(
            "0xsellorder01",
            filled="2",
            price="0.60",
            side=OrderSide.SELL,
        ),
        **APPLY_ARGS,
    )
    reopened_at = NOW + timedelta(minutes=5)
    await state.apply_confirmed_fill(
        filled_result("0xbuyorder002", filled="1", price="0.50"),
        market_end_at=END_AT,
        confirmed_at=reopened_at,
        confirmation_grace_seconds=30,
    )

    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.opened_at == reopened_at
    assert lifecycle.closed_at is None
    assert lifecycle.closed_exit_price is None
    assert lifecycle.closed_realized_pnl is None
    assert lifecycle.last_exit_reason is None
    assert lifecycle.pending_exit_client_order_id is None
    assert lifecycle.exit_attempt_count == 0
    closed = await state.get_closed_position_lifecycles()
    assert len(closed) == 1
    assert closed[0].closed_realized_pnl == Decimal("0.40")


@pytest.mark.asyncio
async def test_cumulative_size_regression_is_rejected() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.apply_confirmed_fill(partial_result("2", "0.40"), **APPLY_ARGS)
    with pytest.raises(PositionAccountingError, match="cumulative_size_regression"):
        await state.apply_confirmed_fill(partial_result("1", "0.40"), **APPLY_ARGS)


@pytest.mark.asyncio
async def test_cumulative_notional_regression_is_rejected() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.apply_confirmed_fill(partial_result("2", "0.50"), **APPLY_ARGS)
    with pytest.raises(PositionAccountingError, match="cumulative_notional_regression"):
        await state.apply_confirmed_fill(partial_result("3", "0.30"), **APPLY_ARGS)


@pytest.mark.asyncio
async def test_missing_identity_is_rejected() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result("0xorder0001", filled="1", price="0.40").model_copy(
        update={"market_id": None}
    )
    with pytest.raises(PositionAccountingError, match="missing_identity"):
        await state.apply_confirmed_fill(result, **APPLY_ARGS)


@pytest.mark.asyncio
async def test_empty_identity_is_rejected() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result("0xorder0001", filled="1", price="0.40").model_copy(
        update={"market_id": ""}
    )
    with pytest.raises(PositionAccountingError, match="missing_identity"):
        await state.apply_confirmed_fill(result, **APPLY_ARGS)


@pytest.mark.asyncio
async def test_missing_fill_price_is_rejected() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result("0xorder0001", filled="1", price="0.40").model_copy(
        update={"avg_fill_price": None}
    )
    with pytest.raises(PositionAccountingError, match="missing_avg_fill_price"):
        await state.apply_confirmed_fill(result, **APPLY_ARGS)


@pytest.mark.asyncio
async def test_unknown_result_never_changes_inventory() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result(
        "0xorder0001", filled="1", price="0.40", status=OrderStatus.UNKNOWN
    )
    with pytest.raises(PositionAccountingError, match="unconfirmed_status"):
        await state.apply_confirmed_fill(result, **APPLY_ARGS)
    assert await state.get_positions() == []


@pytest.mark.asyncio
async def test_simulated_fill_is_rejected_in_live_mode() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result(
        "0xorder0001", filled="1", price="0.40", status=OrderStatus.SIMULATED
    )
    with pytest.raises(PositionAccountingError, match="simulated_fill_in_live_mode"):
        await state.apply_confirmed_fill(result, **APPLY_ARGS)


@pytest.mark.asyncio
async def test_simulated_fill_applies_in_dry_run_mode() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    result = filled_result(
        "client-order-0001", filled="1", price="0.40", status=OrderStatus.SIMULATED
    )
    applied = await state.apply_confirmed_fill(result, **APPLY_ARGS)
    assert applied.delta_size == Decimal("1")
    assert (await state.get_position("m1", "t1")).quantity == Decimal("1")


@pytest.mark.asyncio
async def test_sell_delta_resets_exit_attempt_count() -> None:
    state = state_with_position(quantity="3", average="0.40")
    await state.reserve_exit(
        "m1", "t1", client_order_id="exit-order-0001",
        reason=ExitReason.TAKE_PROFIT, attempted_at=NOW,
    )
    await state.reserve_exit(
        "m1", "t1", client_order_id="exit-order-0002",
        reason=ExitReason.TAKE_PROFIT, attempted_at=NOW,
    )
    await state.apply_confirmed_fill(sell_result("1", "0.50"), **APPLY_ARGS)
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.exit_attempt_count == 0


@pytest.mark.asyncio
async def test_exit_reservation_is_exclusive_until_released() -> None:
    state = state_with_position(quantity="2", average="0.40")
    first = await state.reserve_exit(
        "m1", "t1", client_order_id="exit-order-0001",
        reason=ExitReason.TAKE_PROFIT, attempted_at=NOW,
    )
    second = await state.reserve_exit(
        "m1", "t1", client_order_id="exit-order-0002",
        reason=ExitReason.TAKE_PROFIT, attempted_at=NOW,
    )
    assert first is True and second is False
    assert await state.release_exit("m1", "t1", client_order_id="exit-order-0001") is True
    assert await state.release_exit("m1", "t1", client_order_id="exit-order-0001") is False


@pytest.mark.asyncio
async def test_checkpoint_and_lifecycle_accessors_round_trip() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.apply_confirmed_fill(
        filled_result("0xorder0001", filled="2", price="0.40"), **APPLY_ARGS
    )
    checkpoints = await state.get_fill_checkpoints()
    lifecycles = await state.get_position_lifecycles()
    assert len(checkpoints) == 1
    assert checkpoints[0].accounted_filled_size == Decimal("2")
    assert len(lifecycles) == 1
    assert lifecycles[0].opened_at == NOW

    restored = InMemoryStateStore(mode=Mode.LIVE)
    await restored.restore_fill_checkpoint(checkpoints[0])
    await restored.restore_position_lifecycle(lifecycles[0])
    assert await restored.get_fill_checkpoints() == checkpoints
    assert await restored.get_position_lifecycles() == lifecycles


@pytest.mark.asyncio
async def test_realised_pnl_is_net_of_taker_fees() -> None:
    """A round trip that looks flat gross is a loss once fees are charged."""

    from decimal import Decimal

    from config.schema import Mode
    from models.order import OrderResult, OrderSide, OrderStatus
    from state.store import InMemoryStateStore

    store = InMemoryStateStore(mode=Mode.DRY_RUN, fee_rate=Decimal("0.07"))
    now = datetime.now(tz=UTC)

    buy = OrderResult(
        client_order_id="fee-buy-000001",
        market_id="m1", token_id="t1", side=OrderSide.BUY,
        status=OrderStatus.FILLED, accepted=True,
        requested_size=Decimal("100"), filled_size=Decimal("100"),
        avg_fill_price=Decimal("0.50"),
    )
    await store.apply_confirmed_fill(
        buy, market_end_at=None, confirmed_at=now, confirmation_grace_seconds=30
    )

    sell = OrderResult(
        client_order_id="fee-sell-00001",
        market_id="m1", token_id="t1", side=OrderSide.SELL,
        status=OrderStatus.FILLED, accepted=True,
        requested_size=Decimal("100"), filled_size=Decimal("100"),
        avg_fill_price=Decimal("0.50"),
    )
    await store.apply_confirmed_fill(
        sell, market_end_at=None, confirmed_at=now, confirmation_grace_seconds=30
    )

    # The round trip sells the position back to zero, which closes it (see
    # test_sell_to_zero_closes_position_and_retains_close_record) — so the
    # realised P&L is read off the closed lifecycle, not an active position.
    lifecycle = await store.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    # Flat on price, but two taker fills at 0.50 cost 1.75 each.
    assert lifecycle.closed_realized_pnl == Decimal("-3.5000")


@pytest.mark.asyncio
async def test_maker_fills_are_charged_nothing() -> None:
    from decimal import Decimal

    from config.schema import Mode
    from models.order import OrderResult, OrderSide, OrderStatus
    from state.store import InMemoryStateStore

    store = InMemoryStateStore(mode=Mode.DRY_RUN, fee_rate=Decimal("0.07"))
    now = datetime.now(tz=UTC)

    for side, cid in ((OrderSide.BUY, "mk-buy-0000001"), (OrderSide.SELL, "mk-sell-000001")):
        await store.apply_confirmed_fill(
            OrderResult(
                client_order_id=cid,
                market_id="m1", token_id="t1", side=side,
                status=OrderStatus.FILLED, accepted=True,
                requested_size=Decimal("100"), filled_size=Decimal("100"),
                avg_fill_price=Decimal("0.50"), liquidity="maker",
            ),
            market_end_at=None, confirmed_at=now, confirmation_grace_seconds=30,
        )

    # As above, the round trip closes the position, so check the lifecycle.
    lifecycle = await store.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.closed_realized_pnl == Decimal("0")


@pytest.mark.asyncio
async def test_sub_minimum_residue_is_retired_as_dust_not_deferred() -> None:
    """
    A partial fill can leave inventory below the venue's minimum order size,
    which can never be sold and so can never be reconciled away.

    On 2026-08-26 a live round trip filled 5.00 in and 4.995 out, leaving
    0.005 shares. Reconciliation treated that as a divergence: it deferred
    while the confirmation deadline ran, then recorded
    position_confirmation_timeout on every later pass. The repeated incidents
    tripped the breaker and halted trading over inventory worth a fifth of a
    cent. Dust has to be recognised as dust.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    now = datetime(2026, 8, 26, 8, 5, tzinfo=UTC)
    state._positions[("m1", "t1")] = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("0.005"),
        average_entry_price=Decimal("0.36"),
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=now - timedelta(minutes=3),
        last_fill_at=now - timedelta(minutes=2),
        # Grace already elapsed: remote has had its chance to catch up.
        confirmation_deadline=now - timedelta(seconds=1),
    )

    result = await state.merge_authoritative_positions(
        [], now=now, dust_threshold=Decimal("5")
    )

    assert result.deferred_keys == []
    assert result.expired_keys == []
    assert result.dust_keys == ["m1:t1"]
    # The residue is gone from local state and the deadline is cleared, so no
    # later pass can re-raise it.
    assert await state.get_position("m1", "t1") is None
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.confirmation_deadline is None


@pytest.mark.asyncio
async def test_a_sellable_divergence_is_still_deferred() -> None:
    """Dust handling must not swallow a real, tradeable disagreement."""

    state = InMemoryStateStore(mode=Mode.LIVE)
    now = datetime(2026, 8, 26, 8, 5, tzinfo=UTC)
    state._positions[("m1", "t1")] = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("40"),
        average_entry_price=Decimal("0.36"),
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=now - timedelta(minutes=3),
        last_fill_at=now - timedelta(minutes=2),
        confirmation_deadline=now + timedelta(seconds=30),
    )

    result = await state.merge_authoritative_positions(
        [], now=now, dust_threshold=Decimal("5")
    )

    assert result.deferred_keys == ["m1:t1"]
    assert result.dust_keys == []


@pytest.mark.asyncio
async def test_dust_does_not_jump_the_confirmation_grace_period() -> None:
    """
    Dust retirement must not run ahead of the confirmation deadline.

    That deadline exists to stop a stale remote read from discarding a fill
    the exchange has already confirmed to us. A sub-threshold position is
    still real inventory, so while the grace period is open it is deferred
    like any other divergence; only once remote has had its chance to catch
    up is it written off as dust.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    now = datetime(2026, 8, 26, 8, 5, tzinfo=UTC)
    state._positions[("m1", "t1")] = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("4.995"),
        average_entry_price=Decimal("0.62"),
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=now - timedelta(seconds=5),
        last_fill_at=now - timedelta(seconds=2),
        confirmation_deadline=now + timedelta(seconds=25),
    )

    result = await state.merge_authoritative_positions(
        [], now=now, dust_threshold=Decimal("5")
    )

    assert result.dust_keys == []
    assert result.deferred_keys == ["m1:t1"]
    held = await state.get_position("m1", "t1")
    assert held is not None
    assert held.quantity == Decimal("4.995")
