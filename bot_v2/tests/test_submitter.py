from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from clients.clob_client import (
    ClobClientAdapter,
    ClobPostOnlyCrossError,
    ClobUncertainOutcomeError,
)
from config.schema import AppConfig, Mode
from execution.submitter import OrderSubmitter
from models.order import (
    CancelIntent,
    CancelOutcome,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
)
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
async def test_dry_run_returns_simulated_confirmed_fill() -> None:
    adapter = RecordingAdapter()
    config = AppConfig(bot={"mode": Mode.DRY_RUN})
    submitter = OrderSubmitter(config=config, clob_client=adapter)

    result = await submitter.submit(buy_request(size="2", price="0.45"))

    assert result.status == OrderStatus.SIMULATED
    assert result.accepted is True
    assert result.filled_size == Decimal("2")
    assert result.avg_fill_price == Decimal("0.45")


@pytest.mark.asyncio
async def test_dry_run_order_above_live_cap_is_still_simulated() -> None:
    adapter = RecordingAdapter()
    config = AppConfig(
        bot={"mode": Mode.DRY_RUN},
        execution={"max_live_order_notional": "1"},
    )
    submitter = OrderSubmitter(config=config, clob_client=adapter)

    result = await submitter.submit(buy_request(size="5", price="0.50"))

    assert result.status == OrderStatus.SIMULATED
    assert result.accepted is True
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
async def test_live_submission_liquidity_is_derived_from_post_only() -> None:
    """
    Fee accounting reads OrderResult.liquidity to pick maker vs taker fees.
    A post-only order that crossed would be rejected by the exchange rather
    than filled, so submitted post_only is a sound signal that any resulting
    fill was a maker fill -- and the submitter must set it even though the
    adapter's own OrderResult does not know about the originating order.
    """

    adapter = RecordingAdapter()
    submitter = make_submitter(adapter)

    maker_order = OrderRequest(
        client_order_id="maker-order-001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.49"),
        size=Decimal("1"),
        time_in_force=OrderTimeInForce.GTC,
        post_only=True,
    )
    taker_order = OrderRequest(
        client_order_id="taker-order-001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.51"),
        size=Decimal("1"),
        time_in_force=OrderTimeInForce.GTC,
        post_only=False,
    )

    maker_result = await submitter.submit(maker_order)
    taker_result = await submitter.submit(taker_order)

    assert maker_result.liquidity == "maker"
    assert taker_result.liquidity == "taker"


@pytest.mark.asyncio
async def test_live_sdk_submission_runs_outside_event_loop_thread() -> None:
    main_thread_id = threading.get_ident()

    class ThreadRecordingAdapter(RecordingAdapter):
        submission_thread_id: int | None = None

        def submit_order(self, order: OrderRequest) -> OrderResult:
            self.submission_thread_id = threading.get_ident()
            return super().submit_order(order)

    adapter = ThreadRecordingAdapter()
    submitter = make_submitter(adapter)

    result = await submitter.submit(buy_request())

    assert result.accepted is True
    assert adapter.submission_thread_id is not None
    assert adapter.submission_thread_id != main_thread_id


@pytest.mark.asyncio
async def test_cancel_all_open_orders_delegates_to_adapter() -> None:
    adapter = RecordingAdapter()
    submitter = make_submitter(adapter)
    cancelled = await submitter.cancel_all_open_orders()
    assert cancelled is True
    assert adapter.cancel_all_calls == 1


@pytest.mark.asyncio
async def test_dry_run_post_only_quote_rests_instead_of_filling() -> None:
    submitter = OrderSubmitter(
        config=AppConfig(bot={"mode": Mode.DRY_RUN}),
        clob_client=ClobClientAdapter.disabled(),
    )
    order = OrderRequest(
        client_order_id="quote-000000001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.49"),
        size=Decimal("100"),
        time_in_force=OrderTimeInForce.GTC,
        post_only=True,
    )

    result = await submitter.submit(order)

    # No fantasy fill: a resting quote reports zero filled size.
    assert result.status == OrderStatus.SUBMITTED
    assert result.accepted is True
    assert result.filled_size == Decimal("0")
    assert result.avg_fill_price is None
    assert result.exchange_order_id == "sim-quote-000000001"
    assert result.liquidity == "maker"


