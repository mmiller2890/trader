from __future__ import annotations

from decimal import Decimal

import pytest

from clients.market_data_client import MarketDataClient
from config.schema import Mode
from state.store import InMemoryStateStore


@pytest.mark.asyncio
async def test_book_event_computes_true_best_levels_not_first_entries() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    client = MarketDataClient(state_store=state)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.48", "size": "10"}, {"price": "0.50", "size": "3"}],
        "asks": [{"price": "0.54", "size": "8"}, {"price": "0.52", "size": "4"}],
        "timestamp": "1757908892351",
    })
    snapshot = await state.get_market_snapshot("m1", "t1")
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.50")
    assert snapshot.best_ask == Decimal("0.52")
    assert snapshot.top_bid_size == Decimal("3")
    assert snapshot.top_ask_size == Decimal("4")


@pytest.mark.asyncio
async def test_price_change_deletes_zero_and_upserts_nonzero_levels() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    client = MarketDataClient(state_store=state)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.50", "size": "10"}, {"price": "0.49", "size": "5"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892351",
    })
    await client.handle_ws_message({
        "event_type": "price_change",
        "market": "m1",
        "timestamp": "1757908892352",
        "price_changes": [
            {"asset_id": "t1", "price": "0.50", "size": "0", "side": "BUY"},
            {"asset_id": "t1", "price": "0.495", "size": "7", "side": "BUY"},
        ],
    })
    snapshot = await state.get_market_snapshot("m1", "t1")
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.495")
    assert snapshot.top_bid_size == Decimal("7")


@pytest.mark.asyncio
async def test_snapshot_is_not_published_until_both_sides_exist() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    published: list[object] = []

    async def on_snapshot(snapshot: object) -> None:
        published.append(snapshot)

    client = MarketDataClient(state_store=state, on_snapshot=on_snapshot)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [],
        "timestamp": "1757908892351",
    })
    assert published == []
    assert await state.get_market_snapshot("m1", "t1") is None


@pytest.mark.asyncio
async def test_tick_size_change_is_stored_without_altering_depth() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    client = MarketDataClient(state_store=state)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892351",
    })
    await client.handle_ws_message({
        "event_type": "tick_size_change",
        "market": "m1",
        "asset_id": "t1",
        "new_tick_size": "0.01",
        "timestamp": "1757908892352",
    })
    book = client._books[("m1", "t1")]
    assert book.tick_size == Decimal("0.01")
    snapshot = await state.get_market_snapshot("m1", "t1")
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.50")


@pytest.mark.asyncio
async def test_market_resolved_stops_snapshot_routing() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    published: list[object] = []

    async def on_snapshot(snapshot: object) -> None:
        published.append(snapshot)

    client = MarketDataClient(state_store=state, on_snapshot=on_snapshot)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892351",
    })
    assert len(published) == 1
    await client.handle_ws_message({
        "event_type": "market_resolved",
        "market": "m1",
        "asset_id": "t1",
        "timestamp": "1757908892352",
    })
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.51", "size": "10"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892353",
    })
    assert len(published) == 1


@pytest.mark.asyncio
async def test_last_trade_price_updates_without_altering_depth() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    client = MarketDataClient(state_store=state)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892351",
    })
    await client.handle_ws_message({
        "event_type": "last_trade_price",
        "market": "m1",
        "asset_id": "t1",
        "price": "0.51",
        "timestamp": "1757908892352",
    })
    snapshot = await state.get_market_snapshot("m1", "t1")
    assert snapshot is not None
    assert snapshot.last_trade_price == Decimal("0.51")
    assert snapshot.best_bid == Decimal("0.50")
    assert snapshot.best_ask == Decimal("0.52")


@pytest.mark.asyncio
async def test_unknown_event_type_is_ignored_without_exception() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    client = MarketDataClient(state_store=state)
    await client.handle_ws_message({"event_type": "something_new", "market": "m1"})
    assert await state.get_market_snapshot("m1", "t1") is None


@pytest.mark.asyncio
async def test_malformed_book_event_does_not_mutate_state() -> None:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    client = MarketDataClient(state_store=state)
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.50", "size": "10"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892351",
    })
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.60", "size": "10"}],
        "asks": [{"price": "0.52", "size": "8"}],
        "timestamp": "1757908892352",
    })
    snapshot = await state.get_market_snapshot("m1", "t1")
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.50")
