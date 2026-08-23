"""Typed adapter around the Polymarket CLOB V2 SDK."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from clients.auth import ClobCredentials
from config.schema import AppConfig
from models.order import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
)

logger = logging.getLogger(__name__)


class ClobAdapterError(RuntimeError):
    """Raised when the V2 adapter cannot satisfy a requested operation."""


class ClobUncertainOutcomeError(ClobAdapterError):
    """Raised when a submission outcome is unknown (timeout or transport failure)."""


class CollateralStatus(BaseModel):
    """Normalized pUSD collateral balance and allowance."""

    model_config = ConfigDict(extra="forbid")

    balance: Decimal = Field(ge=Decimal("0"))
    allowance: Decimal = Field(ge=Decimal("0"))


class ClobClientAdapter:
    """
    Polymarket CLOB V2 boundary.

    All SDK interaction is centralized here. Production code calls explicit
    V2 methods; tests inject a fake SDK client through ``sdk_factory``.
    """

    def __init__(
        self,
        client: Any,
        *,
        config: AppConfig,
        allow_trading: bool,
        read_only: bool = True,
    ) -> None:
        self._client = client
        self._config = config
        self._allow_trading = allow_trading
        self._read_only = read_only

    @classmethod
    def disabled(cls) -> "ClobClientAdapter":
        """Create a safe no-op adapter used outside explicit live mode."""

        return cls(
            _DisabledClobClient(),
            config=AppConfig(),
            allow_trading=False,
            read_only=True,
        )

    @classmethod
    def from_v2(
        cls,
        *,
        config: AppConfig,
        credentials: ClobCredentials,
        sdk_factory: Callable[..., Any] | None = None,
    ) -> "ClobClientAdapter":
        """Build a live adapter from explicit V2 constructor arguments."""

        if sdk_factory is None:
            from py_clob_client_v2 import ClobClient as sdk_factory

        if not credentials.private_key:
            raise ClobAdapterError("live mode requires a private key")
        if not credentials.has_l2:
            raise ClobAdapterError("live mode requires complete L2 API credentials")
        if not credentials.proxy_address:
            raise ClobAdapterError("live mode requires a funder address")

        from py_clob_client_v2 import ApiCreds

        client = sdk_factory(
            host=config.exchange.clob_host,
            chain_id=config.exchange.chain_id,
            key=credentials.private_key,
            creds=ApiCreds(
                api_key=credentials.api_key or "",
                api_secret=credentials.secret or "",
                api_passphrase=credentials.passphrase or "",
            ),
            signature_type=config.exchange.signature_type,
            funder=credentials.proxy_address,
        )
        return cls(
            client,
            config=config,
            allow_trading=True,
            read_only=False,
        )

    def healthcheck(self) -> bool:
        """Return True only when the CLOB health endpoint answers OK."""

        try:
            raw = self._client.get_ok()
        except Exception as exc:
            raise ClobAdapterError(f"clob healthcheck failed: {exc}") from exc
        if raw != "OK":
            raise ClobAdapterError(f"clob healthcheck returned unexpected response: {raw!r}")
        return True

    def get_open_orders(self, market_id: str | None = None) -> list[OrderResult]:
        """Fetch open orders through the explicit V2 method."""

        from py_clob_client_v2 import OpenOrderParams

        params = OpenOrderParams(market=market_id) if market_id else None
        try:
            raw = self._client.get_open_orders(params=params)
        except Exception as exc:
            raise ClobAdapterError(f"open orders read failed: {exc}") from exc
        if not isinstance(raw, list):
            raise ClobAdapterError(f"open orders response is not a list: {type(raw).__name__}")

        results: list[OrderResult] = []
        for row in raw:
            if not isinstance(row, dict):
                raise ClobAdapterError(f"open orders row is not an object: {type(row).__name__}")
            order_id = str(row.get("id") or "")
            if not order_id:
                raise ClobAdapterError("open orders row missing order id")
            try:
                requested = Decimal(str(row.get("original_size") or "0"))
                filled = Decimal(str(row.get("size_matched") or "0"))
            except Exception as exc:
                raise ClobAdapterError(f"open orders row has invalid sizes: {exc}") from exc
            results.append(
                OrderResult(
                    client_order_id=order_id,
                    exchange_order_id=order_id,
                    market_id=str(row.get("market")) if row.get("market") else None,
                    token_id=str(row.get("asset_id")) if row.get("asset_id") else None,
                    status=OrderStatus.SUBMITTED,
                    accepted=True,
                    message="open_order_snapshot",
                    requested_size=requested,
                    filled_size=filled,
                )
            )
        return results

    def get_collateral_status(self) -> CollateralStatus:
        """Read pUSD balance and allowance through the explicit V2 method."""

        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        try:
            raw = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        except Exception as exc:
            raise ClobAdapterError(f"collateral read failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise ClobAdapterError(f"collateral response is not an object: {type(raw).__name__}")
        try:
            balance = Decimal(str(raw.get("balance") or "0"))
            allowance = Decimal(str(raw.get("allowance") or "0"))
        except Exception as exc:
            raise ClobAdapterError(f"collateral response has invalid numbers: {exc}") from exc
        return CollateralStatus(balance=balance, allowance=allowance)

    def submit_order(self, order: OrderRequest) -> OrderResult:
        """Sign and submit one order through explicit V2 methods."""

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order submission disabled in current mode")

        notional = order.price * order.size
        if notional > self._config.execution.max_live_order_notional:
            raise ClobAdapterError(
                f"order notional {notional} exceeds live notional cap "
                f"{self._config.execution.max_live_order_notional}"
            )

        from py_clob_client_v2 import OrderArgs, OrderType, Side

        side = Side.BUY if order.side == OrderSide.BUY else Side.SELL
        order_type = {
            OrderTimeInForce.GTC: OrderType.GTC,
            OrderTimeInForce.IOC: OrderType.FAK,
            OrderTimeInForce.FOK: OrderType.FOK,
        }[order.time_in_force]
        args = OrderArgs(
            token_id=order.token_id,
            price=float(str(order.price)),
            size=float(str(order.size)),
            side=side,
        )

        started = datetime.now(tz=UTC)
        try:
            signed = self._client.create_order(args)
            raw = self._client.post_order(signed, order_type=order_type)
        except ClobAdapterError:
            raise
        except Exception as exc:
            raise ClobUncertainOutcomeError(f"order submission outcome unknown: {exc}") from exc
        latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)

        if not isinstance(raw, dict):
            raise ClobAdapterError(f"submission response is not an object: {type(raw).__name__}")
        exchange_order_id = str(raw.get("orderID") or raw.get("order_id") or "")
        if not exchange_order_id:
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.REJECTED,
                accepted=False,
                message="submission_response_missing_order_id",
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
                latency_ms=latency_ms,
            )
        return OrderResult(
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            status=OrderStatus.SUBMITTED,
            accepted=True,
            message="submitted",
            signal_id=order.signal_id,
            strategy_name=order.strategy_name,
            requested_size=order.size,
            latency_ms=latency_ms,
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel one order through the explicit V2 method."""

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order cancellation disabled in current mode")
        from py_clob_client_v2 import OrderPayload

        try:
            raw = self._client.cancel_order(OrderPayload(orderID=order_id))
        except Exception as exc:
            raise ClobAdapterError(f"order cancellation failed: {exc}") from exc
        if isinstance(raw, dict):
            return bool(raw.get("success", True))
        return True

    def cancel_all(self) -> bool:
        """Cancel every open order through the explicit V2 method."""

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order cancellation disabled in current mode")
        try:
            raw = self._client.cancel_all()
        except Exception as exc:
            raise ClobAdapterError(f"cancel-all failed: {exc}") from exc
        if isinstance(raw, dict):
            return bool(raw.get("success", True))
        return True


class _DisabledClobClient:
    """No-op client used when live SDK construction is intentionally skipped."""

    def get_open_orders(self, params: Any = None) -> list[dict[str, Any]]:
        return []
