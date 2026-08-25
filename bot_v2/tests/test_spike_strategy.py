from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.schema import SpikeStrategyConfig
from models.market import MarketSnapshot
from models.signal import SignalSide
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
    # Default direction is momentum: an upward spike is bought, not faded.
    assert signals[0].side.value == "buy"


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


def mm_snapshot(
    *, mid: str, token_id: str = "t1", at: datetime | None = None
) -> MarketSnapshot:
    m = Decimal(mid)
    when = at or datetime(2026, 1, 1, tzinfo=UTC)
    return MarketSnapshot(
        market_id="m1",
        token_id=token_id,
        best_bid=m - Decimal("0.01"),
        best_ask=m + Decimal("0.01"),
        mid_price=m,
        top_bid_size=Decimal("500"),
        top_ask_size=Decimal("500"),
        source_ts=when,
        received_ts=when,
    )


def reversion_config(**overrides: object) -> SpikeStrategyConfig:
    """
    Config pinned to reversion unless a test says otherwise.

    The shipped default is momentum; these cases were written against the
    fade and are pinned so they keep testing the behaviour they describe.
    """

    base: dict[str, object] = {
        "enabled": True,
        "direction": "reversion",
        "lookback_ticks": 3,
        "spike_threshold_bps": 45.0,
        "cooldown_seconds": 0.0,
        "min_top_of_book_liquidity": Decimal("1"),
    }
    base.update(overrides)
    return SpikeStrategyConfig(**base)


