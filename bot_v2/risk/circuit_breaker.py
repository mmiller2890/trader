"""Simple failure-based circuit breaker."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class CircuitBreakerState:
    """Immutable breaker state snapshot."""

    tripped: bool
    failures_in_window: int
    cooldown_until: datetime | None


class CircuitBreaker:
    """Trip when too many failures occur in a rolling window."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        window_seconds: float,
        cooldown_seconds: float,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self._failure_threshold = failure_threshold
        self._window = timedelta(seconds=window_seconds)
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._failures: deque[datetime] = deque()
        self._cooldown_until: datetime | None = None

    def record_failure(self, when: datetime | None = None) -> None:
        """Record a failure and trip if threshold is exceeded."""

        now = when or utc_now()
        self._evict_old(now)
        self._failures.append(now)
        if len(self._failures) >= self._failure_threshold:
            self._cooldown_until = now + self._cooldown

    def record_success(self, when: datetime | None = None) -> None:
        """Progress breaker time window on successful operations."""

        self._evict_old(when or utc_now())

    def reset(self) -> None:
        """Clear breaker state."""

        self._failures.clear()
        self._cooldown_until = None

    def state(self, when: datetime | None = None) -> CircuitBreakerState:
        """Return current breaker state snapshot."""

        now = when or utc_now()
        self._evict_old(now)
        if self._cooldown_until is not None and now >= self._cooldown_until:
            self.reset()
        return CircuitBreakerState(
            tripped=self._cooldown_until is not None,
            failures_in_window=len(self._failures),
            cooldown_until=self._cooldown_until,
        )

    def _evict_old(self, now: datetime) -> None:
        threshold = now - self._window
        while self._failures and self._failures[0] < threshold:
            self._failures.popleft()
