"""Production market-channel full-book and delta reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from models.market import MarketSnapshot


def _timestamp_to_datetime(timestamp: object) -> datetime:
    return datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC)


class LiveBookState:
    """Reconstructed production book for one ``(market_id, token_id)`` pair."""

    def __init__(self, market_id: str, token_id: str) -> None:
        self.market_id = market_id
        self.token_id = token_id
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.tick_size: Decimal | None = None
        self.last_trade_price: Decimal | None = None
        self.resolved = False
        self.source_ts: datetime | None = None
        self.received_ts: datetime | None = None

    def apply_book(self, payload: dict[str, object]) -> None:
        """Replace the book from a full ``book`` event."""

        candidate_bids = self._parse_levels(payload.get("bids"))
        candidate_asks = self._parse_levels(payload.get("asks"))
        if candidate_bids and candidate_asks and max(candidate_bids) > min(candidate_asks):
            raise ValueError("crossed book: best bid exceeds best ask")
        source_ts = _timestamp_to_datetime(payload["timestamp"])
        self.bids = candidate_bids
        self.asks = candidate_asks
        self.source_ts = source_ts
        self.received_ts = source_ts

    def apply_price_change(self, change: dict[str, object], timestamp: object) -> None:
        """Apply one price-change delta atomically."""

        side = str(change.get("side") or "").upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"price change has invalid side: {side!r}")
        try:
            price = Decimal(str(change["price"]))
            size = Decimal(str(change["size"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"price change has invalid price/size: {exc}") from exc
        if size < 0:
            raise ValueError(f"price change has negative size: {size}")

        candidate_bids = dict(self.bids)
        candidate_asks = dict(self.asks)
        target = candidate_bids if side == "BUY" else candidate_asks
        if size > 0:
            target[price] = size
        else:
            target.pop(price, None)
        if candidate_bids and candidate_asks and max(candidate_bids) > min(candidate_asks):
            raise ValueError("crossed book: best bid exceeds best ask")
        source_ts = _timestamp_to_datetime(timestamp)
        self.bids = candidate_bids
        self.asks = candidate_asks
        self.source_ts = source_ts
        self.received_ts = source_ts

    def snapshot(self) -> MarketSnapshot | None:
        """Return a snapshot only when both sides exist."""

        if not self.bids or not self.asks:
            return None
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        return MarketSnapshot(
            market_id=self.market_id,
            token_id=self.token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=(best_bid + best_ask) / Decimal("2"),
            top_bid_size=self.bids[best_bid],
            top_ask_size=self.asks[best_ask],
            last_trade_price=self.last_trade_price,
            source_ts=self.source_ts or datetime.fromtimestamp(0, tz=UTC),
            received_ts=self.received_ts or datetime.fromtimestamp(0, tz=UTC),
        )

    def _parse_levels(self, raw: object) -> dict[Decimal, Decimal]:
        levels: dict[Decimal, Decimal] = {}
        if not isinstance(raw, list):
            raise ValueError(f"book levels are not a list: {type(raw).__name__}")
        for row in raw:
            if not isinstance(row, dict):
                raise ValueError(f"book level is not an object: {type(row).__name__}")
            try:
                price = Decimal(str(row["price"]))
                size = Decimal(str(row["size"]))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(f"book level has invalid price/size: {exc}") from exc
            if size < 0:
                raise ValueError(f"book level has negative size: {size}")
            if size > 0:
                levels[price] = size
        return levels
