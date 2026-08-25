"""Pure position exit policy."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from config.schema import PositionManagementConfig
from models.market import MarketSnapshot
from models.position import ExitReason, Position, PositionLifecycle
from models.tick import DEFAULT_TICK_SIZE
from pydantic import BaseModel, ConfigDict, Field


class ExitDecision(BaseModel):
    """Result of evaluating one position against the exit policy."""

    model_config = ConfigDict(extra="forbid")

    should_exit: bool
    reason: ExitReason | None = None
    requested_size: Decimal = Decimal("0")
    return_bps: Decimal | None = None
    effective_take_profit_bps: Decimal | None = None
    effective_stop_loss_bps: Decimal | None = None
    dust: bool = False
    explanation: str
    use_maker: bool = False


class PositionExitPolicy:
    """Deterministic exit decisions from position, lifecycle, and market state."""

    def __init__(
        self,
        config: PositionManagementConfig,
        *,
        min_order_size: Decimal,
        max_data_age_seconds: float,
        tick_size_provider: Callable[[str], Decimal] | None = None,
        default_tick_size: Decimal = DEFAULT_TICK_SIZE,
    ) -> None:
        self._config = config
        self._min_order_size = min_order_size
        self._max_data_age_seconds = max_data_age_seconds
        self._tick_size_provider = tick_size_provider
        self._default_tick_size = default_tick_size

    def tick_size_for(self, token_id: str) -> Decimal:
        """Resolve the exchange tick size for one token."""

        if self._tick_size_provider is None:
            return self._default_tick_size
        try:
            return self._tick_size_provider(token_id)
        except Exception:
            return self._default_tick_size

    def effective_thresholds(
        self,
        *,
        entry_price: Decimal,
        snapshot: MarketSnapshot,
    ) -> tuple[Decimal, Decimal]:
        """
        Return the take-profit and stop-loss thresholds actually in force.

        The configured bps values are treated as a *minimum ambition*, not the
        final answer. Both are raised to clear whichever is larger: a whole
        number of ticks, or a multiple of the live spread. Without this a stop
        expressed in bps can sit inside the spread, in which case marking a
        fresh entry against the bid trips it before the market has moved.
        """

        tick = self.tick_size_for(snapshot.token_id)
        floors_bps: list[Decimal] = []

        if self._config.min_edge_ticks > 0:
            floors_bps.append(
                (self._config.min_edge_ticks * tick / entry_price) * Decimal("10000")
            )
        spread = snapshot.best_ask - snapshot.best_bid
        if self._config.spread_floor_multiple > 0 and spread > 0:
            floors_bps.append(
                (self._config.spread_floor_multiple * spread / entry_price)
                * Decimal("10000")
            )
        take_profit = max([self._config.take_profit_bps, *floors_bps])

        stop_floors_bps: list[Decimal] = []
        if self._config.min_stop_ticks > 0:
            stop_floors_bps.append(
                (self._config.min_stop_ticks * tick / entry_price) * Decimal("10000")
            )
        if self._config.spread_floor_multiple > 0 and spread > 0:
            stop_floors_bps.append(
                (self._config.spread_floor_multiple * spread / entry_price)
                * Decimal("10000")
            )
        stop_loss = max([self._config.stop_loss_bps, *stop_floors_bps])
        return take_profit, stop_loss

    def _use_maker(
        self,
        *,
        lifecycle: PositionLifecycle,
        reason: ExitReason,
        now: datetime,
    ) -> bool:
        """
        Rest the exit only when there is time for it to fill.

        Expiry always crosses: an unfilled resting exit at resolution leaves the
        full notional on a coin flip, which is strictly worse than paying the
        spread to be certain. This check runs before the deadline is even
        consulted, so no deadline configuration can produce a resting expiry
        exit.
        """

        if self._config.exit_style != "maker_first":
            return False
        if reason == ExitReason.MARKET_EXPIRY:
            return False
        started = lifecycle.exit_first_attempted_at
        if started is None:
            return True
        elapsed = (now - started).total_seconds()
        return elapsed < self._config.maker_exit_deadline_seconds

    def evaluate(
        self,
        *,
        position: Position,
        lifecycle: PositionLifecycle,
        snapshot: MarketSnapshot | None,
        now: datetime,
    ) -> ExitDecision:
        """Evaluate exit priority: expiry, stop loss, take profit, max hold."""

        if position.quantity <= 0:
            return ExitDecision(
                should_exit=False,
                explanation="no_inventory",
            )
        if lifecycle.pending_exit_client_order_id is not None:
            return ExitDecision(
                should_exit=False,
                explanation="exit_pending",
            )
        if position.quantity < self._min_order_size:
            return ExitDecision(
                should_exit=False,
                dust=True,
                explanation="dust_below_min_order_size",
            )
        if snapshot is None:
            return ExitDecision(
                should_exit=False,
                explanation="snapshot_missing",
            )
        if now - snapshot.received_ts > timedelta(seconds=self._max_data_age_seconds):
            return ExitDecision(
                should_exit=False,
                explanation="snapshot_stale",
            )
        if position.average_entry_price <= 0:
            return ExitDecision(
                should_exit=False,
                explanation="entry_price_zero",
            )

        if (
            lifecycle.market_end_at is not None
            and now
            >= lifecycle.market_end_at
            - timedelta(seconds=self._config.exit_before_market_end_seconds)
        ):
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.MARKET_EXPIRY,
                requested_size=position.quantity,
                explanation="market_expiry",
                use_maker=self._use_maker(
                    lifecycle=lifecycle, reason=ExitReason.MARKET_EXPIRY, now=now
                ),
            )

        return_bps = (
            (snapshot.best_bid - position.average_entry_price)
            / position.average_entry_price
        ) * Decimal("10000")
        take_profit_bps, stop_loss_bps = self.effective_thresholds(
            entry_price=position.average_entry_price,
            snapshot=snapshot,
        )

        if return_bps <= -stop_loss_bps:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.STOP_LOSS,
                requested_size=position.quantity,
                return_bps=return_bps,
                effective_take_profit_bps=take_profit_bps,
                effective_stop_loss_bps=stop_loss_bps,
                explanation="stop_loss",
                use_maker=self._use_maker(
                    lifecycle=lifecycle, reason=ExitReason.STOP_LOSS, now=now
                ),
            )
        if return_bps >= take_profit_bps:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TAKE_PROFIT,
                requested_size=position.quantity,
                return_bps=return_bps,
                effective_take_profit_bps=take_profit_bps,
                effective_stop_loss_bps=stop_loss_bps,
                explanation="take_profit",
                use_maker=self._use_maker(
                    lifecycle=lifecycle, reason=ExitReason.TAKE_PROFIT, now=now
                ),
            )
        if now - lifecycle.opened_at >= timedelta(seconds=self._config.max_hold_seconds):
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.MAX_HOLD,
                requested_size=position.quantity,
                return_bps=return_bps,
                effective_take_profit_bps=take_profit_bps,
                effective_stop_loss_bps=stop_loss_bps,
                explanation="max_hold",
                use_maker=self._use_maker(
                    lifecycle=lifecycle, reason=ExitReason.MAX_HOLD, now=now
                ),
            )

        return ExitDecision(
            should_exit=False,
            return_bps=return_bps,
            effective_take_profit_bps=take_profit_bps,
            effective_stop_loss_bps=stop_loss_bps,
            explanation="within_thresholds",
        )
