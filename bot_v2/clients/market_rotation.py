"""Boundary-aware rotation for recurring BTC 15-minute markets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from clients.gamma_markets import DiscoveredMarket, MarketDiscoveryError
from config.schema import AutomaticMarketConfig


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class DiscoveryClient(Protocol):
    async def discover_active(
        self, now: datetime | None = None
    ) -> DiscoveredMarket: ...

    async def close(self) -> None: ...


class SubscriptionManager(Protocol):
    async def replace_asset_ids(self, asset_ids: list[str]) -> bool: ...


class MarketRotationState(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class MarketRotationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: MarketRotationState
    current_market: DiscoveredMarket | None = None
    last_success_at: datetime | None = None
    reason: str


Waiter = Callable[[asyncio.Event, float], Awaitable[bool]]


async def wait_or_stop(stop_event: asyncio.Event, delay: float) -> bool:
    """Wait for a stop request, returning false when the timeout expires."""

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.0, delay))
    except TimeoutError:
        return False
    return True


class Btc15mMarketRotator:
    """Discover and atomically rotate the active BTC 15-minute subscription."""

    def __init__(
        self,
        *,
        config: AutomaticMarketConfig,
        discovery: DiscoveryClient,
        websocket: SubscriptionManager,
        initial_market: DiscoveredMarket | None = None,
        clock: Callable[[], datetime] = utc_now,
        waiter: Waiter = wait_or_stop,
        can_rotate: Callable[[DiscoveredMarket], Awaitable[bool]] | None = None,
    ) -> None:
        self._config = config
        self._discovery = discovery
        self._websocket = websocket
        self._clock = clock
        self._waiter = waiter
        self._can_rotate = can_rotate
        self._closed = False
        self._status = MarketRotationStatus(
            state=(
                MarketRotationState.HEALTHY
                if initial_market is not None
                else MarketRotationState.STARTING
            ),
            current_market=initial_market,
            last_success_at=clock() if initial_market is not None else None,
            reason=(
                "market_discovered"
                if initial_market is not None
                else "awaiting_market_discovery"
            ),
        )

    def status(self) -> MarketRotationStatus:
        """Return an immutable snapshot of public rotation state."""

        return self._status.model_copy(deep=True)

    def mark_failed(self, reason: str) -> None:
        """Expose a supervised terminal failure without discarding market context."""

        self._status = MarketRotationStatus(
            state=MarketRotationState.FAILED,
            current_market=self._status.current_market,
            last_success_at=self._status.last_success_at,
            reason=reason,
        )

    async def initialize(self) -> DiscoveredMarket:
        """Resolve the initial market without altering the WebSocket subscription."""

        if self._status.current_market is not None:
            return self._status.current_market
        self._status = MarketRotationStatus(
            state=MarketRotationState.STARTING,
            reason="discovering_market",
        )
        now = self._now()
        try:
            market = await self._discovery.discover_active(now=now)
        except MarketDiscoveryError as exc:
            self._status = MarketRotationStatus(
                state=MarketRotationState.FAILED,
                reason=exc.reason,
            )
            raise
        self._status = MarketRotationStatus(
            state=MarketRotationState.HEALTHY,
            current_market=market,
            last_success_at=now,
            reason="market_discovered",
        )
        return market

    async def run(self, stop_event: asyncio.Event) -> None:
        """Rotate subscriptions until shutdown is requested."""

        current = await self.initialize()
        retry_delay = 1.0
        while not stop_event.is_set():
            now = self._now()
            lead_at = current.end_at - self._lead_delta()
            if now < lead_at:
                if await self._waiter(stop_event, (lead_at - now).total_seconds()):
                    return
                continue

            try:
                candidate = await self._discovery.discover_active(now=now)
                if candidate.asset_ids == current.asset_ids:
                    retry_delay = 1.0
                    if await self._waiter(stop_event, 1.0):
                        return
                    continue
                if self._can_rotate is not None and not await self._can_rotate(current):
                    if now >= current.end_at:
                        raise RuntimeError("position_open_at_market_end")
                    self._status = MarketRotationStatus(
                        state=MarketRotationState.DEGRADED,
                        current_market=current,
                        last_success_at=self._status.last_success_at,
                        reason="position_exit_pending",
                    )
                    if await self._waiter(stop_event, 1.0):
                        return
                    continue
                await self._websocket.replace_asset_ids(candidate.asset_ids)
            except MarketDiscoveryError as exc:
                self._status = MarketRotationStatus(
                    state=MarketRotationState.DEGRADED,
                    current_market=current,
                    last_success_at=self._status.last_success_at,
                    reason=exc.reason,
                )
                delay = self._retry_delay_seconds(
                    current=current,
                    now=now,
                    retry_delay=retry_delay,
                )
                if await self._waiter(stop_event, delay):
                    return
                retry_delay = min(10.0, retry_delay * 2)
                continue
            except RuntimeError:
                if stop_event.is_set():
                    return
                raise

            current = candidate
            succeeded_at = self._now()
            self._status = MarketRotationStatus(
                state=MarketRotationState.HEALTHY,
                current_market=current,
                last_success_at=succeeded_at,
                reason="market_rotated",
            )
            retry_delay = 1.0

    async def stop(self) -> None:
        """Close the owned discovery boundary exactly once."""

        if self._closed:
            return
        self._closed = True
        await self._discovery.close()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    def _lead_delta(self) -> timedelta:
        return timedelta(seconds=self._config.refresh_lead_seconds)

    def _retry_delay_seconds(
        self,
        *,
        current: DiscoveredMarket,
        now: datetime,
        retry_delay: float,
    ) -> float:
        if now >= current.end_at:
            return retry_delay
        remaining = (current.end_at - now).total_seconds()
        return max(0.1, min(retry_delay, remaining))
