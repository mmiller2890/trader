from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from clients.clob_client import ClobAdapterError
from config.schema import Mode
from execution.tracker import OrderTracker
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import Position, PositionLifecycle
from state.reconciliation import ReconciliationService
from state.store import InMemoryStateStore


NOW = datetime(2025, 1, 1, tzinfo=UTC)


def state_with_pending_buy(*, quantity: str, deadline: datetime) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=Mode.LIVE)
    state._positions[("m1", "t1")] = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal(quantity),
        average_entry_price=Decimal("0.50"),
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        confirmation_deadline=deadline,
    )
    return state


def state_with_closed_pending_sell(*, deadline: datetime) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=Mode.LIVE)
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        closed_at=NOW,
        closed_exit_price=Decimal("0.60"),
        closed_realized_pnl=Decimal("0.40"),
        confirmation_deadline=deadline,
    )
    return state


def make_service(
    state: InMemoryStateStore,
    *,
    remote_positions: list[Position],
    now: object,
) -> ReconciliationService:
    return ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader(remote_positions),
        now=now,
    )


class FakeOrdersReader:
    def __init__(self, orders: list[OrderResult] | None = None, error: Exception | None = None) -> None:
        self._orders = orders or []
        self._error = error

    def get_open_orders(self, market_id: str | None = None) -> list[OrderResult]:
        if self._error is not None:
            raise self._error
        return self._orders


class FakePositionsReader:
    def __init__(self, positions: list[Position] | None = None, error: Exception | None = None) -> None:
        self._positions = positions or []
        self._error = error

    def get_positions(self, user_address: str) -> list[Position]:
        if self._error is not None:
            raise self._error
        return self._positions


def open_order(order_id: str) -> OrderResult:
    return OrderResult(
        client_order_id=order_id,
        exchange_order_id=order_id,
        status=OrderStatus.SUBMITTED,
        accepted=True,
        message="open_order_snapshot",
        requested_size=Decimal("1"),
    )


def filled_remote_order(
    order_id: str,
    *,
    filled: str = "1",
    price: str = "0.40",
    status: OrderStatus = OrderStatus.FILLED,
) -> OrderResult:
    return OrderResult(
        client_order_id=order_id,
        exchange_order_id=order_id,
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        status=status,
        accepted=True,
        message="matched",
        requested_size=Decimal(filled),
        filled_size=Decimal(filled),
        avg_fill_price=Decimal(price),
    )


class TerminalOrderReader(FakeOrdersReader):
    def __init__(
        self,
        orders: list[OrderResult] | None = None,
        *,
        terminal: OrderResult,
    ) -> None:
        super().__init__(orders)
        self._terminal = terminal

    def get_order(
        self,
        order_id: str,
        *,
        client_order_id: str | None = None,
    ) -> OrderResult:
        return self._terminal.model_copy(
            update={"client_order_id": client_order_id or order_id}
        )


@pytest.mark.asyncio
async def test_remote_order_missing_locally_is_imported() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([open_order("0xremote0001")]),
    )
    report = await service.reconcile_startup()
    assert report.ok is True
    assert report.missing_locally == ["0xremote0001"]
    assert report.remote_open_orders == 1
    imported = await state.get_open_orders()
    assert [item.client_order_id for item in imported] == ["0xremote0001"]


@pytest.mark.asyncio
async def test_order_identity_uses_exchange_id_and_preserves_local_client_id() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    local = open_order("client-order-0001").model_copy(
        update={"exchange_order_id": "0xexchange0001"}
    )
    await state.set_order_status(local)
    remote = open_order("0xexchange0001")
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([remote]),
    )

    report = await service.reconcile_startup()

    assert report.ok is True
    assert report.missing_on_remote == []
    assert report.missing_locally == []
    reconciled = await state.get_open_orders()
    assert [item.client_order_id for item in reconciled] == ["client-order-0001"]
    assert reconciled[0].exchange_order_id == "0xexchange0001"


