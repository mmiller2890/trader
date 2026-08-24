"""Typed adapter around the Polymarket CLOB V2 SDK."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from clients.auth import ClobCredentials, effective_funder_address
from config.schema import AppConfig
from models.order import (
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderTimeInForce,
)

logger = logging.getLogger(__name__)
FIXED_SIX_SCALE = Decimal("1000000")


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
        read_only: bool = False,
    ) -> "ClobClientAdapter":
        """Build a live adapter from explicit V2 constructor arguments."""

        if sdk_factory is None:
            from py_clob_client_v2 import ClobClient as sdk_factory

        if not credentials.private_key:
            raise ClobAdapterError("live mode requires a private key")
        if not credentials.has_l2:
            raise ClobAdapterError("live mode requires complete L2 API credentials")
        funder_address = effective_funder_address(config, credentials)
        if not funder_address:
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
            funder=funder_address,
        )
        return cls(
            client,
            config=config,
            allow_trading=not read_only,
            read_only=read_only,
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
                requested = _fixed_six(row.get("original_size"))
                filled = _fixed_six(row.get("size_matched"))
                price = Decimal(str(row.get("price") or "0"))
            except Exception as exc:
                raise ClobAdapterError(f"open orders row has invalid sizes: {exc}") from exc
            raw_side = str(row.get("side") or "").upper()
            side = {
                "BUY": OrderSide.BUY,
                "SELL": OrderSide.SELL,
            }.get(raw_side)
            results.append(
                OrderResult(
                    client_order_id=order_id,
                    exchange_order_id=order_id,
                    market_id=str(row.get("market")) if row.get("market") else None,
                    token_id=str(row.get("asset_id")) if row.get("asset_id") else None,
                    side=side,
                    status=(
                        OrderStatus.PARTIALLY_FILLED
                        if filled > 0
                        else OrderStatus.SUBMITTED
                    ),
                    accepted=True,
                    message="open_order_snapshot",
                    requested_size=requested,
                    filled_size=filled,
                    avg_fill_price=price if filled > 0 and price > 0 else None,
                )
            )
        return results

    def get_order(
        self,
        order_id: str,
        *,
        client_order_id: str | None = None,
    ) -> OrderResult:
        """Poll one order and normalize its latest exchange lifecycle state."""

        try:
            raw = self._client.get_order(order_id)
        except Exception as exc:
            raise ClobAdapterError(f"order read failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise ClobAdapterError(
                f"order response is not an object: {type(raw).__name__}"
            )
        exchange_order_id = str(raw.get("id") or order_id)
        exchange_status = str(raw.get("status") or "").upper().removeprefix(
            "ORDER_STATUS_"
        )
        try:
            requested = _fixed_six(raw.get("original_size"))
            filled = _fixed_six(raw.get("size_matched"))
            price = Decimal(str(raw.get("price") or "0"))
        except Exception as exc:
            raise ClobAdapterError(f"order response has invalid numbers: {exc}") from exc
        if requested <= 0:
            raise ClobAdapterError("order response has non-positive original size")

        if exchange_status == "LIVE":
            status = (
                OrderStatus.PARTIALLY_FILLED
                if filled > 0
                else OrderStatus.SUBMITTED
            )
        elif exchange_status == "MATCHED":
            status = (
                OrderStatus.FILLED
                if filled >= requested
                else OrderStatus.PARTIALLY_FILLED
            )
        elif exchange_status in {"CANCELED", "CANCELED_MARKET_RESOLVED"}:
            status = OrderStatus.CANCELLED
        elif exchange_status == "INVALID":
            status = OrderStatus.REJECTED
        else:
            raise ClobAdapterError(
                f"order response has unknown status: {exchange_status!r}"
            )

        raw_side = str(raw.get("side") or "").upper()
        side = {
            "BUY": OrderSide.BUY,
            "SELL": OrderSide.SELL,
        }.get(raw_side)
        return OrderResult(
            client_order_id=client_order_id or exchange_order_id,
            exchange_order_id=exchange_order_id,
            market_id=str(raw.get("market")) if raw.get("market") else None,
            token_id=str(raw.get("asset_id")) if raw.get("asset_id") else None,
            side=side,
            status=status,
            accepted=status != OrderStatus.REJECTED,
            message=exchange_status.lower(),
            requested_size=requested,
            filled_size=filled,
            avg_fill_price=price if filled > 0 and price > 0 else None,
        )

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
            allowances = raw.get("allowances")
            if not isinstance(allowances, dict):
                raise ValueError("allowances is not an object")
            balance = _fixed_six(raw.get("balance"))
            allowance = _fixed_six(
                min(
                    (Decimal(str(value)) for value in allowances.values()),
                    default=Decimal("0"),
                )
            )
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
        from py_clob_client_v2.exceptions import PolyApiException

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
        except ClobAdapterError:
            raise
        except Exception as exc:
            raise ClobAdapterError(
                f"order creation failed: {type(exc).__name__}"
            ) from exc
        try:
            raw = self._client.post_order(signed, order_type=order_type)
        except ClobAdapterError:
            raise
        except PolyApiException as exc:
            if exc.status_code is not None:
                raise ClobAdapterError(
                    f"order submission rejected:http_{exc.status_code}"
                ) from exc
            raise ClobUncertainOutcomeError(
                "order submission outcome unknown:PolyApiException"
            ) from exc
        except Exception as exc:
            raise ClobUncertainOutcomeError(
                f"order submission outcome unknown: {type(exc).__name__}"
            ) from exc
        latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)

        if not isinstance(raw, dict):
            raise ClobAdapterError(f"submission response is not an object: {type(raw).__name__}")
        exchange_order_id = str(raw.get("orderID") or raw.get("order_id") or "")
        success = raw.get("success")
        error_message = str(raw.get("errorMsg") or "")
        if success is not True or not exchange_order_id:
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.REJECTED,
                accepted=False,
                message=error_message or "submission_response_rejected",
                signal_id=order.signal_id,
                strategy_name=order.strategy_name,
                requested_size=order.size,
                latency_ms=latency_ms,
            )
        exchange_status = str(raw.get("status") or "").lower()
        if exchange_status not in {"live", "delayed", "matched"}:
            raise ClobAdapterError(
                f"submission response has unknown status: {exchange_status!r}"
            )

        filled_size = Decimal("0")
        avg_fill_price: Decimal | None = None
        status = OrderStatus.SUBMITTED
        accepted = True
        result_message = exchange_status
        if (
            order.time_in_force == OrderTimeInForce.FOK
            and exchange_status != "matched"
        ):
            status = OrderStatus.UNKNOWN
            accepted = False
            result_message = f"fok_fill_not_confirmed:{exchange_status}"
        if exchange_status == "matched":
            making_amount = _fixed_six(raw.get("makingAmount"))
            taking_amount = _fixed_six(raw.get("takingAmount"))
            if order.side == OrderSide.BUY:
                filled_size = taking_amount
                fill_notional = making_amount
            else:
                filled_size = making_amount
                fill_notional = taking_amount
            if filled_size <= 0:
                raise ClobAdapterError("matched submission response has zero fill size")
            avg_fill_price = fill_notional / filled_size
            status = (
                OrderStatus.FILLED
                if filled_size >= order.size
                else OrderStatus.PARTIALLY_FILLED
            )
            if (
                order.time_in_force == OrderTimeInForce.FOK
                and filled_size < order.size
            ):
                status = OrderStatus.UNKNOWN
                accepted = False
                result_message = "fok_partial_fill_invariant_violation"

        return OrderResult(
            client_order_id=order.client_order_id,
            exchange_order_id=exchange_order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            status=status,
            accepted=accepted,
            message=result_message,
            signal_id=order.signal_id,
            strategy_name=order.strategy_name,
            requested_size=order.size,
            filled_size=filled_size,
            avg_fill_price=avg_fill_price,
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
        return _validate_cancel_response(raw, expected_order_id=order_id)

    def cancel_all(self) -> bool:
        """Cancel every open order through the explicit V2 method."""

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order cancellation disabled in current mode")
        try:
            raw = self._client.cancel_all()
        except Exception as exc:
            raise ClobAdapterError(f"cancel-all failed: {exc}") from exc
        return _validate_cancel_response(raw)


class _DisabledClobClient:
    """No-op client used when live SDK construction is intentionally skipped."""

    def get_open_orders(self, params: Any = None) -> list[dict[str, Any]]:
        return []


def _fixed_six(value: object) -> Decimal:
    """Convert a CLOB fixed-math amount with six decimals to units."""

    return Decimal(str(value or "0")) / FIXED_SIX_SCALE


def _validate_cancel_response(
    raw: object,
    *,
    expected_order_id: str | None = None,
) -> bool:
    """Require the documented cancellation result to confirm success."""

    if not isinstance(raw, dict):
        raise ClobAdapterError(
            f"cancellation response is not an object: {type(raw).__name__}"
        )
    canceled = raw.get("canceled")
    not_canceled = raw.get("not_canceled")
    if not isinstance(canceled, list) or not isinstance(not_canceled, dict):
        raise ClobAdapterError("cancellation response is malformed")
    if not_canceled:
        failed_ids = ",".join(sorted(str(order_id) for order_id in not_canceled))
        raise ClobAdapterError(f"orders not canceled: {failed_ids}")
    if expected_order_id is not None and expected_order_id not in {
        str(order_id) for order_id in canceled
    }:
        raise ClobAdapterError(f"order not canceled: {expected_order_id}")
    return True
