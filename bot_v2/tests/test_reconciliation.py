from __future__ import annotations

from decimal import Decimal

import pytest

from clients.clob_client import ClobAdapterError
from config.schema import Mode
from models.order import OrderResult, OrderStatus
from models.position import Position
from state.reconciliation import ReconciliationService
from state.store import InMemoryStateStore


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
async def test_adapter_read_error_blocks_live_startup() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    service = ReconciliationService(
        state_store=state,
        mode=Mode.LIVE,
        open_orders_reader=FakeOrdersReader(error=ClobAdapterError("open orders read failed: boom")),
    )
    report = await service.reconcile_startup()
    assert report.ok is False
    assert any("boom" in error for error in report.errors)


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
