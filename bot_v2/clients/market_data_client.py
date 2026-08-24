"""Normalized market-data ingestion boundary."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from clients.live_book import LiveBookState
from models.market import MarketSnapshot
from state.store import InMemoryStateStore

logger = logging.getLogger(__name__)

MarketUpdateHandler = Callable[[MarketSnapshot], Awaitable[None]]


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class MarketDataClient:
    """Parse production market-channel payloads into typed market models."""

    def __init__(
        self,
        *,
        state_store: InMemoryStateStore,
        on_snapshot: MarketUpdateHandler | None = None,
        on_fallback_snapshot: MarketUpdateHandler | None = None,
    ) -> None:
        self._state_store = state_store
        self._on_snapshot = on_snapshot
        self._on_fallback_snapshot = on_fallback_snapshot
        self._books: dict[tuple[str, str], LiveBookState] = {}

    async def handle_ws_message(self, message: dict | str) -> None:
        """Ingest one websocket message and fan out typed updates."""

        if not isinstance(message, dict):
            return
        event_type = str(message.get("event_type") or "")
        if event_type == "book":
            await self._handle_book(message)
        elif event_type == "price_change":
            await self._handle_price_change(message)
        elif event_type == "tick_size_change":
            await self._handle_tick_size_change(message)
        elif event_type == "market_resolved":
            await self._handle_market_resolved(message)
        elif event_type == "last_trade_price":
            await self._handle_last_trade_price(message)
        else:
            logger.debug(
                "unknown websocket event ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "unknown_ws_event",
                    "reason": event_type,
                },
            )

    async def _handle_book(self, message: dict) -> None:
        market_id, token_id = self._ids(message)
        if market_id is None or token_id is None:
            logger.warning(
                "malformed book event ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "malformed_ws_event",
                    "reason": "missing market or asset id",
                },
            )
            return
        book = self._books.setdefault((market_id, token_id), LiveBookState(market_id, token_id))
        try:
            book.apply_book(message)
        except (KeyError, ValueError) as exc:
            logger.warning(
                "malformed book event ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "malformed_ws_event",
                    "reason": str(exc),
                },
            )
            return
        await self._publish(book)

    async def _handle_price_change(self, message: dict) -> None:
        market_id = message.get("market") or message.get("market_id")
        if market_id is None:
            logger.warning(
                "malformed price_change event ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "malformed_ws_event",
                    "reason": "missing market id",
                },
            )
            return
        changes = message.get("price_changes")
        if not isinstance(changes, list):
            logger.warning(
                "malformed price_change event ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "malformed_ws_event",
                    "reason": "price_changes is not a list",
                },
            )
            return
        grouped: dict[str, list[dict[str, object]]] = {}
        for change in changes:
            if not isinstance(change, dict):
                continue
            asset_id = str(change.get("asset_id") or "")
            if not asset_id:
                continue
            grouped.setdefault(asset_id, []).append(change)
        for asset_id, asset_changes in grouped.items():
            book = self._books.get((str(market_id), asset_id))
            if book is None:
                continue
            try:
                book.apply_price_changes(asset_changes, message["timestamp"])
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "malformed price_change ignored",
                    extra={
                        "component": "market_data_client",
                        "event_type": "malformed_ws_event",
                        "reason": str(exc),
                    },
                )
                continue
            await self._publish(book)

    async def _handle_tick_size_change(self, message: dict) -> None:
        market_id, token_id = self._ids(message)
        if market_id is None or token_id is None:
            return
        book = self._books.get((market_id, token_id))
        if book is None:
            return
        try:
            book.tick_size = Decimal(str(message["new_tick_size"]))
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "malformed tick_size_change ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "malformed_ws_event",
                    "reason": "invalid new_tick_size",
                },
            )

    async def _handle_market_resolved(self, message: dict) -> None:
        market_id = str(message.get("market") or message.get("market_id") or "")
        asset_ids = message.get("assets_ids")
        if not market_id or not isinstance(asset_ids, list):
            return
        resolved_assets = {str(asset_id) for asset_id in asset_ids}
        for (book_market_id, token_id), book in self._books.items():
            if book_market_id == market_id and token_id in resolved_assets:
                book.resolved = True

    async def _handle_last_trade_price(self, message: dict) -> None:
        market_id, token_id = self._ids(message)
        if market_id is None or token_id is None:
            return
        book = self._books.get((market_id, token_id))
        if book is None:
            return
        try:
            book.last_trade_price = Decimal(str(message["price"]))
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "malformed last_trade_price ignored",
                extra={
                    "component": "market_data_client",
                    "event_type": "malformed_ws_event",
                    "reason": "invalid price",
                },
            )
            return
        await self._publish(book)

    async def ingest_fallback_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Store a REST fallback snapshot for exit/reconciliation only."""

        await self._state_store.update_market_snapshot(snapshot)
        await self._state_store.update_heartbeat("market_data", snapshot.received_ts)
        if self._on_fallback_snapshot is not None:
            await self._on_fallback_snapshot(snapshot)

    async def _publish(self, book: LiveBookState) -> None:
        if book.resolved:
            return
        snapshot = book.snapshot()
        if snapshot is None:
            return
        await self._state_store.update_market_snapshot(snapshot)
        await self._state_store.update_heartbeat("market_data", snapshot.received_ts)
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

    def _ids(self, message: dict) -> tuple[str | None, str | None]:
        market_id = message.get("market") or message.get("market_id")
        token_id = message.get("asset_id") or message.get("token_id")
        if market_id is None or token_id is None:
            return None, None
        return str(market_id), str(token_id)
