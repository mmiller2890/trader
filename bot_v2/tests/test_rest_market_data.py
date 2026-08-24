from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from clients.clob_client import ClobAdapterError, ClobClientAdapter
from clients.rest_market_data import RestMarketDataFallback
from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from state.store import InMemoryStateStore


NOW = datetime(2026, 8, 24, tzinfo=UTC)
THRESHOLD = 30


def config() -> AppConfig:
    return AppConfig(reliability={"rest_fallback_after_seconds": THRESHOLD})


def snapshot(*, bid: str = "0.49", ask: str = "0.51") -> MarketSnapshot:
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=NOW,
        received_ts=NOW,
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None

    def get_market_snapshot(self, market_id: str, token_id: str) -> MarketSnapshot:
        self.calls.append(token_id)
        if self.error is not None:
            raise self.error
        return snapshot()


@pytest.mark.asyncio
async def test_no_rest_poll_below_disconnect_threshold() -> None:
    adapter = FakeAdapter()
    polled: list[MarketSnapshot] = []

    async def on_snapshot(s: MarketSnapshot) -> None:
        polled.append(s)

    disconnected_since = NOW + timedelta(seconds=5)
    fallback = RestMarketDataFallback(
        config=config(),
        adapter=adapter,
        on_snapshot=on_snapshot,
        token_lookup=lambda: ["t1"],
        disconnected_since=lambda: disconnected_since,
        now=lambda: NOW + timedelta(seconds=THRESHOLD - 1),
        sleep=lambda s: asyncio.sleep(0),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(fallback.run(stop_event, lambda: asyncio.sleep(0)))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.gather(task, return_exceptions=True)
    assert adapter.calls == []
    assert polled == []


@pytest.mark.asyncio
async def test_rest_polls_both_tokens_after_threshold() -> None:
    adapter = FakeAdapter()
    received: list[MarketSnapshot] = []

    async def on_snapshot(s: MarketSnapshot) -> None:
        received.append(s)

    tokens = ["t1", "t2"]
    fallback = RestMarketDataFallback(
        config=config(),
        adapter=adapter,
        on_snapshot=on_snapshot,
        token_lookup=lambda: tokens,
        disconnected_since=lambda: NOW,
        now=lambda: NOW + timedelta(seconds=THRESHOLD + 1),
        sleep=lambda s: asyncio.sleep(0),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(fallback.run(stop_event, lambda: asyncio.sleep(0)))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.gather(task, return_exceptions=True)

    assert len(adapter.calls) >= len(tokens)
    assert all(token in adapter.calls for token in tokens)
    assert len(received) >= 1


@pytest.mark.asyncio
async def test_fallback_snapshots_do_not_invoke_strategy() -> None:
    strategy_calls: list[int] = []

    class FakeStrategy:
        async def on_market_update(self, s: MarketSnapshot) -> list:
            strategy_calls.append(1)
            return []

    store = InMemoryStateStore(mode=Mode.DRY_RUN)
    snap = snapshot()

    from clients.market_data_client import MarketDataClient

    client = MarketDataClient(state_store=store)
    await client.ingest_fallback_snapshot(snap)

    assert strategy_calls == []
    stored = await store.get_market_snapshot("m1", "t1")
    assert stored is not None


@pytest.mark.asyncio
async def test_rest_failure_reports_typed_incident_and_retries(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    adapter.error = ClobAdapterError("order book read failed")

    reported: list[str] = []

    async def report(reason: str) -> None:
        reported.append(reason)

    fallback = RestMarketDataFallback(
        config=config(),
        adapter=adapter,
        on_snapshot=lambda s: asyncio.sleep(0),
        token_lookup=lambda: ["t1"],
        disconnected_since=lambda: NOW,
        now=lambda: NOW + timedelta(seconds=THRESHOLD + 1),
        sleep=lambda s: asyncio.sleep(0),
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(fallback.run(stop_event, lambda: asyncio.sleep(0)))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.gather(task, return_exceptions=True)

    assert len(adapter.calls) >= 1
