"""Normalized market-data ingestion boundary."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from models.market import MarketSnapshot, OrderBookLevel, OrderBookUpdate
from state.store import InMemoryStateStore

logger = logging.getLogger(__name__)

MarketUpdateHandler = Callable[[MarketSnapshot], Awaitable[None]]
OrderBookHandler = Callable[[OrderBookUpdate], Awaitable[None]]


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class MarketDataClient:
    """Parse transport payloads into internal typed market models."""

    def __init__(
        self,
        *,
        state_store: InMemoryStateStore,
        on_snapshot: MarketUpdateHandler | None = None,
        on_orderbook: OrderBookHandler | None = None,
    ) -> None:
        self._state_store = state_store
        self._on_snapshot = on_snapshot
        self._on_orderbook = on_orderbook

    async def handle_ws_message(self, message: dict | str) -> None:
        """Ingest websocket message and fan out typed updates."""

        if not isinstance(message, dict):
            return

        market_id = self._get_first(message, ("market_id", "market", "marketId"))
        token_id = self._get_first(message, ("token_id", "token", "asset_id", "tokenId"))
        if not market_id or not token_id:
            return

        bids = self._parse_levels(message.get("bids") or message.get("buy") or [])
        asks = self._parse_levels(message.get("asks") or message.get("sell") or [])
        if not bids or not asks:
            return

        update = OrderBookUpdate(
            market_id=str(market_id),
            token_id=str(token_id),
            bids=bids,
            asks=asks,
            sequence_id=self._maybe_int(message.get("sequence") or message.get("seq")),
            source_ts=utc_now(),
            received_ts=utc_now(),
        )
        snapshot = self._to_snapshot(update)

        await self._state_store.update_orderbook(update)
        await self._state_store.update_market_snapshot(snapshot)
        await self._state_store.update_heartbeat("market_data", snapshot.received_ts)

        if self._on_orderbook is not None:
            await self._on_orderbook(update)
        if self._on_snapshot is not None:
            await self._on_snapshot(snapshot)

        logger.debug(
            "market data ingested",
            extra={
                "component": "market_data_client",
                "event_type": "market_update_received",
                "market_id": snapshot.market_id,
                "token_id": snapshot.token_id,
            },
        )

    def _to_snapshot(self, update: OrderBookUpdate) -> MarketSnapshot:
        best_bid = update.bids[0]
        best_ask = update.asks[0]
        mid_price = (best_bid.price + best_ask.price) / Decimal("2")
        return MarketSnapshot(
            market_id=update.market_id,
            token_id=update.token_id,
            best_bid=best_bid.price,
            best_ask=best_ask.price,
            mid_price=mid_price,
            top_bid_size=best_bid.size,
            top_ask_size=best_ask.size,
            source_ts=update.source_ts,
            received_ts=update.received_ts,
        )

    def _parse_levels(self, levels: Any) -> list[OrderBookLevel]:
        out: list[OrderBookLevel] = []
        if not isinstance(levels, list):
            return out
        for level in levels:
            price: Any
            size: Any
            if isinstance(level, dict):
                price = level.get("price")
                size = level.get("size")
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]
            else:
                continue
            try:
                out.append(OrderBookLevel(price=Decimal(str(price)), size=Decimal(str(size))))
            except Exception:
                continue
        return out

    def _get_first(self, payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in payload:
                return payload[key]
        return None

    def _maybe_int(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
