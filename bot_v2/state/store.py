"""Async-safe in-memory state store."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import Mode
from models.fees import maker_fee, taker_fee
from models.market import MarketSnapshot, OrderBookUpdate
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import (
    Balance,
    ExitReason,
    FillApplication,
    FillCheckpoint,
    Position,
    PositionLifecycle,
    PositionMergeResult,
)
from models.signal import TradeSignal
from models.operations import OperationalState
from portfolio.exposure import total_marked_exposure


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


MarketTokenKey = tuple[str, str]


class PositionAccountingError(RuntimeError):
    """Raised when a confirmed fill violates a position accounting invariant."""


class InMemoryStateStore:
    """Holds runtime state for strategy/risk/execution components."""

    def __init__(
        self,
        *,
        mode: Mode,
        kill_switch_active: bool = False,
        fee_rate: Decimal = Decimal("0"),
    ) -> None:
        self._mode = mode
        self._kill_switch_active = kill_switch_active
        self._kill_switch_reason = (
            "kill_switch_on_startup" if kill_switch_active else None
        )
        self._fee_rate = fee_rate
        self._lock = asyncio.Lock()

        self._orderbooks: dict[MarketTokenKey, OrderBookUpdate] = {}
        self._snapshots: dict[MarketTokenKey, MarketSnapshot] = {}
        self._signals: dict[str, TradeSignal] = {}
        self._open_orders: dict[str, OrderResult] = {}
        self._positions: dict[MarketTokenKey, Position] = {}
        self._balances: dict[str, Balance] = {}
        self._heartbeats: dict[str, datetime] = {}
        self._fill_checkpoints: dict[str, FillCheckpoint] = {}
        self._lifecycles: dict[MarketTokenKey, PositionLifecycle] = {}
        self._closed_lifecycles: list[PositionLifecycle] = []
        self._operational_state = OperationalState.RUNNING
        self._operational_reason: str | None = None
        self._realized_pnl_by_day: dict[str, Decimal] = {}

    @property
    def mode(self) -> Mode:
        """Current bot mode."""

        return self._mode

    async def update_orderbook(self, update: OrderBookUpdate) -> None:
        """Insert latest orderbook update for a market token."""

        async with self._lock:
            self._orderbooks[(update.market_id, update.token_id)] = update

    async def update_market_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Insert latest snapshot for a market token."""

        async with self._lock:
            self._snapshots[(snapshot.market_id, snapshot.token_id)] = snapshot

    async def add_signal(self, signal: TradeSignal) -> None:
        """Track active/recent signal."""

        async with self._lock:
            self._signals[signal.signal_id] = signal

    async def copy_signal_index(self) -> list[tuple[str, datetime]]:
        """Copy (signal_id, created_at) candidates under the lock."""

        async with self._lock:
            return [
                (signal_id, item.created_at)
                for signal_id, item in self._signals.items()
            ]

    async def remove_signals(self, signal_ids: list[str]) -> int:
        """Remove only the named signals; returns how many were removed."""

        async with self._lock:
            removed = 0
            for signal_id in signal_ids:
                if self._signals.pop(signal_id, None) is not None:
                    removed += 1
            return removed

    async def set_order_status(self, result: OrderResult) -> None:
        """Upsert open order map based on latest order status."""

        async with self._lock:
            if result.status in {
                OrderStatus.CANCELLED,
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.REJECTED,
                OrderStatus.FAILED,
                OrderStatus.SIMULATED,
            }:
                self._open_orders.pop(result.client_order_id, None)
            else:
                self._open_orders[result.client_order_id] = result

    async def set_kill_switch(
        self,
        enabled: bool,
        *,
        reason: str | None = None,
    ) -> None:
        """Enable or disable kill-switch."""

        async with self._lock:
            self._kill_switch_active = enabled
            if enabled:
                if reason is not None:
                    self._kill_switch_reason = reason
            else:
                self._kill_switch_reason = None

    async def activate_kill_switch(self, reason: str) -> bool:
        """Atomically latch the kill switch and retain its first reason."""

        async with self._lock:
            if self._kill_switch_active:
                return False
            self._kill_switch_active = True
            self._kill_switch_reason = reason
            return True

    async def is_kill_switch_active(self) -> bool:
        """Return kill-switch state."""

        async with self._lock:
            return self._kill_switch_active

    async def get_kill_switch_reason(self) -> str | None:
        """Return the reason that first latched the active kill switch."""

        async with self._lock:
            return self._kill_switch_reason

    async def get_market_snapshot(self, market_id: str, token_id: str) -> MarketSnapshot | None:
        """Fetch current snapshot for market token."""

        async with self._lock:
            return self._snapshots.get((market_id, token_id))

    async def get_orderbook(self, market_id: str, token_id: str) -> OrderBookUpdate | None:
        """Fetch current orderbook update for market token."""

        async with self._lock:
            return self._orderbooks.get((market_id, token_id))

    async def copy_orderbook_keys(self) -> list[MarketTokenKey]:
        """Copy orderbook key candidates under the lock."""

        async with self._lock:
            return list(self._orderbooks)

    async def copy_snapshot_keys(self) -> list[MarketTokenKey]:
        """Copy market-snapshot key candidates under the lock."""

        async with self._lock:
            return list(self._snapshots)

    async def remove_orderbooks(self, keys: list[MarketTokenKey]) -> int:
        """Remove only the named orderbooks; returns how many were removed."""

        async with self._lock:
            removed = 0
            for key in keys:
                if self._orderbooks.pop(key, None) is not None:
                    removed += 1
            return removed

    async def remove_market_snapshots(self, keys: list[MarketTokenKey]) -> int:
        """Remove only the named snapshots; returns how many were removed."""

        async with self._lock:
            removed = 0
            for key in keys:
                if self._snapshots.pop(key, None) is not None:
                    removed += 1
            return removed

    async def get_open_orders(self) -> list[OrderResult]:
        """Return all open orders."""

        async with self._lock:
            return list(self._open_orders.values())

    async def get_signals(self) -> list[TradeSignal]:
        """Return tracked signals."""

        async with self._lock:
            return list(self._signals.values())

    async def set_position(self, position: Position) -> None:
        """Upsert position by market token."""

        async with self._lock:
            self._positions[(position.market_id, position.token_id)] = position

    async def get_positions(self) -> list[Position]:
        """List known positions."""

        async with self._lock:
            return list(self._positions.values())

    async def replace_positions(self, positions: list[Position]) -> None:
        """Replace the complete position snapshot with exchange truth."""

        async with self._lock:
            self._positions = {
                (position.market_id, position.token_id): position
                for position in positions
            }

    async def merge_authoritative_positions(
        self,
        remote: list[Position],
        *,
        now: datetime,
        market_end_lookup: Callable[[str, str], datetime | None] | None = None,
        dust_threshold: Decimal = Decimal("0"),
    ) -> PositionMergeResult:
        """
        Merge remote truth while preserving pending confirmed local fills.

        ``dust_threshold`` is the smallest quantity the venue will accept in an
        order. When neither side holds that much, the difference between them
        cannot be traded away by anyone, so it is retired as dust instead of
        being deferred and then reported as a divergence on every later pass.
        """

        async with self._lock:
            remote_map = {
                (position.market_id, position.token_id): position
                for position in remote
            }
            keys = set(self._positions) | set(remote_map) | set(self._lifecycles)
            deferred: list[str] = []
            expired: list[str] = []
            dust: list[str] = []
            for key in sorted(keys):
                local = self._positions.get(key)
                remote_position = remote_map.get(key)
                local_quantity = local.quantity if local is not None else Decimal("0")
                remote_quantity = (
                    remote_position.quantity
                    if remote_position is not None
                    else Decimal("0")
                )
                if local_quantity == remote_quantity:
                    lifecycle = self._lifecycles.get(key)
                    if remote_position is not None:
                        self._positions[key] = remote_position
                        self._lifecycles[key] = self._adopt_lifecycle_locked(
                            remote_position,
                            lifecycle=lifecycle,
                            now=now,
                            market_end_lookup=market_end_lookup,
                        )
                    else:
                        self._positions.pop(key, None)
                        if lifecycle is not None and lifecycle.confirmation_deadline is not None:
                            self._lifecycles[key] = lifecycle.model_copy(
                                update={"confirmation_deadline": None}
                            )
                    continue
                lifecycle = self._lifecycles.get(key)
                if (
                    dust_threshold > 0
                    and local_quantity < dust_threshold
                    and remote_quantity < dust_threshold
                    # Never ahead of the confirmation grace period. That window
                    # exists to stop a stale remote read from discarding a fill
                    # the exchange already confirmed to us, and a sub-threshold
                    # position is still real inventory. While it is open this
                    # falls through and defers like any other divergence.
                    and (
                        lifecycle is None
                        or lifecycle.confirmation_deadline is None
                        or now >= lifecycle.confirmation_deadline
                    )
                ):
                    # Neither side can place an order in this market, so the
                    # gap is permanent. Take remote as truth, stop the
                    # confirmation clock, and record it as dust.
                    dust.append(f"{key[0]}:{key[1]}")
                    if remote_position is not None and remote_quantity > 0:
                        self._positions[key] = remote_position
                    else:
                        self._positions.pop(key, None)
                    if lifecycle is not None and lifecycle.confirmation_deadline is not None:
                        self._lifecycles[key] = lifecycle.model_copy(
                            update={"confirmation_deadline": None}
                        )
                    continue
                if (
                    local is None
                    and remote_position is not None
                    and lifecycle is not None
                    and lifecycle.closed_at is None
                ):
                    self._positions[key] = remote_position
                    self._lifecycles[key] = self._adopt_lifecycle_locked(
                        remote_position,
                        lifecycle=lifecycle,
                        now=now,
                        market_end_lookup=market_end_lookup,
                    )
                    continue
                if lifecycle is not None and lifecycle.confirmation_deadline is not None:
                    if now < lifecycle.confirmation_deadline:
                        deferred.append(f"{key[0]}:{key[1]}")
                        continue
                    expired.append(f"{key[0]}:{key[1]}")
                    self._lifecycles[key] = lifecycle.model_copy(
                        update={"confirmation_deadline": None}
                    )
                if remote_position is not None:
                    self._positions[key] = remote_position
                    self._lifecycles[key] = self._adopt_lifecycle_locked(
                        remote_position,
                        lifecycle=self._lifecycles.get(key),
                        now=now,
                        market_end_lookup=market_end_lookup,
                    )
                else:
                    self._positions.pop(key, None)
            unknown_market = [
                f"{position.market_id}:{position.token_id}"
                for position in remote
                if position.quantity > 0
                and (
                    self._lifecycles.get((position.market_id, position.token_id))
                    is None
                    or self._lifecycles[
                        (position.market_id, position.token_id)
                    ].market_end_at
                    is None
                )
            ] if market_end_lookup is not None else []
            return PositionMergeResult(
                deferred_keys=deferred,
                expired_keys=expired,
                unknown_market_keys=unknown_market,
                dust_keys=dust,
            )

    def _adopt_lifecycle_locked(
        self,
        position: Position,
        *,
        lifecycle: PositionLifecycle | None,
        now: datetime,
        market_end_lookup: Callable[[str, str], datetime | None] | None,
    ) -> PositionLifecycle:
        market_end_at = (
            market_end_lookup(position.market_id, position.token_id)
            if market_end_lookup is not None
            else None
        )
        if lifecycle is None or lifecycle.closed_at is not None:
            return PositionLifecycle(
                market_id=position.market_id,
                token_id=position.token_id,
                opened_at=now,
                last_fill_at=now,
                market_end_at=market_end_at,
            )
        return lifecycle.model_copy(
            update={
                "confirmation_deadline": None,
                "market_end_at": market_end_at or lifecycle.market_end_at,
            }
        )

    async def get_position(self, market_id: str, token_id: str) -> Position | None:
        """Get position by market token."""

        async with self._lock:
            return self._positions.get((market_id, token_id))

    async def apply_confirmed_fill(
        self,
        result: OrderResult,
        *,
        market_end_at: datetime | None,
        confirmed_at: datetime,
        confirmation_grace_seconds: float,
    ) -> FillApplication:
        """Apply one confirmed cumulative fill delta atomically."""

        async with self._lock:
            return self._apply_confirmed_fill_locked(
                result,
                market_end_at=market_end_at,
                confirmed_at=confirmed_at,
                confirmation_grace_seconds=confirmation_grace_seconds,
            )

    def _apply_confirmed_fill_locked(
        self,
        result: OrderResult,
        *,
        market_end_at: datetime | None,
        confirmed_at: datetime,
        confirmation_grace_seconds: float,
    ) -> FillApplication:
        if result.status == OrderStatus.SIMULATED:
            if self._mode != Mode.DRY_RUN:
                raise PositionAccountingError("simulated_fill_in_live_mode")
        elif result.status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
            raise PositionAccountingError(f"unconfirmed_status:{result.status.value}")

        if result.market_id is None or result.token_id is None or result.side is None:
            raise PositionAccountingError("missing_identity")
        if not result.market_id or not result.token_id:
            raise PositionAccountingError("missing_identity")
        if result.filled_size <= 0:
            raise PositionAccountingError("missing_filled_size")
        if result.avg_fill_price is None:
            raise PositionAccountingError("missing_avg_fill_price")

        order_key = result.exchange_order_id or result.client_order_id
        cumulative_size = result.filled_size
        cumulative_notional = cumulative_size * result.avg_fill_price

        checkpoint = self._fill_checkpoints.get(order_key)
        if checkpoint is not None:
            if cumulative_size < checkpoint.accounted_filled_size:
                raise PositionAccountingError("cumulative_size_regression")
            if cumulative_notional < checkpoint.accounted_fill_notional:
                raise PositionAccountingError("cumulative_notional_regression")
            delta_size = cumulative_size - checkpoint.accounted_filled_size
            delta_notional = cumulative_notional - checkpoint.accounted_fill_notional
        else:
            delta_size = cumulative_size
            delta_notional = cumulative_notional

        if delta_size == 0:
            return FillApplication(
                order_key=order_key,
                delta_size=Decimal("0"),
                delta_notional=Decimal("0"),
                duplicate=True,
                position=self._positions.get((result.market_id, result.token_id)),
            )

        key = (result.market_id, result.token_id)
        existing = self._positions.get(key)
        quantity = existing.quantity if existing is not None else Decimal("0")
        entry_price = (
            existing.average_entry_price if existing is not None else Decimal("0")
        )
        realized_pnl = existing.realized_pnl if existing is not None else Decimal("0")
        delta_price = delta_notional / delta_size

        # The exchange charges a fee on every fill, not just reducing ones. An
        # opening (BUY) fill produces no gross realised P&L of its own, but the
        # fee it incurs is a real, immediate cost, so it is charged straight
        # into realized_pnl on both sides rather than silently dropped.
        fee = (
            maker_fee(delta_size, result.avg_fill_price, self._fee_rate)
            if result.liquidity == "maker"
            else taker_fee(delta_size, result.avg_fill_price, self._fee_rate)
        )

        if result.side == OrderSide.BUY:
            new_quantity = quantity + delta_size
            new_entry_price = (
                (quantity * entry_price + delta_notional) / new_quantity
                if new_quantity != 0
                else Decimal("0")
            )
            realized_delta = -fee
            new_realized = realized_pnl + realized_delta
        else:
            if delta_size > quantity:
                raise PositionAccountingError("sell_exceeds_inventory")
            new_quantity = quantity - delta_size
            new_entry_price = entry_price if new_quantity != 0 else Decimal("0")
            realized_delta = (delta_price - entry_price) * delta_size - fee
            new_realized = realized_pnl + realized_delta

        position = Position(
            market_id=result.market_id,
            token_id=result.token_id,
            quantity=new_quantity,
            average_entry_price=new_entry_price,
            mark_price=result.avg_fill_price,
            realized_pnl=new_realized,
            unrealized_pnl=Decimal("0"),
            updated_at=confirmed_at,
        )

        lifecycle = self._lifecycles.get(key)
        if (
            lifecycle is None
            or (result.side == OrderSide.BUY and lifecycle.closed_at is not None)
        ):
            lifecycle = PositionLifecycle(
                market_id=result.market_id,
                token_id=result.token_id,
                opened_at=confirmed_at,
                last_fill_at=confirmed_at,
                market_end_at=market_end_at,
            )
        else:
            lifecycle = lifecycle.model_copy(
                update={
                    "last_fill_at": confirmed_at,
                    "market_end_at": market_end_at or lifecycle.market_end_at,
                }
            )

        if result.side == OrderSide.SELL and delta_size > 0:
            lifecycle = lifecycle.model_copy(update={"exit_attempt_count": 0})

        if new_quantity == 0:
            lifecycle = lifecycle.model_copy(
                update={
                    "closed_at": confirmed_at,
                    "closed_exit_price": delta_price,
                    "closed_realized_pnl": new_realized,
                    "confirmation_deadline": confirmed_at
                    + timedelta(seconds=confirmation_grace_seconds),
                    "pending_exit_client_order_id": None,
                    "pending_exit_is_maker": False,
                }
            )
            self._positions.pop(key, None)
            self._lifecycles[key] = lifecycle
            self._closed_lifecycles.append(lifecycle)
            self._closed_lifecycles = self._closed_lifecycles[-20:]
        else:
            lifecycle = lifecycle.model_copy(
                update={
                    "confirmation_deadline": confirmed_at
                    + timedelta(seconds=confirmation_grace_seconds),
                }
            )
            self._positions[key] = position
            self._lifecycles[key] = lifecycle

        self._fill_checkpoints[order_key] = FillCheckpoint(
            order_key=order_key,
            market_id=result.market_id,
            token_id=result.token_id,
            side=result.side,
            accounted_filled_size=cumulative_size,
            accounted_fill_notional=cumulative_notional,
            confirmed_at=confirmed_at,
        )
        if realized_delta != 0:
            day_key = confirmed_at.astimezone(UTC).date().isoformat()
            self._realized_pnl_by_day[day_key] = (
                self._realized_pnl_by_day.get(day_key, Decimal("0"))
                + realized_delta
            )

        return FillApplication(
            order_key=order_key,
            delta_size=delta_size,
            delta_notional=delta_notional,
            duplicate=False,
            position=position,
        )

    async def get_fill_checkpoints(self) -> list[FillCheckpoint]:
        """Return a copy of every fill checkpoint."""

        async with self._lock:
            return list(self._fill_checkpoints.values())

    async def remove_fill_checkpoints(self, order_keys: list[str]) -> int:
        """Remove only the named checkpoints; returns how many were removed."""

        async with self._lock:
            removed = 0
            for order_key in order_keys:
                if self._fill_checkpoints.pop(order_key, None) is not None:
                    removed += 1
            return removed

    async def restore_fill_checkpoint(self, checkpoint: FillCheckpoint) -> None:
        """Restore one fill checkpoint from a snapshot."""

        async with self._lock:
            self._fill_checkpoints[checkpoint.order_key] = checkpoint

    async def get_position_lifecycles(self) -> list[PositionLifecycle]:
        """Return a copy of every active position lifecycle."""

        async with self._lock:
            return list(self._lifecycles.values())

    async def get_closed_position_lifecycles(self) -> list[PositionLifecycle]:
        """Return the most recent closed lifecycle records."""

        async with self._lock:
            return list(self._closed_lifecycles)

    async def remove_closed_position_lifecycles(
        self,
        lifecycles: list[PositionLifecycle],
    ) -> int:
        """Remove only exact closed-lifecycle records; returns count removed."""

        async with self._lock:
            removed = 0
            for lifecycle in lifecycles:
                try:
                    self._closed_lifecycles.remove(lifecycle)
                except ValueError:
                    continue
                removed += 1
            return removed

    async def get_position_lifecycle(
        self, market_id: str, token_id: str
    ) -> PositionLifecycle | None:
        """Get the lifecycle for one market token."""

        async with self._lock:
            return self._lifecycles.get((market_id, token_id))

    async def restore_position_lifecycle(self, lifecycle: PositionLifecycle) -> None:
        """Restore one position lifecycle from a snapshot."""

        async with self._lock:
            self._lifecycles[(lifecycle.market_id, lifecycle.token_id)] = lifecycle

    async def restore_closed_position_lifecycle(
        self,
        lifecycle: PositionLifecycle,
    ) -> None:
        """Restore one immutable closed lifecycle record from a snapshot."""

        async with self._lock:
            self._closed_lifecycles.append(lifecycle)
            self._closed_lifecycles = self._closed_lifecycles[-20:]

    async def get_realized_pnl_by_day(self) -> dict[str, Decimal]:
        """Return confirmed realized P&L grouped by UTC trading day."""

        async with self._lock:
            return dict(self._realized_pnl_by_day)

    async def remove_realized_pnl_days(self, days: list[str]) -> int:
        """Remove only the named UTC days; returns how many were removed."""

        async with self._lock:
            removed = 0
            for day in days:
                if self._realized_pnl_by_day.pop(day, None) is not None:
                    removed += 1
            return removed

    async def get_daily_realized_pnl(
        self,
        at: datetime | None = None,
    ) -> Decimal:
        """Return confirmed realized P&L for one UTC day."""

        current = (at or utc_now()).astimezone(UTC).date().isoformat()
        async with self._lock:
            return self._realized_pnl_by_day.get(current, Decimal("0"))

    async def restore_realized_pnl_by_day(
        self,
        values: dict[str, Decimal],
    ) -> None:
        """Restore the durable UTC daily realized-P&L ledger."""

        async with self._lock:
            self._realized_pnl_by_day = dict(values)

    async def reserve_exit(
        self,
        market_id: str,
        token_id: str,
        *,
        client_order_id: str,
        reason: ExitReason,
        attempted_at: datetime,
        mark_maker_attempt: bool = False,
    ) -> bool:
        """
        Reserve one exit attempt; returns False when already reserved.

        When ``mark_maker_attempt`` is True and the lifecycle has not yet
        recorded a maker exit attempt, ``exit_first_attempted_at`` is stamped
        with ``attempted_at``. It is left untouched on every later call so a
        taker escalation attempt never resets the maker-exit deadline clock,
        and it stays None for a position whose exits have always been taker.
        """

        async with self._lock:
            lifecycle = self._lifecycles.get((market_id, token_id))
            if lifecycle is None:
                return False
            if lifecycle.pending_exit_client_order_id is not None:
                return False
            update: dict[str, object] = {
                "pending_exit_client_order_id": client_order_id,
                "last_exit_reason": reason,
                "last_exit_attempt_at": attempted_at,
                "exit_attempt_count": lifecycle.exit_attempt_count + 1,
                "pending_exit_is_maker": mark_maker_attempt,
            }
            if mark_maker_attempt and lifecycle.exit_first_attempted_at is None:
                update["exit_first_attempted_at"] = attempted_at
            self._lifecycles[(market_id, token_id)] = lifecycle.model_copy(update=update)
            return True

    async def release_exit(
        self,
        market_id: str,
        token_id: str,
        *,
        client_order_id: str,
    ) -> bool:
        """Release an exit reservation; returns False when not reserved."""

        async with self._lock:
            lifecycle = self._lifecycles.get((market_id, token_id))
            if lifecycle is None:
                return False
            if lifecycle.pending_exit_client_order_id != client_order_id:
                return False
            self._lifecycles[(market_id, token_id)] = lifecycle.model_copy(
                update={
                    "pending_exit_client_order_id": None,
                    "pending_exit_is_maker": False,
                }
            )
            return True

    async def set_balance(self, balance: Balance) -> None:
        """Upsert balance by currency."""

        async with self._lock:
            self._balances[balance.currency] = balance

    async def get_balances(self) -> list[Balance]:
        """List known balances."""

        async with self._lock:
            return list(self._balances.values())

    async def update_heartbeat(self, component: str, timestamp: datetime | None = None) -> None:
        """Update heartbeat timestamp for a component."""

        async with self._lock:
            self._heartbeats[component] = timestamp or utc_now()

    async def get_heartbeat(self, component: str) -> datetime | None:
        """Get heartbeat timestamp for one component."""

        async with self._lock:
            return self._heartbeats.get(component)

    async def get_heartbeats(self) -> dict[str, datetime]:
        """Return a copy of every component heartbeat."""

        async with self._lock:
            return dict(self._heartbeats)

    async def set_operational_state(
        self, state: OperationalState, *, reason: str | None = None
    ) -> None:
        """Record the current operating state and why."""

        async with self._lock:
            self._operational_state = state
            self._operational_reason = reason

    async def get_operational_state(self) -> tuple[OperationalState, str | None]:
        """Return the current operating state and its reason."""

        async with self._lock:
            return self._operational_state, self._operational_reason

    async def entries_permitted(self) -> bool:
        """Entries are permitted only in RUNNING state."""

        async with self._lock:
            return self._operational_state == OperationalState.RUNNING

    async def is_heartbeat_stale(self, component: str, *, max_age_seconds: float) -> bool:
        """Whether a heartbeat is older than threshold."""

        async with self._lock:
            ts = self._heartbeats.get(component)
        if ts is None:
            return True
        return utc_now() - ts > timedelta(seconds=max_age_seconds)

    async def total_marked_exposure(self) -> Decimal:
        """Compute marked notional exposure across positions."""

        async with self._lock:
            positions = list(self._positions.values())
        return total_marked_exposure(positions)
