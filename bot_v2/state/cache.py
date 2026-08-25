"""Small in-memory cache utilities for market history."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from datetime import datetime, timedelta

from models.market import MarketSnapshot

MarketTokenKey = tuple[str, str]


class MarketHistoryCache:
    """Bounded tick history used by deterministic strategies."""

    def __init__(
        self,
        max_points_per_market: int = 200,
        *,
        max_age_seconds: float | None = None,
    ) -> None:
        if max_points_per_market <= 1:
            raise ValueError("max_points_per_market must be > 1")
        self._max_points = max_points_per_market
        self._max_age_seconds = max_age_seconds
        self._cache: dict[MarketTokenKey, deque[MarketSnapshot]] = defaultdict(
            lambda: deque(maxlen=self._max_points)
        )
        self._lock = asyncio.Lock()

    async def add_snapshot(self, snapshot: MarketSnapshot) -> None:
        """
        Append new snapshot for market token.

        When ``max_age_seconds`` is set, entries older than that are dropped on
        append. A count-only bound is not enough for time-window queries:
        book updates arrive at a few hundred per second, so a 200-point deque
        holds well under a second of history.
        """

        async with self._lock:
            series = self._cache[(snapshot.market_id, snapshot.token_id)]
            series.append(snapshot)
            if self._max_age_seconds is None:
                return
            cutoff = snapshot.received_ts - timedelta(seconds=self._max_age_seconds)
            while series and series[0].received_ts < cutoff:
                series.popleft()

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

    async def snapshots_since(
        self, market_id: str, token_id: str, cutoff: datetime
    ) -> list[MarketSnapshot]:
        """
        Return snapshots observed at or after ``cutoff``, newest last.

        Counting book updates is not the same as measuring time: updates arrive
        in bursts, so a fixed count spans wildly different wall-clock windows.
        Strategies that want "the move over the last N seconds" use this.
        """

        async with self._lock:
            data = self._cache.get((market_id, token_id))
            if not data:
                return []
            return [
                snapshot for snapshot in data if snapshot.received_ts >= cutoff
            ]
