from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from clients.clob_client import ClobAdapterError
from config.schema import Mode
from models.order import OrderResult, OrderStatus
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
                    "status": OrderStatus.FILLED,
                    "filled_size": Decimal("1"),
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
    )

    report = await service.reconcile_startup()

    assert report.ok is True
    assert await state.get_positions() == [remote]


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
