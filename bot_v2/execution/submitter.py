"""Order submission boundary."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from clients.auth import is_live_trading_enabled
from clients.clob_client import (
    ClobAdapterError,
    ClobClientAdapter,
    ClobUncertainOutcomeError,
)
from config.schema import AppConfig, Mode
from models.order import OrderRequest, OrderResult, OrderStatus
from risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


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
        self._submitted_ids: set[str] = set()

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

        notional = order.price * order.size
        if notional > self._config.execution.max_live_order_notional:
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

        self._submitted_ids.add(order.client_order_id)
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

        if self._config.bot.mode == Mode.DRY_RUN or not is_live_trading_enabled(self._config):
            latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_success()
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
                latency_ms=latency_ms,
            )

        try:
            result = self._clob_client.submit_order(order)
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
