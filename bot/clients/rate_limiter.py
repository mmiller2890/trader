"""Async token-bucket style rate limiter."""

from __future__ import annotations

import asyncio
from collections import deque
from time import monotonic


class AsyncRateLimiter:
    """Simple sliding-window limiter for async callers."""

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be > 0")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be > 0")
        self._max_calls = max_calls
        self._period = period_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a call slot is available."""

        while True:
            async with self._lock:
                now = monotonic()
                self._evict_expired(now)
                if len(self._timestamps) < self._max_calls:
                    self._timestamps.append(now)
                    return
                oldest = self._timestamps[0]
                wait_seconds = max(0.0, self._period - (now - oldest))
            await asyncio.sleep(wait_seconds if wait_seconds > 0 else 0.001)

    def _evict_expired(self, now: float) -> None:
        threshold = now - self._period
        while self._timestamps and self._timestamps[0] <= threshold:
            self._timestamps.popleft()
