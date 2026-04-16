"""Thin adapter around py-clob-client."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from clients.auth import ClobCredentials
from models.market import OrderBookLevel, OrderBookUpdate
from models.order import OrderRequest, OrderResult, OrderStatus
from models.position import Position

logger = logging.getLogger(__name__)

try:
    from py_clob_client.client import ClobClient as _SdkClobClient
except Exception:  # pragma: no cover - depends on runtime dependency presence
    _SdkClobClient = None


class ClobAdapterError(RuntimeError):
    """Raised when SDK adapter cannot satisfy requested operation."""


class ClobClientAdapter:
    """
    Polymarket CLOB boundary.

    All SDK uncertainty is intentionally centralized in this class.
    """

    def __init__(
        self,
        client: Any,
        *,
        allow_trading: bool,
        read_only: bool = True,
    ) -> None:
        self._client = client
        self._allow_trading = allow_trading
        self._read_only = read_only

    @classmethod
    def disabled(cls) -> "ClobClientAdapter":
        """Create a safe no-op adapter used outside explicit live mode."""

        return cls(_DisabledClobClient(), allow_trading=False, read_only=True)

    @classmethod
    def from_sdk(
        cls,
        *,
        host: str,
        credentials: ClobCredentials,
        chain_id: int = 137,
        allow_trading: bool = False,
    ) -> "ClobClientAdapter":
        """
        Build adapter with an SDK client instance.

        Adapter boundary note:
        py-clob-client constructor signatures have changed between versions,
        so initialization attempts are bounded here instead of leaked elsewhere.
        """

        if _SdkClobClient is None:
            raise ClobAdapterError("py-clob-client is not installed or unavailable")

        init_attempts: list[tuple[tuple[Any, ...], dict[str, Any]]] = [
            ((host, credentials.private_key, chain_id), {}),
            ((), {"host": host, "key": credentials.private_key, "chain_id": chain_id}),
            ((), {"host": host, "private_key": credentials.private_key, "chain_id": chain_id}),
        ]
        last_error: Exception | None = None
        for args, kwargs in init_attempts:
            try:
                client = _SdkClobClient(*args, **kwargs)
                return cls(client, allow_trading=allow_trading, read_only=not allow_trading)
            except Exception as exc:  # pragma: no cover - runtime sdk behavior
                last_error = exc
                continue
        raise ClobAdapterError(f"failed to initialize SDK client: {last_error}")

    def get_order_book(self, market_id: str, token_id: str) -> OrderBookUpdate:
        """Fetch and normalize orderbook for a token."""

        raw = self._call_first_available(
            ("get_order_book", "get_book", "order_book"),
            market_id=market_id,
            token_id=token_id,
        )
        return self._normalize_order_book(raw=raw, market_id=market_id, token_id=token_id)

    def get_open_orders(self, market_id: str | None = None) -> list[OrderResult]:
        """Fetch open orders as internal typed results."""

        raw = self._call_first_available(
            ("get_open_orders", "list_open_orders", "get_orders"),
            market_id=market_id,
        )
        if not isinstance(raw, list):
            return []

        results: list[OrderResult] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            client_order_id = str(row.get("client_order_id") or row.get("id") or "")
            if not client_order_id:
                continue
            size = Decimal(str(row.get("size", "0") or "0"))
            if size <= 0:
                continue
            results.append(
                OrderResult(
                    client_order_id=client_order_id,
                    exchange_order_id=str(row.get("order_id")) if row.get("order_id") else None,
                    market_id=str(row.get("market_id")) if row.get("market_id") else None,
                    token_id=str(row.get("token_id")) if row.get("token_id") else None,
                    status=OrderStatus.SUBMITTED,
                    accepted=True,
                    message="open_order_snapshot",
                    requested_size=size,
                    filled_size=Decimal(str(row.get("filled_size", "0") or "0")),
                )
            )
        return results

    def get_positions(self) -> list[Position]:
        """
        Fetch positions from exchange if available.

        If SDK support is unavailable, return empty list from this safe boundary.
        """

        try:
            raw = self._call_first_available(("get_positions", "list_positions", "positions"))
        except ClobAdapterError:
            logger.info(
                "positions API unavailable",
                extra={"component": "clob_client", "event_type": "positions_unavailable"},
            )
            return []

        if not isinstance(raw, list):
            return []
        positions: list[Position] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            market_id = str(row.get("market_id") or "")
            token_id = str(row.get("token_id") or "")
            if not market_id or not token_id:
                continue
            positions.append(
                Position(
                    market_id=market_id,
                    token_id=token_id,
                    quantity=Decimal(str(row.get("size", "0") or "0")),
                    average_entry_price=Decimal(str(row.get("avg_entry", "0") or "0")),
                    mark_price=Decimal(str(row["mark_price"])) if row.get("mark_price") else None,
                    unrealized_pnl=Decimal(str(row.get("unrealized_pnl", "0") or "0")),
                    realized_pnl=Decimal(str(row.get("realized_pnl", "0") or "0")),
                )
            )
        return positions

    def submit_order(self, order: OrderRequest) -> OrderResult:
        """Submit order through SDK when live trading is explicitly enabled."""

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order submission disabled in current mode")

        started = datetime.now(tz=UTC)
        payload = {
            "market_id": order.market_id,
            "token_id": order.token_id,
            "side": order.side.value,
            "price": str(order.price),
            "size": str(order.size),
            "time_in_force": order.time_in_force.value,
            "client_order_id": order.client_order_id,
        }
        raw = self._call_first_available(("submit_order", "create_order", "place_order"), **payload)
        latency_ms = int((datetime.now(tz=UTC) - started).total_seconds() * 1000)

        exchange_order_id = None
        if isinstance(raw, dict) and raw.get("order_id"):
            exchange_order_id = str(raw["order_id"])
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
        """Cancel an order if trading is enabled."""

        if self._read_only or not self._allow_trading:
            raise ClobAdapterError("real order cancellation disabled in current mode")
        raw = self._call_first_available(("cancel_order", "cancel"), order_id=order_id)
        if isinstance(raw, dict):
            return bool(raw.get("success", True))
        if isinstance(raw, bool):
            return raw
        return True

    def _call_first_available(self, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
        for name in names:
            target = getattr(self._client, name, None)
            if target is None:
                continue
            try:
                return target(*args, **kwargs)
            except TypeError:
                if kwargs:
                    return target(*args)
                raise
        raise ClobAdapterError(f"SDK method not found for adapter operation: {names}")

    def _normalize_order_book(self, *, raw: Any, market_id: str, token_id: str) -> OrderBookUpdate:
        if not isinstance(raw, dict):
            raise ClobAdapterError("unexpected orderbook payload shape")
        bids_raw = raw.get("bids") or raw.get("buy") or []
        asks_raw = raw.get("asks") or raw.get("sell") or []
        sequence_id = raw.get("sequence") or raw.get("seq")
        return OrderBookUpdate(
            market_id=market_id,
            token_id=token_id,
            bids=self._normalize_levels(bids_raw),
            asks=self._normalize_levels(asks_raw),
            sequence_id=int(sequence_id) if sequence_id is not None else None,
        )

    def _normalize_levels(self, levels: Any) -> list[OrderBookLevel]:
        normalized: list[OrderBookLevel] = []
        if not isinstance(levels, list):
            return normalized
        for row in levels:
            if isinstance(row, dict):
                price = row.get("price")
                size = row.get("size")
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                price, size = row[0], row[1]
            else:
                continue
            try:
                normalized.append(
                    OrderBookLevel(price=Decimal(str(price)), size=Decimal(str(size)))
                )
            except Exception:
                continue
        return normalized


class _DisabledClobClient:
    """No-op client used when SDK initialization is intentionally skipped."""

    def get_open_orders(self, market_id: str | None = None) -> list[dict[str, Any]]:
        _ = market_id
        return []

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def get_order_book(self, market_id: str, token_id: str) -> dict[str, Any]:
        raise ClobAdapterError(
            f"order book unavailable via disabled client for market_id={market_id} token_id={token_id}"
        )
