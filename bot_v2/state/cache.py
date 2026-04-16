"""Small in-memory cache utilities for market history."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque

from models.market import MarketSnapshot

MarketTokenKey = tuple[str, str]


class MarketHistoryCache:
    """Bounded tick history used by deterministic strategies."""

    def __init__(self, max_points_per_market: int = 200) -> None:
        if max_points_per_market <= 1:
            raise ValueError("max_points_per_market must be > 1")
        self._max_points = max_points_per_market
        self._cache: dict[MarketTokenKey, deque[MarketSnapshot]] = defaultdict(
            lambda: deque(maxlen=self._max_points)
        )
        self._lock = asyncio.Lock()

    async def add_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Append new snapshot for market token."""

        async with self._lock:
            self._cache[(snapshot.market_id, snapshot.token_id)].append(snapshot)

    async def recent_snapshots(
        self, market_id: str, token_id: str, limit: int
    ) -> list[MarketSnapshot]:
        """Return up to `limit` recent snapshots, newest last."""

        if limit <= 0:
            return []
        async with self._lock:
            data = self._cache.get((market_id, token_id))
            if not data:
                return []
            return list(data)[-limit:]