@pytest.mark.asyncio
async def test_local_open_order_missing_remotely_blocks_live_startup() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_order_status(open_order("0xlocal0001"))
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
    )
    report = await service.reconcile_startup()
    assert report.ok is False
    assert report.missing_on_remote == ["0xlocal0001"]


@pytest.mark.asyncio
async def test_missing_open_order_is_resolved_by_terminal_order_poll() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    local = open_order("client-order-0001").model_copy(
        update={"exchange_order_id": "0xexchange0001"}
    )
    await state.set_order_status(local)

    class TerminalOrderReader(FakeOrdersReader):
        def get_order(
            self,
            order_id: str,
            *,
            client_order_id: str | None = None,
        ) -> OrderResult:
            return local.model_copy(
                    update={
                        "client_order_id": client_order_id or order_id,
                        "status": OrderStatus.CANCELLED,
                        "filled_size": Decimal("0"),
                    }
            )

    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=TerminalOrderReader([]),
    )

    report = await service.reconcile_startup()

    assert report.ok is True
    assert report.missing_on_remote == []
    assert await state.get_open_orders() == []


@pytest.mark.asyncio
async def test_terminal_fill_poll_is_accounted_exactly_once() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    local = open_order("client-order-0001").model_copy(
        update={
            "exchange_order_id": "0xexchange0001",
            "market_id": "m1",
            "token_id": "t1",
            "side": OrderSide.BUY,
        }
    )
    await state.set_order_status(local)
    terminal = filled_remote_order("0xexchange0001", filled="2", price="0.40")
    tracker = OrderTracker(state)
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=TerminalOrderReader([], terminal=terminal),
        apply_fill=tracker.handle_order_result,
    )

    first = await service.reconcile_runtime()
    second = await service.reconcile_runtime()

    assert first.ok is True
    assert second.ok is True
    position = await state.get_position("m1", "t1")
    assert position is not None
    assert position.quantity == Decimal("2")
    checkpoints = await state.get_fill_checkpoints()
    assert len(checkpoints) == 1


@pytest.mark.asyncio
async def test_remote_position_mismatch_blocks_live_startup() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("5"),
            average_entry_price=Decimal("0.50"),
        )
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([]),
    )
    report = await service.reconcile_startup()
    assert report.ok is False
    assert "position_mismatch" in report.errors


@pytest.mark.asyncio
async def test_empty_local_positions_adopt_remote_account_truth() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    remote = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("5"),
        average_entry_price=Decimal("0.50"),
    )
    reader = FakePositionsReader([remote])
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=reader,
        funder_address="0xfunder",
        now=lambda: NOW,
    )

    report = await service.reconcile_startup()

    assert report.ok is True
    assert await state.get_positions() == [remote]
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.opened_at == NOW


@pytest.mark.asyncio
async def test_adopted_position_receives_known_market_deadline() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    remote = Position(
        market_id="condition-1",
        token_id="token-1",
        quantity=Decimal("2"),
        average_entry_price=Decimal("0.50"),
    )
    end_at = NOW + timedelta(minutes=15)
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([remote]),
        now=lambda: NOW,
        market_end_lookup=lambda market_id, token_id: (
            end_at
            if (market_id, token_id) == ("condition-1", "token-1")
            else None
        ),
        require_position_market_end=True,
        min_order_size=Decimal("1"),
    )

    report = await service.reconcile_startup()

    assert report.ok is True
    lifecycle = await state.get_position_lifecycle("condition-1", "token-1")
    assert lifecycle is not None
    assert lifecycle.market_end_at == end_at