@pytest.mark.asyncio
async def test_dry_run_taker_order_still_simulates_a_fill() -> None:
    submitter = OrderSubmitter(
        config=AppConfig(bot={"mode": Mode.DRY_RUN}),
        clob_client=ClobClientAdapter.disabled(),
    )
    order = OrderRequest(
        client_order_id="taker-000000001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.51"),
        size=Decimal("10"),
        time_in_force=OrderTimeInForce.FOK,
    )

    result = await submitter.submit(order)

    assert result.status == OrderStatus.SIMULATED
    assert result.filled_size == Decimal("10")
    assert result.liquidity == "taker"


@pytest.mark.asyncio
async def test_dry_run_cancellation_is_simulated_and_terminal() -> None:
    submitter = OrderSubmitter(
        config=AppConfig(bot={"mode": Mode.DRY_RUN}),
        clob_client=ClobClientAdapter.disabled(),
    )

    result = await submitter.cancel_order(
        CancelIntent(
            client_order_id="quote-000000001",
            exchange_order_id="sim-quote-000000001",
            market_id="m1",
            token_id="t1",
            side=OrderSide.BUY,
            reason="quote_stale",
        )
    )

    assert result.outcome == CancelOutcome.SIMULATED
    assert result.terminal is True


@pytest.mark.asyncio
async def test_submitted_id_history_is_bounded() -> None:
    from execution.submitter import SUBMITTED_ID_HISTORY

    submitter = OrderSubmitter(
        config=AppConfig(bot={"mode": Mode.DRY_RUN}),
        clob_client=ClobClientAdapter.disabled(),
    )
    for index in range(SUBMITTED_ID_HISTORY + 50):
        submitter._remember_submission(f"order-{index:08d}")

    assert len(submitter._submitted_ids) == SUBMITTED_ID_HISTORY
    # The oldest ids are evicted, the newest retained.
    assert "order-00000000" not in submitter._submitted_ids
    assert f"order-{SUBMITTED_ID_HISTORY + 49:08d}" in submitter._submitted_ids


class PostOnlyCrossAdapter(RecordingAdapter):
    def submit_order(self, order: OrderRequest) -> OrderResult:
        self.submitted.append(order)
        raise ClobPostOnlyCrossError(
            "order submission rejected:http_400:invalid post-only order: "
            "order crosses book"
        )


@pytest.mark.asyncio
async def test_post_only_cross_is_rejected_without_tripping_the_breaker() -> None:
    """
    A post-only order refused for crossing is the venue doing its job, not a
    fault. The book moved between the snapshot the price was built from and
    the moment it arrived, so the order would have taken liquidity and was
    correctly refused.

    Counting that toward the circuit breaker halts the bot for quoting, which
    with entry_style: maker is every entry it makes. Two of these arrived in
    the first live session on 2026-08-26 and would have halted it at five.
    """

    adapter = PostOnlyCrossAdapter()
    submitter = make_submitter(adapter)

    for index in range(4):
        # Under the notional cap so the request reaches the adapter, and with a
        # distinct id so the duplicate guard does not record the failure first.
        request = buy_request(size="2", price="0.50")
        request = request.model_copy(update={"client_order_id": f"cross-{index:04d}"})
        result = await submitter.submit(request)
        assert result.status == OrderStatus.REJECTED
        assert result.accepted is False
        assert "post-only" in result.message

    # Threshold is 3; a genuine fault sequence would have tripped it by now.
    state = submitter._circuit_breaker.state()
    assert state.tripped is False
    assert state.failures_in_window == 0
