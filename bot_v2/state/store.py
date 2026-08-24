"""Async-safe in-memory state store."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import Mode
from models.market import MarketSnapshot, OrderBookUpdate
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import (
    Balance,
    ExitReason,
    FillApplication,
    FillCheckpoint,
    Position,
    PositionLifecycle,
)
from models.signal import TradeSignal
from portfolio.exposure import total_marked_exposure


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


MarketTokenKey = tuple[str, str]


class PositionAccountingError(RuntimeError):
    """Raised when a confirmed fill violates a position accounting invariant."""


class InMemoryStateStore:
    """Holds runtime state for strategy/risk/execution components."""

    def __init__(self, *, mode: Mode, kill_switch_active: bool = False) -> None:
        self._mode = mode
        self._kill_switch_active = kill_switch_active
        self._kill_switch_reason = (
            "kill_switch_on_startup" if kill_switch_active else None
        )
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

    async def set_order_status(self, result: OrderResult) -> None:
        """Upsert open order map based on latest order status."""

        async with self._lock:
            if result.status in {
                OrderStatus.CANCELLED,
                OrderStatus.FILLED,
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

        if result.side == OrderSide.BUY:
            new_quantity = quantity + delta_size
            new_entry_price = (
                (quantity * entry_price + delta_notional) / new_quantity
                if new_quantity != 0
                else Decimal("0")
            )
            new_realized = realized_pnl
        else:
            if delta_size > quantity:
                raise PositionAccountingError("sell_exceeds_inventory")
            new_quantity = quantity - delta_size
            new_entry_price = entry_price if new_quantity != 0 else Decimal("0")
            new_realized = realized_pnl + (delta_price - entry_price) * delta_size

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
        if lifecycle is None:
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

    async def reserve_exit(
        self,
        market_id: str,
        token_id: str,
        *,
        client_order_id: str,
        reason: ExitReason,
        attempted_at: datetime,
    ) -> bool:
        """Reserve one exit attempt; returns False when already reserved."""

        async with self._lock:
            lifecycle = self._lifecycles.get((market_id, token_id))
            if lifecycle is None:
                return False
            if lifecycle.pending_exit_client_order_id is not None:
                return False
            self._lifecycles[(market_id, token_id)] = lifecycle.model_copy(
                update={
                    "pending_exit_client_order_id": client_order_id,
                    "last_exit_reason": reason,
                    "last_exit_attempt_at": attempted_at,
                    "exit_attempt_count": lifecycle.exit_attempt_count + 1,
                }
            )
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
                update={"pending_exit_client_order_id": None}
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
