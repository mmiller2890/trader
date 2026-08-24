from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from clients.gamma_markets import (
    DiscoveredMarket,
    MarketDiscoveryError,
    MarketOutcome,
)
from clients.market_rotation import Btc15mMarketRotator, MarketRotationState
from config.schema import AutomaticMarketConfig


CURRENT_START = datetime(2026, 8, 24, 2, 45, tzinfo=UTC)


def market(window: int, up: str, down: str) -> DiscoveredMarket:
    start = CURRENT_START + timedelta(minutes=15 * window)
    return DiscoveredMarket(
        event_id=f"event-{window}",
        market_id=f"market-{window}",
        condition_id=f"condition-{window}",
        slug=f"btc-updown-15m-{int(start.timestamp())}",
        title="Bitcoin Up or Down",
        start_at=start,
        end_at=start + timedelta(minutes=15),
        up=MarketOutcome(name="Up", token_id=up),
        down=MarketOutcome(name="Down", token_id=down),
    )


class FakeDiscovery:
    def __init__(self, results: list[DiscoveredMarket | Exception]) -> None:
        self.results = list(results)
        self.calls: list[datetime] = []
        self.close_calls = 0

    async def discover_active(self, now: datetime | None = None) -> DiscoveredMarket:
        assert now is not None
        self.calls.append(now)
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def close(self) -> None:
        self.close_calls += 1


class FakeWebSocket:
    def __init__(self) -> None:
        self.replacements: list[list[str]] = []

    async def replace_asset_ids(self, asset_ids: list[str]) -> bool:
        self.replacements.append(list(asset_ids))
        return True


@pytest.mark.asyncio
async def test_initialize_sets_healthy_current_market_without_replacement() -> None:
    now = datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC)
    discovery = FakeDiscovery([market(0, "111", "222")])
    websocket = FakeWebSocket()
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True),
        discovery=discovery,
        websocket=websocket,
        clock=lambda: now,
    )

    discovered = await rotator.initialize()

    status = rotator.status()
    assert discovered.asset_ids == ["111", "222"]
    assert status.state == MarketRotationState.HEALTHY
    assert status.current_market == discovered
    assert status.last_success_at == now
    assert status.reason == "market_discovered"
    assert websocket.replacements == []


@pytest.mark.asyncio
async def test_initialize_uses_seed_market_without_second_discovery() -> None:
    now = datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC)
    current = market(0, "111", "222")
    discovery = FakeDiscovery([current])
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True),
        discovery=discovery,
        websocket=FakeWebSocket(),
        initial_market=current,
        clock=lambda: now,
    )

    assert await rotator.initialize() == current
    assert discovery.calls == []


@pytest.mark.asyncio
async def test_initialize_failure_sets_failed_status() -> None:
    error = MarketDiscoveryError("gamma_unavailable")
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True),
        discovery=FakeDiscovery([error]),
        websocket=FakeWebSocket(),
        clock=lambda: datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC),
    )

    with pytest.raises(MarketDiscoveryError) as captured:
        await rotator.initialize()

    assert captured.value.reason == "gamma_unavailable"
    assert rotator.status().state == MarketRotationState.FAILED
    assert rotator.status().reason == "gamma_unavailable"


@pytest.mark.asyncio
async def test_run_waits_for_boundary_and_replaces_changed_market() -> None:
    current = market(0, "111", "222")
    next_market = market(1, "333", "444")
    now = [datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC)]
    stop_event = asyncio.Event()
    waits: list[float] = []
    websocket = FakeWebSocket()

    async def waiter(event: asyncio.Event, delay: float) -> bool:
        waits.append(delay)
        now[0] += timedelta(seconds=delay)
        if websocket.replacements:
            event.set()
            return True
        return False

    discovery = FakeDiscovery([current, current, current, next_market])
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True, refresh_lead_seconds=2),
        discovery=discovery,
        websocket=websocket,
        clock=lambda: now[0],
        waiter=waiter,
    )

    await rotator.initialize()
    await rotator.run(stop_event)

    assert waits == [8.0, 1.0, 1.0, 898.0]
    assert websocket.replacements == [["333", "444"]]
    assert rotator.status().state == MarketRotationState.HEALTHY
    assert rotator.status().current_market == next_market
    assert rotator.status().last_success_at == datetime(
        2026, 8, 24, 3, 0, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_run_marks_discovery_failure_degraded_and_retries() -> None:
    current = market(0, "111", "222")
    now = [datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC)]
    stop_event = asyncio.Event()
    waits: list[float] = []

    async def waiter(event: asyncio.Event, delay: float) -> bool:
        waits.append(delay)
        now[0] += timedelta(seconds=delay)
        if len(waits) == 2:
            event.set()
            return True
        return False

    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True, refresh_lead_seconds=2),
        discovery=FakeDiscovery(
            [current, MarketDiscoveryError("gamma_unavailable")]
        ),
        websocket=FakeWebSocket(),
        clock=lambda: now[0],
        waiter=waiter,
    )

    await rotator.initialize()
    await rotator.run(stop_event)

    assert waits == [8.0, 1.0]
    assert rotator.status().state == MarketRotationState.DEGRADED
    assert rotator.status().reason == "gamma_unavailable"


