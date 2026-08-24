"""REST market-data fallback for exit/reconciliation during WS outages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from clients.clob_client import ClobAdapterError, ClobClientAdapter
from config.schema import AppConfig
from models.market import MarketSnapshot

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


Heartbeat = Callable[[], Awaitable[None]]
Report = Callable[[str], Awaitable[None]]


class RestMarketDataFallback:
    """Polls current tokens via REST after the WS disconnect threshold.

    Snapshots feed only exit/reconciliation paths; entry strategy is never
    evaluated from fallback data.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        adapter: ClobClientAdapter,
        on_snapshot: Callable[[MarketSnapshot], Awaitable[None]],
        token_lookup: Callable[[], list[str]],
        disconnected_since: Callable[[], datetime | None],
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._on_snapshot = on_snapshot
        self._token_lookup = token_lookup
        self._disconnected_since = disconnected_since
        self._now = now
        self._sleep = sleep
        self.threshold = config.reliability.rest_fallback_after_seconds

    async def run(self, stop_event: asyncio.Event, heartbeat: Heartbeat) -> None:
        while not stop_event.is_set():
            since = self._disconnected_since()
            if since is None:
                await self._sleep(1.0)
                await heartbeat()
                continue
            outage_seconds = (self._now() - since).total_seconds()
            if outage_seconds < self.threshold:
                await self._sleep(1.0)
                await heartbeat()
                continue

            tokens = self._token_lookup()
            for token_id in tokens:
                try:
                    snapshot = await self.fetch_snapshot(token_id)
                except Exception:
                    logger.warning(
                        "rest fallback poll failed",
                        extra={
                            "component": "rest_market_data",
                            "event_type": "rest_poll_failed",
                            "reason": "poll_error",
                        },
                    )
                    continue
                await self._on_snapshot(snapshot)
            await heartbeat()
            await self._sleep(2.0)

    async def fetch_snapshot(self, token_id: str) -> MarketSnapshot:
        return await asyncio.to_thread(
            self._adapter.get_market_snapshot, "fallback", token_id
        )
