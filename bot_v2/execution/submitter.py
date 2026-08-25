"""Order submission boundary."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime

from clients.auth import is_live_trading_enabled
from clients.clob_client import (
    ClobAdapterError,
    ClobClientAdapter,
    ClobUncertainOutcomeError,
)
from config.schema import AppConfig, Mode
from models.order import (
    CancelIntent,
    CancelOutcome,
    CancelResult,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

#: Upper bound on remembered client order ids. Market making mints a new id on
#: every quote refresh, so an unbounded set would grow for the life of the
#: process. The window only needs to outlive in-flight duplicates.
SUBMITTED_ID_HISTORY = 10_000


class OrderSubmitter:
    """Submit orders in dry-run or live mode, guarding duplicate sends."""

    def __init__(
        self,
        *,
        config: AppConfig,
        clob_client: ClobClientAdapter,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self._config = config
        self._clob_client = clob_client
        self._circuit_breaker = circuit_breaker
        self._submitted_ids: OrderedDict[str, None] = OrderedDict()

    async def submit(self, order: OrderRequest) -> OrderResult:
        """Submit one order request and return typed result."""

        if order.client_order_id in self._submitted_ids:
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_failure()
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.REJECTED,
                accepted=False,
                message="duplicate_client_order_id",
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
            )

        live_trading_enabled = is_live_trading_enabled(self._config)
        notional = order.price * order.size
        if (
            live_trading_enabled
            and notional > self._config.execution.max_live_order_notional
        ):
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_failure()
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.REJECTED,
                accepted=False,
                message=(
                    f"order notional {notional} exceeds live notional cap "
                    f"{self._config.execution.max_live_order_notional}"
                ),
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
            )

        self._remember_submission(order.client_order_id)
        started = datetime.now(tz=UTC)
        logger.info(
            "submitting order",
            extra={
                "component": "submitter",
                "event_type": "order_submitted",
                "market_id": order.market_id,
                "token_id": order.token_id,
                "strategy_name": order.strategy_name,
                "signal_id": order.signal_id,
                "client_order_id": order.client_order_id,
                "mode": self._config.bot.mode.value,
            },
        )

        if self._config.bot.mode == Mode.DRY_RUN or not live_trading_enabled:
            latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_success()
            if order.post_only:
                # A post-only quote sits behind the touch by construction, so
                # simulating an instant fill at its own limit price would be a
                # fiction -- and the most flattering one available. Dry run
                # reports it as resting instead. Whether such a quote would
                # actually trade is a queue-position question that only the
                # backtester models.
                return OrderResult(
                    client_order_id=order.client_order_id,
                    exchange_order_id=f"sim-{order.client_order_id}",
                    market_id=order.market_id,
                    token_id=order.token_id,
                    side=order.side,
                    status=OrderStatus.SUBMITTED,
                    accepted=True,
                    message="simulated_resting_quote",
                    signal_id=order.signal_id,
                    strategy_name=order.strategy_name,
                    requested_size=order.size,
                    latency_ms=latency_ms,
                )
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.SIMULATED,
                accepted=True,
                message="simulated_submission",
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
                filled_size=order.size,
                avg_fill_price=order.price,
                latency_ms=latency_ms,
            )

        try:
            result = await asyncio.to_thread(self._clob_client.submit_order, order)
        except ClobUncertainOutcomeError as exc:
            latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_failure()
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.UNKNOWN,
                accepted=False,
                message=str(exc),
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
                latency_ms=latency_ms,
            )
        except ClobAdapterError as exc:
            latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_failure()
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.FAILED,
                accepted=False,
                message=str(exc),
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
                latency_ms=latency_ms,
            )

        if self._circuit_breaker is not None:
            self._circuit_breaker.record_success()
        result.market_id = order.market_id
        result.token_id = order.token_id
        result.side = order.side
        result.signal_id = order.signal_id
        result.strategy_name = order.strategy_name
        return result

    def _remember_submission(self, client_order_id: str) -> None:
        """Record a submitted id, evicting the oldest beyond the window."""

        self._submitted_ids[client_order_id] = None
        while len(self._submitted_ids) > SUBMITTED_ID_HISTORY:
            self._submitted_ids.popitem(last=False)

    async def cancel_order(self, intent: CancelIntent) -> CancelResult:
        """
        Cancel exactly one resting order.

        Dry run reports a simulated cancellation so quote lifecycles behave
        identically in both modes. Live mode delegates to the adapter, which
        distinguishes an order that already left the book from a genuine
        refusal.
        """

        if self._config.bot.mode == Mode.DRY_RUN or not is_live_trading_enabled(
            self._config
        ):
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=intent.exchange_order_id,
                outcome=CancelOutcome.SIMULATED,
                message="simulated_cancellation",
            )
        try:
            return await asyncio.to_thread(
                self._clob_client.cancel_resting_order, intent
            )
        except ClobAdapterError as exc:
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=intent.exchange_order_id,
                outcome=CancelOutcome.FAILED,
                message=str(exc),
            )

    async def cancel_all_open_orders(self) -> bool:
        """Cancel every known open order through the adapter."""

        try:
            return await asyncio.to_thread(self._clob_client.cancel_all)
        except ClobAdapterError as exc:
            logger.error(
                "cancel-all failed",
                extra={
                    "component": "submitter",
                    "event_type": "cancel_all_failed",
                    "reason": str(exc),
                },
            )
            raise
