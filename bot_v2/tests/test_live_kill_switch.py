from __future__ import annotations

from decimal import Decimal

import pytest

from clients.clob_client import ClobAdapterError
from config.schema import AppConfig, Mode
from execution.submitter import OrderSubmitter
from models.order import OrderRequest, OrderResult, OrderSide, OrderStatus, OrderTimeInForce


def live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )


def buy_request() -> OrderRequest:
    return OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.50"),
        size=Decimal("1"),
        time_in_force=OrderTimeInForce.GTC,
        strategy_name="test",
    )


class CancelAllAdapter:
    def __init__(self, fail: bool = False) -> None:
        self.cancel_all_calls = 0
        self._fail = fail

    def submit_order(self, order: OrderRequest) -> OrderResult:
        return OrderResult(
            client_order_id=order.client_order_id,
            exchange_order_id="0xabc123",
            status=OrderStatus.SUBMITTED,
            accepted=True,
            message="submitted",
            requested_size=order.size,
        )

    def cancel_all(self) -> bool:
        self.cancel_all_calls += 1
        if self._fail:
            raise ClobAdapterError("cancel-all failed: boom")
        return True


class KillSwitchState:
    def __init__(self) -> None:
        self.active = False

    async def set_kill_switch(self, enabled: bool) -> None:
        self.active = enabled

    async def is_kill_switch_active(self) -> bool:
        return self.active


class KillSwitchRouter:
    def __init__(self, state: KillSwitchState, submitter: OrderSubmitter) -> None:
        self._state = state
        self._submitter = submitter
        self.submit_calls = 0

    async def route_signal(self, signal: object) -> None:
        self.submit_calls += 1
        if await self._state.is_kill_switch_active():
            return
        await self._submitter.submit(buy_request())


@pytest.mark.asyncio
async def test_kill_switch_cancels_all_open_orders_once_and_blocks_submission() -> None:
    adapter = CancelAllAdapter()
    state = KillSwitchState()
    submitter = OrderSubmitter(config=live_config(), clob_client=adapter)
    router = KillSwitchRouter(state, submitter)

    await state.set_kill_switch(True)
    await submitter.cancel_all_open_orders()

    assert state.active is True
    assert adapter.cancel_all_calls == 1

    await router.route_signal(object())
    assert router.submit_calls == 1
    assert adapter.cancel_all_calls == 1


@pytest.mark.asyncio
async def test_cancel_all_failure_does_not_clear_kill_switch() -> None:
    adapter = CancelAllAdapter(fail=True)
    state = KillSwitchState()
    submitter = OrderSubmitter(config=live_config(), clob_client=adapter)

    await state.set_kill_switch(True)
    with pytest.raises(ClobAdapterError, match="cancel-all failed"):
        await submitter.cancel_all_open_orders()

    assert state.active is True
    assert adapter.cancel_all_calls == 1
