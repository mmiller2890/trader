"""Deterministic jittered exponential backoff."""

from __future__ import annotations

import random
from collections.abc import Callable

from config.schema import ReliabilityConfig


class BackoffSchedule:
    """Capped exponential delay with bounded jitter."""

    def __init__(
        self,
        config: ReliabilityConfig,
        *,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        self._initial = config.retry_initial_seconds
        self._max = config.retry_max_seconds
        self._jitter_ratio = config.retry_jitter_ratio
        self._random: Callable[[], float] = random_source or random.random

    def delay(self, attempt: int, *, random_source: Callable[[], float] | None = None) -> float:
        """Return the capped exponential delay for a one-based attempt number."""

        if attempt < 0:
            raise ValueError("attempt must be non-negative")
        source = random_source or self._random
        exponent = max(0, attempt - 1)
        base = min(self._max, self._initial * (2**exponent))
        if base >= self._max:
            base = float(self._max)
        jitter_span = base * self._jitter_ratio
        offset = (source() * 2.0 - 1.0) * jitter_span
        return max(0.0, base + offset)
