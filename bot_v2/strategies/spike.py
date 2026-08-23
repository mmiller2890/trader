"""Deterministic spike strategy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from config.schema import SpikeStrategyConfig
from models.market import MarketSnapshot
from models.order import OrderResult
from models.signal import SignalSide, TradeSignal
from state.cache import MarketHistoryCache
from strategies.base import StrategyBase


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def _bps_change(reference: Decimal, current: Decimal) -> float:
    if reference <= 0:
        return 0.0
    return float(((current - reference) / reference) * Decimal("10000"))


class SpikeStrategy(StrategyBase):
    """Simple price-spike strategy with cooldown and liquidity filter."""

    def __init__(
        self,
        config: SpikeStrategyConfig,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._now = now
        self._history = MarketHistoryCache(max_points_per_market=max(200, config.lookback_ticks * 20))
        self._last_signal_at: dict[tuple[str, str], datetime] = {}

    @property
    def name(self) -> str:
        return "spike"

    def set_clock(self, now: Callable[[], datetime]) -> None:
        """Set the clock used for cooldowns and emitted signal timestamps."""

        self._now = now

    async def on_market_update(self, snapshot: MarketSnapshot) -> list[TradeSignal]:
        if not self._config.enabled:
            return []
        if self._config.target_market_ids and snapshot.market_id not in self._config.target_market_ids:
            return []
        if self._config.target_token_ids and snapshot.token_id not in self._config.target_token_ids:
            return []
        if (
            snapshot.top_bid_size < self._config.min_top_of_book_liquidity
            or snapshot.top_ask_size < self._config.min_top_of_book_liquidity
        ):
            await self._history.add_snapshot(snapshot)
            return []

        key = (snapshot.market_id, snapshot.token_id)
        history = await self._history.recent_snapshots(
            snapshot.market_id,
            snapshot.token_id,
            self._config.lookback_ticks,
        )
        await self._history.add_snapshot(snapshot)
        if len(history) < self._config.lookback_ticks:
            return []

        reference = history[0].mid_price
        move_bps = _bps_change(reference, snapshot.mid_price)
        if abs(move_bps) < self._config.spike_threshold_bps:
            return []
        if not self._cooldown_elapsed(key):
            return []

        side = self._choose_side(move_bps)
        if side is None:
            return []

        self._last_signal_at[key] = self._now()
        signal = TradeSignal(
            strategy_name=self.name,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            side=side,
            reference_price=reference,
            target_price=snapshot.mid_price,
            observed_move_bps=abs(move_bps),
            created_at=self._now(),
            reason=f"spike_{'up' if move_bps > 0 else 'down'}_{abs(move_bps):.2f}bps",
        )
        return [signal]

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        _ = order_result
        return []

    async def on_timer(self) -> list[TradeSignal]:
        return []

    def _cooldown_elapsed(self, key: tuple[str, str]) -> bool:
        if self._config.cooldown_seconds <= 0:
            return True
        previous = self._last_signal_at.get(key)
        if previous is None:
            return True
        return (self._now() - previous).total_seconds() >= self._config.cooldown_seconds

    def _choose_side(self, move_bps: float) -> SignalSide | None:
        if move_bps > 0 and self._config.emit_on_upward_spike:
            return SignalSide.SELL
        if move_bps < 0 and self._config.emit_on_downward_spike:
            return SignalSide.BUY
        return None
