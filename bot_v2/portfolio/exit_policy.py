"""Pure position exit policy."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from config.schema import PositionManagementConfig
from models.market import MarketSnapshot
from models.position import ExitReason, Position, PositionLifecycle
from pydantic import BaseModel, ConfigDict, Field


class ExitDecision(BaseModel):
    """Result of evaluating one position against the exit policy."""

    model_config = ConfigDict(extra="forbid")

    should_exit: bool
    reason: ExitReason | None = None
    requested_size: Decimal = Decimal("0")
    return_bps: Decimal | None = None
    dust: bool = False
    explanation: str


class PositionExitPolicy:
    """Deterministic exit decisions from position, lifecycle, and market state."""

    def __init__(
        self,
        config: PositionManagementConfig,
        *,
        min_order_size: Decimal,
        max_data_age_seconds: float,
    ) -> None:
        self._config = config
        self._min_order_size = min_order_size
        self._max_data_age_seconds = max_data_age_seconds

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
            )

        return_bps = (
            (snapshot.best_bid - position.average_entry_price)
            / position.average_entry_price
        ) * Decimal("10000")

        if return_bps <= -self._config.stop_loss_bps:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.STOP_LOSS,
                requested_size=position.quantity,
                return_bps=return_bps,
                explanation="stop_loss",
            )
        if return_bps >= self._config.take_profit_bps:
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TAKE_PROFIT,
                requested_size=position.quantity,
                return_bps=return_bps,
                explanation="take_profit",
            )
        if now - lifecycle.opened_at >= timedelta(seconds=self._config.max_hold_seconds):
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.MAX_HOLD,
                requested_size=position.quantity,
                return_bps=return_bps,
                explanation="max_hold",
            )

        return ExitDecision(
            should_exit=False,
            return_bps=return_bps,
            explanation="within_thresholds",
        )
