"""Reservation-aware position exit coordinator."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from config.schema import AppConfig
from models.events import BotEvent, EventType
from models.market import MarketSnapshot
from models.order import OrderTimeInForce
from models.position import ExitReason, Position, PositionLifecycle
from models.signal import SignalSide, SignalType, TradeSignal
from persistence.snapshots import SnapshotStore
from portfolio.exit_policy import PositionExitPolicy
from portfolio.sizing import fixed_size
from state.store import InMemoryStateStore


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class PositionExitManager:
    """Converts exit decisions and strategy SELLs into reserved exit signals."""

    def __init__(
        self,
        *,
        config: AppConfig,
        state_store: InMemoryStateStore,
        snapshots: SnapshotStore | None,
        policy: PositionExitPolicy,
        now: Callable[[], datetime] = utc_now,
        on_event: Callable[[BotEvent], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._snapshots = snapshots
        self._policy = policy
        self._now = now
        self._on_event = on_event
        self._dust_notified: set[tuple[str, str]] = set()

    def set_clock(self, now: Callable[[], datetime]) -> None:
        """Replace the clock used for retry timing."""

        self._now = now

    async def on_market_update(
        self,
        snapshot: MarketSnapshot,
        *,
        market_end_at: datetime | None,
    ) -> list[TradeSignal]:
        """Evaluate the exit policy for the position this snapshot belongs to."""

        if not self._config.position_management.enabled:
            return []
        signals: list[TradeSignal] = []
        for position in await self._state_store.get_positions():
            if (
                position.market_id,
                position.token_id,
            ) != (snapshot.market_id, snapshot.token_id):
                continue
            lifecycle = await self._state_store.get_position_lifecycle(
                position.market_id, position.token_id
            )
            if lifecycle is None:
                continue
            decision = self._policy.evaluate(
                position=position,
                lifecycle=lifecycle,
                snapshot=snapshot,
                now=self._now(),
            )
            if decision.dust:
                key = (position.market_id, position.token_id)
                if key not in self._dust_notified:
                    self._dust_notified.add(key)
                    await self._emit_event(
                        BotEvent(
                            event_type=EventType.POSITION_DUST,
                            component="exit_manager",
                            mode=self._config.bot.mode.value,
                            message="sub-minimum residual inventory marked as dust",
                            market_id=position.market_id,
                            token_id=position.token_id,
                            quantity=position.quantity,
                        )
                    )
                continue
            if not decision.should_exit or decision.reason is None:
                continue
            if lifecycle.exit_attempt_count >= self._config.position_management.max_exit_attempts:
                await self._activate_kill_switch(
                    f"exit_attempts_exhausted:{position.market_id}:{position.token_id}"
                )
                continue
            signal = await self._emit_exit(
                position=position,
                lifecycle=lifecycle,
                reason=decision.reason,
                requested_size=decision.requested_size,
                market_end_at=market_end_at,
            )
            if signal is not None:
                signals.append(signal)
        return signals

    async def from_strategy_signal(
        self,
        signal: TradeSignal,
        *,
        snapshot: MarketSnapshot,
        market_end_at: datetime | None,
    ) -> TradeSignal | None:
        """Convert a strategy SELL into a reserved exit when inventory exists."""

        if not self._config.position_management.enabled:
            return None
        if signal.side != SignalSide.SELL:
            return None
        if not self._config.position_management.exit_on_strategy_sell:
            return None
        position = await self._state_store.get_position(
            signal.market_id, signal.token_id
        )
        if position is None or position.quantity <= 0:
            return None
        lifecycle = await self._state_store.get_position_lifecycle(
            signal.market_id, signal.token_id
        )
        if lifecycle is None:
            return None
        if lifecycle.pending_exit_client_order_id is not None:
            return None
        return await self._emit_exit(
            position=position,
            lifecycle=lifecycle,
            reason=ExitReason.STRATEGY_SIGNAL,
            requested_size=position.quantity,
            market_end_at=market_end_at,
        )

    async def on_timer(
        self,
        *,
        market_end_lookup: Callable[[str], datetime | None],
    ) -> list[TradeSignal]:
        """Evaluate time-based exits for every open position."""

        if not self._config.position_management.enabled:
            return []
        signals: list[TradeSignal] = []
        for position in await self._state_store.get_positions():
            lifecycle = await self._state_store.get_position_lifecycle(
                position.market_id, position.token_id
            )
            if lifecycle is None:
                continue
            market_end_at = market_end_lookup(position.market_id)
            if market_end_at is not None and lifecycle.market_end_at != market_end_at:
                lifecycle = lifecycle.model_copy(update={"market_end_at": market_end_at})
            snapshot = await self._state_store.get_market_snapshot(
                position.market_id, position.token_id
            )
            decision = self._policy.evaluate(
                position=position,
                lifecycle=lifecycle,
                snapshot=snapshot,
                now=self._now(),
            )
            if not decision.should_exit or decision.reason is None:
                continue
            signal = await self._emit_exit(
                position=position,
                lifecycle=lifecycle,
                reason=decision.reason,
                requested_size=decision.requested_size,
                market_end_at=market_end_at,
            )
            if signal is not None:
                signals.append(signal)
        return signals

    async def _emit_exit(
        self,
        *,
        position: Position,
        lifecycle: PositionLifecycle,
        reason: ExitReason,
        requested_size: Decimal,
        market_end_at: datetime | None,
    ) -> TradeSignal | None:
        effective_size = (
            requested_size
            if self._config.position_management.liquidate_full_position
            else min(requested_size, fixed_size(self._config.execution))
        )
        if lifecycle.pending_exit_client_order_id is not None:
            return None
        if lifecycle.exit_attempt_count >= self._config.position_management.max_exit_attempts:
            return None
        if lifecycle.last_exit_attempt_at is not None:
            retry_after = lifecycle.last_exit_attempt_at + timedelta(
                seconds=self._config.position_management.exit_retry_interval_seconds
            )
            if self._now() < retry_after:
                return None

        signal_id = uuid4().hex
        client_order_id = (
            f"{self._config.execution.client_order_id_prefix}-{signal_id[:18]}"
        )
        reserved = await self._state_store.reserve_exit(
            position.market_id,
            position.token_id,
            client_order_id=client_order_id,
            reason=reason,
            attempted_at=self._now(),
        )
        if not reserved:
            return None
        if self._snapshots is not None:
            await self._snapshots.save_from_state(self._state_store)
        await self._emit_event(
            BotEvent(
                event_type=EventType.EXIT_TRIGGERED,
                component="exit_manager",
                mode=self._config.bot.mode.value,
                message="position exit triggered",
                market_id=position.market_id,
                token_id=position.token_id,
                client_order_id=client_order_id,
                reason=f"position_exit:{reason.value}",
                quantity=effective_size,
            )
        )
        return TradeSignal(
            signal_id=signal_id,
            strategy_name="position_exit",
            signal_type=SignalType.POSITION_EXIT,
            market_id=position.market_id,
            token_id=position.token_id,
            side=SignalSide.SELL,
            reference_price=position.average_entry_price,
            target_price=position.average_entry_price,
            observed_move_bps=0,
            created_at=self._now(),
            reason=f"position_exit:{reason.value}",
            requested_size=effective_size,
            reduce_only=True,
            time_in_force=self._config.position_management.exit_time_in_force,
        )

    async def _emit_event(self, event: BotEvent) -> None:
        if self._on_event is not None:
            await self._on_event(event)

    async def _activate_kill_switch(self, reason: str) -> bool:
        """Persist a newly activated halt before returning control."""

        activated = await self._state_store.activate_kill_switch(reason)
        if activated and self._snapshots is not None:
            await self._snapshots.save_from_state(self._state_store)
        return activated