@pytest.mark.asyncio
async def test_upward_spike_buys_the_complement_instead_of_selling() -> None:
    strategy = SpikeStrategy(
        reversion_config(sell_via_complement=True),
        complement_provider=lambda market_id, token_id: "t2",
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert len(signals) == 1
    signal = signals[0]
    # Selling YES is unexecutable without inventory; buying NO is the same view.
    assert signal.side == SignalSide.BUY
    assert signal.token_id == "t2"
    assert "via_complement" in signal.reason
    # Prices are mirrored into the complement's frame.
    assert signal.target_price == Decimal("0.40")
    assert signal.reference_price == Decimal("0.50")


@pytest.mark.asyncio
async def test_downward_spike_still_buys_the_observed_token() -> None:
    strategy = SpikeStrategy(
        reversion_config(sell_via_complement=True),
        complement_provider=lambda market_id, token_id: "t2",
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.40"))

    assert len(signals) == 1
    assert signals[0].side == SignalSide.BUY
    assert signals[0].token_id == "t1"
    assert "via_complement" not in signals[0].reason


@pytest.mark.asyncio
async def test_unknown_complement_falls_back_to_a_plain_sell() -> None:
    strategy = SpikeStrategy(
        reversion_config(sell_via_complement=True),
        complement_provider=lambda market_id, token_id: None,
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert signals[0].side == SignalSide.SELL
    assert signals[0].token_id == "t1"


@pytest.mark.asyncio
async def test_complement_routing_can_be_disabled() -> None:
    strategy = SpikeStrategy(
        reversion_config(sell_via_complement=False),
        complement_provider=lambda market_id, token_id: "t2",
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert signals[0].side == SignalSide.SELL


@pytest.mark.asyncio
async def test_a_failing_complement_lookup_does_not_break_signalling() -> None:
    def broken(market_id: str, token_id: str) -> str | None:
        raise RuntimeError("rotator unavailable")

    strategy = SpikeStrategy(
        reversion_config(sell_via_complement=True), complement_provider=broken
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert signals[0].side == SignalSide.SELL


@pytest.mark.asyncio
async def test_time_lookback_measures_wall_clock_not_update_count() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": start}
    strategy = SpikeStrategy(
        reversion_config(lookback_seconds=10.0),
        now=lambda: clock["now"],
    )

    # A burst of updates spanning only milliseconds must not qualify, however
    # many of them arrive.
    for offset_ms in range(0, 200, 10):
        at = start + timedelta(milliseconds=offset_ms)
        clock["now"] = at
        signals = await strategy.on_market_update(mm_snapshot(mid="0.50", at=at))
        assert signals == []

    burst_end = start + timedelta(milliseconds=200)
    clock["now"] = burst_end
    assert await strategy.on_market_update(
        mm_snapshot(mid="0.60", at=burst_end)
    ) == []


@pytest.mark.asyncio
async def test_time_lookback_fires_once_the_window_is_genuinely_spanned() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": start}
    strategy = SpikeStrategy(
        reversion_config(lookback_seconds=10.0),
        now=lambda: clock["now"],
    )
    for seconds in (0, 4, 8):
        at = start + timedelta(seconds=seconds)
        clock["now"] = at
        await strategy.on_market_update(mm_snapshot(mid="0.50", at=at))

    at = start + timedelta(seconds=9)
    clock["now"] = at
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60", at=at))

    assert len(signals) == 1
    assert signals[0].observed_move_bps > 45


@pytest.mark.asyncio
async def test_history_retains_enough_updates_to_span_the_time_window() -> None:
    """
    Regression: a count-bounded cache silently starves a time-based lookback.

    At a few hundred book updates per second a 200-point history holds under a
    second, so a 20-second window never fills and the strategy goes permanently
    silent -- with no error to explain why.
    """

    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": start}
    strategy = SpikeStrategy(
        reversion_config(lookback_seconds=20.0),
        now=lambda: clock["now"],
    )

    # 12 seconds of history at 100 updates/sec -- far more than 200 points.
    for step in range(1200):
        at = start + timedelta(milliseconds=step * 10)
        clock["now"] = at
        await strategy.on_market_update(mm_snapshot(mid="0.50", at=at))

    at = start + timedelta(seconds=12)
    clock["now"] = at
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60", at=at))

    assert len(signals) == 1


@pytest.mark.asyncio
async def test_history_does_not_grow_without_bound() -> None:
    from strategies.spike import MAX_HISTORY_POINTS, _history_capacity

    assert _history_capacity(reversion_config(lookback_seconds=3600.0)) == (
        MAX_HISTORY_POINTS
    )
    # A tick-count config keeps the original, much smaller sizing.
    assert _history_capacity(reversion_config(lookback_ticks=3)) == 200


@pytest.mark.asyncio
async def test_entry_near_the_upper_bound_is_refused() -> None:
    strategy = SpikeStrategy(
        reversion_config(min_entry_price=Decimal("0.10"), max_entry_price=Decimal("0.90"))
    )
    for mid in ("0.95", "0.95", "0.95"):
        await strategy.on_market_update(mm_snapshot(mid=mid))

    # A downward spike would normally BUY here, but at 0.92 the fade risks
    # 92 cents to make 8.
    assert await strategy.on_market_update(mm_snapshot(mid="0.92")) == []


@pytest.mark.asyncio
async def test_entry_near_the_lower_bound_is_refused() -> None:
    strategy = SpikeStrategy(
        reversion_config(min_entry_price=Decimal("0.10"), max_entry_price=Decimal("0.90"))
    )
    for mid in ("0.20", "0.20", "0.20"):
        await strategy.on_market_update(mm_snapshot(mid=mid))

    # A downward spike to 0.05 would BUY there; 5 cents of downside against
    # 95 of upside sounds good until you notice how rarely it resolves YES.
    assert await strategy.on_market_update(mm_snapshot(mid="0.05")) == []


@pytest.mark.asyncio
async def test_complement_entry_is_checked_against_the_band_too() -> None:
    """
    Regression: a YES token collapsing to 0.03 routed into a 0.97 NO entry.

    The band has to be applied in the complement's own price frame, not the
    observed token's, or the check passes on the wrong number.
    """

    strategy = SpikeStrategy(
        reversion_config(
            min_entry_price=Decimal("0.10"), max_entry_price=Decimal("0.90")
        ),
        complement_provider=lambda market_id, token_id: "t2",
    )
    for mid in ("0.20", "0.20", "0.20"):
        await strategy.on_market_update(mm_snapshot(mid=mid))

    # YES spikes up to 0.88 -> fade means buying NO at 0.12, inside the band.
    inside = await strategy.on_market_update(mm_snapshot(mid="0.88"))
    assert len(inside) == 1
    assert inside[0].target_price == Decimal("0.12")


@pytest.mark.asyncio
async def test_complement_entry_outside_the_band_is_refused() -> None:
    strategy = SpikeStrategy(
        reversion_config(
            min_entry_price=Decimal("0.10"), max_entry_price=Decimal("0.90")
        ),
        complement_provider=lambda market_id, token_id: "t2",
    )
    for mid in ("0.20", "0.20", "0.20"):
        await strategy.on_market_update(mm_snapshot(mid=mid))

    # YES spikes to 0.97 -> NO entry would be 0.03, outside the band.
    assert await strategy.on_market_update(mm_snapshot(mid="0.03")) == []


@pytest.mark.asyncio
async def test_a_refused_entry_does_not_burn_the_cooldown() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    clock = {"now": start}
    strategy = SpikeStrategy(
        reversion_config(
            cooldown_seconds=60.0,
            min_entry_price=Decimal("0.10"),
            max_entry_price=Decimal("0.90"),
        ),
        now=lambda: clock["now"],
    )
    for mid in ("0.95", "0.95", "0.95"):
        await strategy.on_market_update(mm_snapshot(mid=mid, at=start))

    # Refused for price band...
    assert await strategy.on_market_update(mm_snapshot(mid="0.92", at=start)) == []
    # ...so a valid entry moments later is still allowed.
    later = start + timedelta(seconds=1)
    clock["now"] = later
    signals = await strategy.on_market_update(mm_snapshot(mid="0.50", at=later))
    assert len(signals) == 1


@pytest.mark.asyncio
async def test_momentum_buys_an_upward_spike() -> None:
    strategy = SpikeStrategy(
        reversion_config(direction="momentum", sell_via_complement=False)
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert len(signals) == 1
    assert signals[0].side == SignalSide.BUY
    assert signals[0].token_id == "t1"


@pytest.mark.asyncio
async def test_momentum_sells_a_downward_spike() -> None:
    strategy = SpikeStrategy(
        reversion_config(direction="momentum", sell_via_complement=False)
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.40"))

    assert len(signals) == 1
    assert signals[0].side == SignalSide.SELL


@pytest.mark.asyncio
async def test_momentum_downward_spike_routes_through_the_complement() -> None:
    # Selling needs inventory; buying the paired token is the same trade.
    strategy = SpikeStrategy(
        reversion_config(direction="momentum", sell_via_complement=True),
        complement_provider=lambda market_id, token_id: "t2",
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.40"))

    assert len(signals) == 1
    assert signals[0].side == SignalSide.BUY
    assert signals[0].token_id == "t2"
    assert "via_complement" in signals[0].reason


@pytest.mark.asyncio
async def test_direction_flag_produces_exactly_opposite_sides() -> None:
    """The A/B has to be a clean inversion, not two loosely related configs."""

    async def side_for(direction: str, spike_to: str) -> SignalSide:
        strategy = SpikeStrategy(
            reversion_config(direction=direction, sell_via_complement=False)
        )
        for mid in ("0.50", "0.50", "0.50"):
            await strategy.on_market_update(mm_snapshot(mid=mid))
        signals = await strategy.on_market_update(mm_snapshot(mid=spike_to))
        return signals[0].side

    assert await side_for("momentum", "0.60") != await side_for("reversion", "0.60")
    assert await side_for("momentum", "0.40") != await side_for("reversion", "0.40")


@pytest.mark.asyncio
async def test_reversion_direction_still_available_for_comparison() -> None:
    strategy = SpikeStrategy(
        reversion_config(direction="reversion", sell_via_complement=False)
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert signals[0].side == SignalSide.SELL