@pytest.mark.asyncio
async def test_run_caps_retry_delay_to_remaining_time_before_boundary() -> None:
    current = market(0, "111", "222")
    now = [datetime(2026, 8, 24, 2, 59, 55, tzinfo=UTC)]
    stop_event = asyncio.Event()
    waits: list[float] = []

    async def waiter(event: asyncio.Event, delay: float) -> bool:
        waits.append(delay)
        now[0] += timedelta(seconds=delay)
        if len(waits) == 3:
            event.set()
            return True
        return False

    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True, refresh_lead_seconds=2),
        discovery=FakeDiscovery(
            [current, MarketDiscoveryError("gamma_unavailable")]
        ),
        websocket=FakeWebSocket(),
        clock=lambda: now[0],
        waiter=waiter,
    )

    await rotator.initialize()
    await rotator.run(stop_event)

    assert waits == [3.0, 1.0, 1.0]
    assert rotator.status().state == MarketRotationState.DEGRADED
    assert rotator.status().reason == "gamma_unavailable"


@pytest.mark.asyncio
async def test_stop_closes_discovery_once() -> None:
    discovery = FakeDiscovery([market(0, "111", "222")])
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True),
        discovery=discovery,
        websocket=FakeWebSocket(),
    )

    await rotator.stop()
    await rotator.stop()

    assert discovery.close_calls == 1


def test_mark_failed_makes_dead_rotation_visible() -> None:
    current = market(0, "111", "222")
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True),
        discovery=FakeDiscovery([current]),
        websocket=FakeWebSocket(),
        initial_market=current,
    )

    rotator.mark_failed("RuntimeError")

    assert rotator.status().state == MarketRotationState.FAILED
    assert rotator.status().current_market == current
    assert rotator.status().reason == "RuntimeError"


class RepeatingDiscovery(FakeDiscovery):
    def __init__(self, results: list[DiscoveredMarket | Exception]) -> None:
        super().__init__(results)
        self._all = list(results)

    async def discover_active(self, now: datetime | None = None) -> DiscoveredMarket:
        assert now is not None
        self.calls.append(now)
        result = self._all[min(len(self.calls) - 1, len(self._all) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_rotation_gate_blocks_replacement_until_position_exits() -> None:
    current = market(0, "111", "222")
    next_market = market(1, "333", "444")
    now = [datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC)]
    stop_event = asyncio.Event()
    waits: list[float] = []
    websocket = FakeWebSocket()
    gate = {"allow": False}
    gate_open = asyncio.Event()

    async def can_rotate(market: DiscoveredMarket) -> bool:
        return gate["allow"]

    async def waiter(event: asyncio.Event, delay: float) -> bool:
        waits.append(delay)
        if len(waits) == 1:
            now[0] += timedelta(seconds=delay)
            return False
        if not gate["allow"]:
            await gate_open.wait()
            return False
        now[0] += timedelta(seconds=delay)
        if websocket.replacements:
            event.set()
            return True
        return False

    discovery = RepeatingDiscovery([current, next_market, next_market])
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True, refresh_lead_seconds=2),
        discovery=discovery,
        websocket=websocket,
        clock=lambda: now[0],
        waiter=waiter,
        can_rotate=can_rotate,
    )

    await rotator.initialize()
    run_task = asyncio.create_task(rotator.run(stop_event))
    await asyncio.sleep(0.05)
    assert websocket.replacements == []
    assert rotator.status().state == MarketRotationState.DEGRADED
    assert rotator.status().reason == "position_exit_pending"

    gate["allow"] = True
    gate_open.set()
    await asyncio.sleep(0.05)
    assert websocket.replacements == [["333", "444"]]
    stop_event.set()
    await run_task


@pytest.mark.asyncio
async def test_rotation_gate_raises_when_market_ends_with_position() -> None:
    current = market(0, "111", "222")
    next_market = market(1, "333", "444")
    now = [datetime(2026, 8, 24, 2, 59, 50, tzinfo=UTC)]
    stop_event = asyncio.Event()
    websocket = FakeWebSocket()

    async def waiter(event: asyncio.Event, delay: float) -> bool:
        now[0] += timedelta(seconds=delay)
        return False

    async def can_rotate(market: DiscoveredMarket) -> bool:
        return False

    discovery = RepeatingDiscovery([current, current, next_market])
    rotator = Btc15mMarketRotator(
        config=AutomaticMarketConfig(enabled=True, refresh_lead_seconds=2),
        discovery=discovery,
        websocket=websocket,
        clock=lambda: now[0],
        waiter=waiter,
        can_rotate=can_rotate,
    )

    await rotator.initialize()
    with pytest.raises(RuntimeError, match="position_open_at_market_end"):
        await rotator.run(stop_event)
