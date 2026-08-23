from __future__ import annotations

from decimal import Decimal

import pytest

from clients.clob_client import ClobUncertainOutcomeError
from config.schema import AppConfig, Mode
from execution.submitter import OrderSubmitter
from models.order import OrderRequest, OrderResult, OrderSide, OrderStatus, OrderTimeInForce
from risk.circuit_breaker import CircuitBreaker


def live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "max_live_order_notional": "2",
        },
    )


def buy_request(*, size: str = "1", price: str = "0.50") -> OrderRequest:
    return OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal(price),
        size=Decimal(size),
        time_in_force=OrderTimeInForce.GTC,
        strategy_name="test",
    )


class RecordingAdapter:
    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.cancel_all_calls = 0

    def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submitted.append(order)
        return OrderResult(
            client_order_id=order.client_order_id,
            exchange_order_id="0xabc123",
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            status=OrderStatus.SUBMITTED,
            accepted=True,
            message="submitted",
            requested_size=order.size,
        )

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True

    def cancel_all(self) -> bool:
        self.cancel_all_calls += 1
        return True


class TimeoutAdapter(RecordingAdapter):
    def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submitted.append(order)
        raise ClobUncertainOutcomeError("order submission outcome unknown: timeout")


def make_submitter(adapter: RecordingAdapter) -> OrderSubmitter:
    return OrderSubmitter(
        config=live_config(),
        clob_client=adapter,
        circuit_breaker=CircuitBreaker(
            failure_threshold=3,
            window_seconds=60,
            cooldown_seconds=120,
        ),
    )


@pytest.mark.asyncio
async def test_live_order_above_notional_cap_is_rejected_before_adapter_call() -> None:
    adapter = RecordingAdapter()
    submitter = make_submitter(adapter)
    result = await submitter.submit(buy_request(size="5", price="0.50"))
    assert result.status == OrderStatus.REJECTED
    assert result.accepted is False
    assert "notional cap" in (result.message or "")
    assert adapter.submitted == []


@pytest.mark.asyncio
async def test_uncertain_submit_outcome_is_not_retried() -> None:
    adapter = TimeoutAdapter()
    submitter = make_submitter(adapter)
    first = await submitter.submit(buy_request())
    assert first.status == OrderStatus.UNKNOWN
    assert first.accepted is False
    assert "timeout" in (first.message or "")
    assert len(adapter.submitted) == 1

    second = await submitter.submit(buy_request())
    assert second.status == OrderStatus.REJECTED
    assert second.message == "duplicate_client_order_id"
    assert len(adapter.submitted) == 1


@pytest.mark.asyncio
async def test_successful_live_submit_reaches_adapter_once() -> None:
    adapter = RecordingAdapter()
    submitter = make_submitter(adapter)
    result = await submitter.submit(buy_request())
    assert result.status == OrderStatus.SUBMITTED
    assert result.accepted is True
    assert len(adapter.submitted) == 1
    assert adapter.submitted[0].client_order_id == "test-order-0001"


@pytest.mark.asyncio
async def test_cancel_all_open_orders_delegates_to_adapter() -> None:
    adapter = RecordingAdapter()
    submitter = make_submitter(adapter)
    cancelled = await submitter.cancel_all_open_orders()
    assert cancelled is True
    assert adapter.cancel_all_calls == 1