@pytest.mark.asyncio
async def test_unknown_adopted_market_window_blocks_live_reconciliation() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    remote = Position(
        market_id="old-condition",
        token_id="old-token",
        quantity=Decimal("2"),
        average_entry_price=Decimal("0.50"),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([remote]),
        now=lambda: NOW,
        market_end_lookup=lambda market_id, token_id: None,
        require_position_market_end=True,
        min_order_size=Decimal("1"),
    )

    report = await service.reconcile_startup()

    assert report.ok is False
    assert report.errors == [
        "position_market_window_unknown:old-condition:old-token"
    ]


@pytest.mark.asyncio
async def test_adapter_read_error_blocks_live_startup() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader(error=ClobAdapterError("open orders read failed: boom")),
    )
    report = await service.reconcile_startup()
    assert report.ok is False
    assert report.errors == ["remote_open_orders_fetch_failed:ClobAdapterError"]
    assert "boom" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_dry_run_reconciliation_remains_non_blocking() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_order_status(open_order("0xlocal0001"))
    service = ReconciliationService(
        state_store=state,
        mode=Mode.DRY_RUN,
        open_orders_reader=FakeOrdersReader([]),
    )
    report = await service.reconcile_startup()
    assert report.ok is True
    assert report.missing_on_remote == ["0xlocal0001"]


@pytest.mark.asyncio
async def test_matching_positions_pass_reconciliation() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("5"),
            average_entry_price=Decimal("0.50"),
        )
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([
            Position(
                market_id="m1",
                token_id="t1",
                quantity=Decimal("5"),
                average_entry_price=Decimal("0.50"),
            )
        ]),
    )
    report = await service.reconcile_startup()
    assert report.ok is True


@pytest.mark.asyncio
async def test_runtime_reconciliation_refreshes_positions_from_exchange() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("1"),
            average_entry_price=Decimal("0.50"),
        )
    )
    remote = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("2"),
        average_entry_price=Decimal("0.45"),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([remote]),
    )

    report = await service.reconcile_runtime()

    assert report.ok is True
    assert await state.get_positions() == [remote]


@pytest.mark.asyncio
async def test_runtime_position_read_failure_does_not_erase_local_state() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    local = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("1"),
        average_entry_price=Decimal("0.50"),
    )
    await state.set_position(local)
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader(error=RuntimeError("positions unavailable")),
    )

    report = await service.reconcile_runtime()

    assert report.ok is False
    assert await state.get_positions() == [local]


@pytest.mark.asyncio
async def test_reconciliation_preserves_confirmed_local_fill_during_grace() -> None:
    state = state_with_pending_buy(
        quantity="2", deadline=NOW + timedelta(seconds=30)
    )
    report = await make_service(
        state, remote_positions=[], now=lambda: NOW
    ).reconcile_runtime()
    assert report.ok is True
    assert report.deferred_positions == ["m1:t1"]
    assert (await state.get_position("m1", "t1")).quantity == Decimal("2")


@pytest.mark.asyncio
async def test_reconciliation_fails_after_confirmation_grace() -> None:
    state = state_with_pending_buy(
        quantity="2", deadline=NOW - timedelta(seconds=1)
    )
    report = await make_service(
        state, remote_positions=[], now=lambda: NOW
    ).reconcile_runtime()
    assert report.ok is False
    assert report.errors == ["position_confirmation_timeout:m1:t1"]


@pytest.mark.asyncio
async def test_absent_remote_confirms_local_sell_to_zero() -> None:
    state = state_with_closed_pending_sell(
        deadline=NOW + timedelta(seconds=30)
    )
    report = await make_service(
        state, remote_positions=[], now=lambda: NOW
    ).reconcile_runtime()
    assert report.ok is True
    assert (await state.get_position_lifecycle("m1", "t1")).confirmation_deadline is None


@pytest.mark.asyncio
async def test_matching_remote_quantity_clears_confirmation_deadline() -> None:
    state = state_with_pending_buy(
        quantity="2", deadline=NOW + timedelta(seconds=30)
    )
    remote = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("2"),
        average_entry_price=Decimal("0.50"),
    )
    report = await make_service(
        state, remote_positions=[remote], now=lambda: NOW
    ).reconcile_runtime()
    assert report.ok is True
    assert report.deferred_positions == []
    assert (await state.get_position_lifecycle("m1", "t1")).confirmation_deadline is None


