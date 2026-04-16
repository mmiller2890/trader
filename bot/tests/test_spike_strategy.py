from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from config.schema import SpikeStrategyConfig
from models.market import MarketSnapshot
from strategies.spike import SpikeStrategy


def snapshot(mid_price: str) -> MarketSnapshot:
    value = Decimal(mid_price)
    now = datetime.now(tz=UTC)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=value - Decimal("0.01"),
        best_ask=value + Decimal("0.01"),
        mid_price=value,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        received_ts=now,
        source_ts=now,
    )


@pytest.mark.asyncio
async def test_spike_strategy_emits_signal_after_threshold_cross() -> None:
    strategy = SpikeStrategy(
        SpikeStrategyConfig(lookback_ticks=2, spike_threshold_bps=100, cooldown_seconds=30)
    )

    assert await strategy.on_market_update(snapshot("0.50")) == []
    assert await strategy.on_market_update(snapshot("0.50")) == []
    signals = await strategy.on_market_update(snapshot("0.53"))

    assert len(signals) == 1
    assert signals[0].strategy_name == "spike"
    assert signals[0].side.value == "sell"


@pytest.mark.asyncio
async def test_spike_strategy_respects_cooldown() -> None:
    strategy = SpikeStrategy(
        SpikeStrategyConfig(lookback_ticks=2, spike_threshold_bps=100, cooldown_seconds=999)
    )

    await strategy.on_market_update(snapshot("0.50"))
    await strategy.on_market_update(snapshot("0.50"))
    first = await strategy.on_market_update(snapshot("0.53"))
    second = await strategy.on_market_update(snapshot("0.56"))

    assert len(first) == 1
    assert second == []
