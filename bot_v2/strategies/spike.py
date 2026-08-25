"""Deterministic spike strategy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import SpikeStrategyConfig
from models.market import MarketSnapshot
from models.order import OrderResult
from models.signal import SignalSide, TradeSignal
from state.cache import MarketHistoryCache
from strategies.base import StrategyBase


#: Maps (market_id, token_id) to the market's other outcome token, if known.
ComplementProvider = Callable[[str, str], "str | None"]

#: Observed peak book-update rate per token on Polymarket crypto markets.
#: Used only to size retained history; age-based eviction does the real work.
ASSUMED_PEAK_UPDATES_PER_SECOND = 400

#: Hard ceiling on retained snapshots per token, so a burst cannot grow memory
#: without bound however long the configured window is.
MAX_HISTORY_POINTS = 20_000


def _history_capacity(config: SpikeStrategyConfig) -> int:
    """Retain enough history to actually span the configured lookback."""

    if config.lookback_seconds is None:
        return max(200, config.lookback_ticks * 20)
    needed = int(config.lookback_seconds * 1.5 * ASSUMED_PEAK_UPDATES_PER_SECOND)
    return max(200, min(needed, MAX_HISTORY_POINTS))


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
        complement_provider: ComplementProvider | None = None,
    ) -> None:
        self._config = config
        self._now = now
        self._complement_provider = complement_provider
        self._history = MarketHistoryCache(
            max_points_per_market=_history_capacity(config),
            max_age_seconds=(
                config.lookback_seconds * 1.5
                if config.lookback_seconds is not None
                else None
            ),
        )
        self._last_signal_at: dict[tuple[str, str], datetime] = {}

    def set_complement_provider(self, provider: ComplementProvider) -> None:
        """Set the lookup that pairs a token with the market's other outcome."""

        self._complement_provider = provider

    def _complement_of(self, market_id: str, token_id: str) -> str | None:
        if self._complement_provider is None:
            return None
        try:
            return self._complement_provider(market_id, token_id)
        except Exception:
            return None

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
        history = await self._window(snapshot)
        await self._history.add_snapshot(snapshot)
        if not history:
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

        direction = "up" if move_bps > 0 else "down"
        reason = f"spike_{direction}_{abs(move_bps):.2f}bps"

        if side == SignalSide.SELL and self._config.sell_via_complement:
            complement = self._complement_of(snapshot.market_id, snapshot.token_id)
            if complement is not None:
                # The complement is bought at ITS ask, which mirrors this
                # book's bid -- not its mid. On a wide book those differ by
                # the whole spread, and the mid flatters the entry badly.
                complement_price = Decimal("1") - snapshot.best_bid
                if not self._entry_price_allowed(complement_price):
                    return []
                self._last_signal_at[key] = self._now()
                # Selling YES needs YES on hand. Buying the paired NO at
                # 1 - p expresses the identical view and always executes.
                self._last_signal_at[(snapshot.market_id, complement)] = self._now()
                return [
                    TradeSignal(
                        strategy_name=self.name,
                        market_id=snapshot.market_id,
                        token_id=complement,
                        side=SignalSide.BUY,
                        reference_price=Decimal("1") - reference,
                        target_price=Decimal("1") - snapshot.mid_price,
                        observed_move_bps=abs(move_bps),
                        created_at=self._now(),
                        reason=f"{reason}_via_complement",
                    )
                ]

        # A buy fills at the ask, so that is the price to judge, not the mid.
        if side == SignalSide.BUY and not self._entry_price_allowed(
            snapshot.best_ask
        ):
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
            reason=reason,
        )
        return [signal]

    async def _window(self, snapshot: MarketSnapshot) -> list[MarketSnapshot]:
        """
        Return the history the move is measured against, or empty if too short.

        A time window is preferred when configured, because a fixed number of
        book updates spans an unpredictable amount of wall clock.
        """

        if self._config.lookback_seconds is not None:
            cutoff = self._now() - timedelta(seconds=self._config.lookback_seconds)
            window = await self._history.snapshots_since(
                snapshot.market_id, snapshot.token_id, cutoff
            )
            # Require the window to be genuinely spanned, not just one stale
            # point that happens to sit inside it.
            if len(window) < 2:
                return []
            span = (snapshot.received_ts - window[0].received_ts).total_seconds()
            if span < self._config.lookback_seconds / 2:
                return []
            return window

        window = await self._history.recent_snapshots(
            snapshot.market_id,
            snapshot.token_id,
            self._config.lookback_ticks,
        )
        if len(window) < self._config.lookback_ticks:
            return []
        return window

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        _ = order_result
        return []

    async def on_timer(self) -> list[TradeSignal]:
        return []

    def _entry_price_allowed(self, price: Decimal) -> bool:
        """
        Refuse entries where the payoff is lopsided against us.

        Near the bounds a fade risks almost the whole notional to win a few
        cents. Reversion frequency cannot rescue that reward/risk.
        """

        return (
            self._config.min_entry_price <= price <= self._config.max_entry_price
        )

    def _cooldown_elapsed(self, key: tuple[str, str]) -> bool:
        if self._config.cooldown_seconds <= 0:
            return True
        previous = self._last_signal_at.get(key)
        if previous is None:
            return True
        return (self._now() - previous).total_seconds() >= self._config.cooldown_seconds

    def _choose_side(self, move_bps: float) -> SignalSide | None:
        """
        Pick the side to trade a detected spike.

        Momentum goes with the move, reversion fades it. Either way a SELL is
        re-expressed as a BUY of the paired complement downstream, because
        Polymarket has no borrow.
        """

        momentum = self._config.direction == "momentum"
        if move_bps > 0 and self._config.emit_on_upward_spike:
            return SignalSide.BUY if momentum else SignalSide.SELL
        if move_bps < 0 and self._config.emit_on_downward_spike:
            return SignalSide.SELL if momentum else SignalSide.BUY
        return None