@pytest.mark.asyncio
async def test_delayed_remote_fill_is_applied_to_inventory() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    local = open_order("client-order-0001").model_copy(
        update={"exchange_order_id": "0xexchange0001"}
    )
    await state.set_order_status(local)
    tracker = OrderTracker(state)
    applied: list[OrderResult] = []

    async def apply_fill(result: OrderResult) -> None:
        outcome = await tracker.handle_order_result(result)
        if outcome.fill_applied:
            applied.append(result)

    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=TerminalOrderReader(
            [], terminal=filled_remote_order("0xexchange0001", filled="2", price="0.40")
        ),
        apply_fill=apply_fill,
    )
    report = await service.reconcile_startup()

    assert report.ok is True
    assert len(applied) == 1
    position = await state.get_position("m1", "t1")
    assert position is not None and position.quantity == Decimal("2")
    assert len(await state.get_fill_checkpoints()) == 1


@pytest.mark.asyncio
async def test_runtime_partial_fill_applies_once_across_cycles() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    remote = filled_remote_order(
        "0xfill00001", filled="3", price="0.50", status=OrderStatus.PARTIALLY_FILLED
    )
    applied: list[OrderResult] = []

    async def apply_fill(result: OrderResult) -> None:
        outcome = await OrderTracker(state).handle_order_result(result)
        if outcome.fill_applied:
            applied.append(result)

    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([remote]),
        apply_fill=apply_fill,
        now=lambda: NOW,
    )
    await service.reconcile_runtime()
    assert len(applied) == 1

    await service.reconcile_runtime()
    assert len(applied) == 1
    assert (await state.get_position("m1", "t1")).quantity == Decimal("3")


@pytest.mark.asyncio
async def test_missing_fill_recorder_blocks_live_when_fills_exist() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    remote = filled_remote_order("0xfill00001", filled="3", price="0.50")
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([remote]),
    )
    report = await service.reconcile_runtime()

    assert report.ok is False
    assert "fill_recorder_not_configured" in report.errors
    assert await state.get_positions() == []


@pytest.mark.asyncio
async def test_dust_residue_does_not_keep_reporting_a_divergence() -> None:
    """
    Runtime reconciliation must not raise an incident every pass over inventory
    the venue will not let anyone trade.

    A live round trip on 2026-08-26 left 0.005 shares against a venue minimum
    of 5. Each pass deferred, then timed out, and the repeated incidents
    halted trading. The residue is real but untradeable, so it is retired.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("0.005"),
            average_entry_price=Decimal("0.36"),
        )
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        market_end_at=NOW + timedelta(minutes=15),
        confirmation_deadline=NOW + timedelta(seconds=30),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([]),
        min_order_size=Decimal("5"),
    )

    report = await service.reconcile_runtime()

    assert report.deferred_positions == []
    assert not any("position_confirmation_timeout" in e for e in report.errors)
    assert await state.get_position("m1", "t1") is None

    # A second pass must stay quiet rather than rediscovering it.
    again = await service.reconcile_runtime()
    assert again.deferred_positions == []


@pytest.mark.asyncio
async def test_reconciliation_uses_the_venue_minimum_for_the_dust_threshold() -> None:
    """
    The order builder reads minimum_order_size per market; reconciliation has
    to use the same floor or it will keep flagging inventory nobody can sell.

    Config says 5, this market says 50. A 20-share residue clears the config
    floor but not the venue's, so without a per-market lookup it would defer,
    time out, and halt exactly as the 0.005 residue did on 2026-08-26.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="thin",
            token_id="t1",
            quantity=Decimal("20"),
            average_entry_price=Decimal("0.40"),
        )
    )
    state._lifecycles[("thin", "t1")] = PositionLifecycle(
        market_id="thin",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        market_end_at=NOW + timedelta(minutes=15),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([]),
        min_order_size=Decimal("5"),
        min_size_provider=lambda market_id: Decimal("50"),
    )

    report = await service.reconcile_runtime()

    assert report.deferred_positions == []
    assert not any("position_confirmation_timeout" in e for e in report.errors)
    assert await state.get_position("thin", "t1") is None


