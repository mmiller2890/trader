"""Typed adapter around the Polymarket CLOB V2 SDK."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from clients.auth import ClobCredentials, effective_funder_address
from config.schema import AppConfig
from models.market import MarketSnapshot, OrderBookLevel
from models.tick import TickSizeError, normalize_tick_size
from models.order import (
    CancelIntent,
    CancelOutcome,
    CancelResult,
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
        self._tick_size_cache: dict[str, Decimal] = {}
        self._neg_risk_cache: dict[str, bool] = {}
        self._minimum_order_size_cache: dict[str, Decimal] = {}

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

    def get_market_snapshot(self, market_id: str, token_id: str) -> MarketSnapshot:
        """Fetch and normalize one order book via the explicit V2 method."""

        from decimal import Decimal as D

        try:
            raw = self._client.get_order_book(token_id)
        except Exception as exc:
            raise ClobAdapterError(f"order book read failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise ClobAdapterError(f"order book response is not an object: {type(raw).__name__}")
        bids_raw = raw.get("bids") or raw.get("buy") or []
        asks_raw = raw.get("asks") or raw.get("sell") or []
        if not isinstance(bids_raw, list) or not isinstance(asks_raw, list):
            raise ClobAdapterError("order book levels are not lists")
        try:
            bids = [
                (D(str(row["price"])), D(str(row["size"])))
                for row in bids_raw
                if isinstance(row, dict) and D(str(row.get("size", "0"))) > 0
            ]
            asks = [
                (D(str(row["price"])), D(str(row["size"])))
                for row in asks_raw
                if isinstance(row, dict) and D(str(row.get("size", "0"))) > 0
            ]
        except (KeyError, ValueError) as exc:
            raise ClobAdapterError(f"order book has invalid price/size: {exc}") from exc
        if not bids or not asks:
            raise ClobAdapterError("order book is missing bids or asks")
        best_bid = max(p for p, _ in bids)
        best_ask = min(p for p, _ in asks)
        if best_bid > best_ask:
            raise ClobAdapterError("crossed book: best bid exceeds best ask")
        bid_size = max(s for p, s in bids if p == best_bid)
        ask_size = max(s for p, s in asks if p == best_ask)
        from datetime import UTC, datetime

        now = datetime.now(tz=UTC)
        return MarketSnapshot(
            market_id=market_id,
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=(best_bid + best_ask) / D("2"),
            top_bid_size=bid_size,
            top_ask_size=ask_size,
            source_ts=now,
            received_ts=now,
        )

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
                requested = _decimal_units(row.get("original_size"))
                filled = _decimal_units(row.get("size_matched"))
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
            requested = _decimal_units(raw.get("original_size"))
            filled = _decimal_units(raw.get("size_matched"))
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

    def get_tick_size(self, token_id: str) -> Decimal:
        """
        Return the exchange tick size for ``token_id``.

        The value is cached because it is immutable for the life of a market
        and every quote refresh needs it. A transport failure falls back to
        the configured default rather than blocking execution, because the
        order builder still snaps onto a valid grid either way.
        """

        cached = self._tick_size_cache.get(token_id)
        if cached is not None:
            return cached
        try:
            raw = self._client.get_tick_size(token_id)
        except Exception as exc:
            logger.warning(
                "tick size lookup failed",
                extra={
                    "component": "clob_client",
                    "event_type": "tick_size_lookup_failed",
                    "token_id": token_id,
                    "reason": type(exc).__name__,
                },
            )
            return self._config.execution.default_tick_size
        try:
            tick_size = normalize_tick_size(str(raw))
        except TickSizeError:
            logger.warning(
                "tick size response unsupported",
                extra={
                    "component": "clob_client",
                    "event_type": "tick_size_unsupported",
                    "token_id": token_id,
                },
            )
            return self._config.execution.default_tick_size
        self._tick_size_cache[token_id] = tick_size
        return tick_size

    def get_minimum_order_size(self, market_id: str) -> Decimal:
        """
        Return the venue's minimum order size for ``market_id``.

        Polymarket publishes this per market and rejects anything under it with
        `order is invalid. size (N) lower than the minimum: M`. Knowing it up
        front lets the order builder refuse locally, with a reason naming the
        real constraint, instead of spending a round trip to be told.

        Cached because it is immutable for the life of a market. A transport
        failure falls back to the configured minimum rather than blocking
        execution, matching how tick size degrades.
        """

        cached = self._minimum_order_size_cache.get(market_id)
        if cached is not None:
            return cached
        try:
            raw = self._client.get_market(market_id)
            value = Decimal(str(raw["minimum_order_size"]))
        except Exception as exc:
            logger.warning(
                "minimum order size lookup failed",
                extra={
                    "component": "clob_client",
                    "event_type": "minimum_order_size_lookup_failed",
                    "market_id": market_id,
                    "reason": type(exc).__name__,
                },
            )
            return self._config.execution.min_order_size
        if value <= 0:
            return self._config.execution.min_order_size
        self._minimum_order_size_cache[market_id] = value
        return value

    def get_neg_risk(self, token_id: str) -> bool:
        """Return the cached negative-risk flag required for order signing."""

        cached = self._neg_risk_cache.get(token_id)
        if cached is not None:
            return cached
        try:
            value = bool(self._client.get_neg_risk(token_id))
        except Exception as exc:
            logger.warning(
                "neg risk lookup failed",
                extra={
                    "component": "clob_client",
                    "event_type": "neg_risk_lookup_failed",
                    "token_id": token_id,
                    "reason": type(exc).__name__,
                },
            )
            return False
        self._neg_risk_cache[token_id] = value
        return value

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

        from py_clob_client_v2 import (
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )
        from py_clob_client_v2.exceptions import PolyApiException

        tick_size = self.get_tick_size(order.token_id)
        if order.price % tick_size != 0:
            raise ClobAdapterError(
                f"order price {order.price} is not a multiple of tick size {tick_size}"
            )
        side = Side.BUY if order.side == OrderSide.BUY else Side.SELL
        order_type = {
            OrderTimeInForce.GTC: OrderType.GTC,
            OrderTimeInForce.IOC: OrderType.FAK,
            OrderTimeInForce.FOK: OrderType.FOK,
        }[order.time_in_force]
        # post_only and a killing time-in-force are mutually exclusive: a
        # post-only order rests by definition, and FOK/FAK cancel whatever does
        # not fill immediately. The SDK raises ValueError for the combination,
        # which the generic handler around post_order would turn into
        # ClobUncertainOutcomeError -- recording a deterministic local bug as an
        # unknown outcome, the one category that forces divergence handling.
        # Refuse it here, before anything is signed or sent.
        if order.post_only and order_type in {OrderType.FOK, OrderType.FAK}:
            raise ClobAdapterError(
                f"post_only is not supported with time in force "
                f"{order.time_in_force.value}"
            )
        args = OrderArgs(
            token_id=order.token_id,
            price=float(str(order.price)),
            size=float(str(order.size)),
            side=side,
        )
        options = PartialCreateOrderOptions(
            tick_size=str(tick_size),
            neg_risk=self.get_neg_risk(order.token_id),
        )

        started = datetime.now(tz=UTC)
        try:
            signed = self._client.create_order(args, options)
        except ClobAdapterError:
            raise
        except Exception as exc:
            raise ClobAdapterError(
                f"order creation failed: {type(exc).__name__}"
            ) from exc
        try:
            raw = self._client.post_order(
                signed,
                order_type=order_type,
                post_only=order.post_only,
            )
        except ClobAdapterError:
            raise
        except PolyApiException as exc:
            if exc.status_code is not None:
                raise ClobAdapterError(
                    f"order submission rejected:http_{exc.status_code}"
                    f":{_sanitize_upstream_error(exc.error_msg)}"
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
                liquidity="maker" if order.post_only else "taker",
            )
        exchange_status = str(raw.get("status") or "").lower()
        if exchange_status not in {"live", "delayed", "matched", "unmatched"}:
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
            making_amount = _decimal_units(raw.get("makingAmount"))
            taking_amount = _decimal_units(raw.get("takingAmount"))
            if order.side == OrderSide.BUY:
                filled_size = taking_amount
                fill_notional = making_amount
            else:
                filled_size = making_amount
                fill_notional = taking_amount
            if filled_size <= 0:
                raise ClobAdapterError("matched submission response has zero fill size")
            # A venue cannot fill more than was asked for, so a fill that
            # exceeds the request is a unit misread rather than a real fill.
            # These amounts were parsed as six-decimal fixed point until
            # 2026-08-25 and as plain decimals since, and that reading has
            # never been confirmed against the live venue -- getting it wrong
            # this way turns one share into a million in position accounting.
            # Fail closed so reconciliation resolves it against the exchange.
            if filled_size > order.size:
                raise ClobAdapterError(
                    f"matched fill size {filled_size} exceeds requested size "
                    f"{order.size}"
                )
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
            # A post-only order that crossed would be rejected outright
            # rather than filled, so a fill here is trustworthy as a maker
            # fill whenever the order was submitted post-only.
            liquidity="maker" if order.post_only else "taker",
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

    def cancel_resting_order(self, intent: CancelIntent) -> CancelResult:
        """
        Cancel one resting order and classify the outcome.

        Market making cancels constantly, and an order that filled or expired
        between the decision and the request is the normal case rather than a
        fault. Those resolve to ``NOT_FOUND`` — terminal, book is clean — while
        a genuine refusal resolves to ``FAILED`` and a transport failure to
        ``UNKNOWN`` so the caller can fail closed.
        """

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order cancellation disabled in current mode")
        order_id = intent.exchange_order_id
        if not order_id:
            return CancelResult(
                client_order_id=intent.client_order_id,
                outcome=CancelOutcome.NOT_FOUND,
                message="no_exchange_order_id",
            )
        from py_clob_client_v2 import OrderPayload

        try:
            raw = self._client.cancel_order(OrderPayload(orderID=order_id))
        except Exception as exc:
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=order_id,
                outcome=CancelOutcome.UNKNOWN,
                message=f"cancel_transport_failed:{type(exc).__name__}",
            )
        if not isinstance(raw, dict):
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=order_id,
                outcome=CancelOutcome.UNKNOWN,
                message="cancellation_response_malformed",
            )
        canceled = raw.get("canceled")
        not_canceled = raw.get("not_canceled")
        if not isinstance(canceled, list) or not isinstance(not_canceled, dict):
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=order_id,
                outcome=CancelOutcome.UNKNOWN,
                message="cancellation_response_malformed",
            )
        if order_id in {str(value) for value in canceled}:
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=order_id,
                outcome=CancelOutcome.CANCELLED,
                message="cancelled",
            )
        refusal = _sanitize_upstream_error(not_canceled.get(order_id))
        if _is_already_gone(refusal):
            return CancelResult(
                client_order_id=intent.client_order_id,
                exchange_order_id=order_id,
                outcome=CancelOutcome.NOT_FOUND,
                message=refusal,
            )
        return CancelResult(
            client_order_id=intent.client_order_id,
            exchange_order_id=order_id,
            outcome=CancelOutcome.FAILED,
            message=refusal,
        )

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


_SECRET_SHAPED = re.compile(r"[A-Za-z0-9+=]{16,}")
_ALLOWED_ERROR_CHARS = re.compile(r"[^a-z0-9 ,.:;'()/_-]")


_ALREADY_GONE_MARKERS = (
    "not found",
    "no order",
    "does not exist",
    "already canceled",
    "already cancelled",
    "already filled",
    "already matched",
    "not live",
)


def _is_already_gone(refusal: str) -> bool:
    """True when a cancel refusal means the order already left the book."""

    return any(marker in refusal for marker in _ALREADY_GONE_MARKERS)


def _sanitize_upstream_error(message: object) -> str:
    """
    Reduce an upstream error body to a short, secret-free diagnostic.

    The CLOB returns the actionable reason for a 4xx ("not enough balance",
    "invalid tick size") which the operator needs, but the body is attacker-
    and account-adjacent text. Any run of 16 or more token characters is the
    shape of a key, signature, address, or order hash, so it is dropped
    before the remainder is character-filtered and truncated.
    """

    if message is None:
        return "no_detail"
    if isinstance(message, dict):
        for key in ("error", "errorMsg", "message", "detail"):
            value = message.get(key)
            if isinstance(value, str) and value:
                message = value
                break
        else:
            message = ",".join(sorted(str(key) for key in message))
    text = str(message).strip().lower()
    text = _SECRET_SHAPED.sub("", text)
    text = _ALLOWED_ERROR_CHARS.sub(" ", text)
    text = " ".join(text.split())
    if not text:
        return "no_detail"
    return text[:160]


def _fixed_six(value: object) -> Decimal:
    """Convert a CLOB fixed-math amount with six decimals to units."""

    return Decimal(str(value or "0")) / FIXED_SIX_SCALE


def _decimal_units(value: object) -> Decimal:
    """Convert decimal-unit amounts returned by immediate order submission."""

    return Decimal(str(value or "0"))


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