@pytest.mark.asyncio
async def test_settled_market_position_does_not_block_live_startup() -> None:
    """
    A position whose market has already ended is not a divergence.

    The data API is queried with redeemable=false, so the moment a market
    resolves its position disappears from the remote read while local still
    holds it. Startup reconciliation compared the two by strict equality and
    reported position_mismatch, which failed preflight and blocked live start
    -- every time a position was held into resolution, which for 15-minute
    markets is routine. It blocked the 09:16 preflight on 2026-08-26.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="ended",
            token_id="t1",
            quantity=Decimal("5"),
            average_entry_price=Decimal("0.60"),
        )
    )
    state._lifecycles[("ended", "t1")] = PositionLifecycle(
        market_id="ended",
        token_id="t1",
        opened_at=NOW - timedelta(hours=1),
        last_fill_at=NOW - timedelta(hours=1),
        market_end_at=NOW - timedelta(minutes=30),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([]),
        now=lambda: NOW,
        min_order_size=Decimal("5"),
    )

    report = await service.reconcile_startup()

    assert "position_mismatch" not in report.errors
    assert report.ok is True


@pytest.mark.asyncio
async def test_live_market_position_mismatch_still_blocks_live_startup() -> None:
    """Settlement handling must not weaken the guard for a tradeable market."""

    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="live",
            token_id="t1",
            quantity=Decimal("5"),
            average_entry_price=Decimal("0.60"),
        )
    )
    state._lifecycles[("live", "t1")] = PositionLifecycle(
        market_id="live",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        market_end_at=NOW + timedelta(minutes=10),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([]),
        now=lambda: NOW,
        min_order_size=Decimal("5"),
    )

    report = await service.reconcile_startup()

    assert "position_mismatch" in report.errors
    assert report.ok is False


@pytest.mark.asyncio
async def test_sellable_uses_the_same_floor_as_dust_retirement() -> None:
    """
    merge_authoritative_positions retires dust at max(config, venue), but the
    sellable set that drives position_market_window_unknown used the config
    minimum alone.

    A remote position between the two is retired as dust and simultaneously
    counted sellable, so the error is raised on every pass, ok stays False,
    and the runtime halts over inventory it already decided nobody can trade.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    remote = Position(
        market_id="thin",
        token_id="t1",
        quantity=Decimal("2"),
        average_entry_price=Decimal("0.40"),
    )
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([remote]),
        market_end_lookup=lambda market_id, token_id: None,
        require_position_market_end=True,
        min_order_size=Decimal("1"),
        min_size_provider=lambda market_id: Decimal("5"),
    )

    report = await service.reconcile_runtime()

    assert not any("position_market_window_unknown" in e for e in report.errors), (
        f"unexpected errors: {report.errors}"
    )


@pytest.mark.asyncio
async def test_a_failing_venue_lookup_falls_back_to_the_configured_floor() -> None:
    """
    Thresholds are resolved before the state lock is taken, so a venue lookup
    failure has to degrade there. It must fall back to the configured minimum
    rather than disabling dust handling, which would restore the halt loop.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)

    def broken(market_id: str) -> Decimal:
        raise RuntimeError("venue lookup failed")

    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader([]),
        positions_reader=FakePositionsReader([]),
        min_order_size=Decimal("5"),
        min_size_provider=broken,
    )

    assert service._minimum_tradeable_size("anything") == Decimal("5")
