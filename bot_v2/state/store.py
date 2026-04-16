"""Async-safe in-memory state store."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import Mode
from models.market import MarketSnapshot, OrderBookUpdate
from models.order import OrderResult, OrderStatus
from models.position import Balance, Position
from models.signal import TradeSignal


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


MarketTokenKey = tuple[str, str]


class InMemoryStateStore:
    """Holds runtime state for strategy/risk/execution components."""

    def __init__(self, *, mode: Mode, kill_switch_active: bool = False) -> None:
        self._mode = mode
        self._kill_switch_active = kill_switch_active
        self._lock = asyncio.Lock()

        self._orderbooks: dict[MarketTokenKey, OrderBookUpdate] = {}
        self._snapshots: dict[MarketTokenKey, MarketSnapshot] = {}
        self._signals: dict[str, TradeSignal] = {}
        self._open_orders: dict[str, OrderResult] = {}
        self._positions: dict[MarketTokenKey, Position] = {}
        self._balances: dict[str, Balance] = {}
        self._heartbeats: dict[str, datetime] = {}

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

    async def set_kill_switch(self, enabled: bool) -> None:
        """Enable or disable kill-switch."""

        async with self._lock:
            self._kill_switch_active = enabled

    async def is_kill_switch_active(self) -> bool:
        """Return kill-switch state."""

        async with self._lock:
            return self._kill_switch_active

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

    async def get_position(self, market_id: str, token_id: str) -> Position | None:
        """Get position by market token."""

        async with self._lock:
            return self._positions.get((market_id, token_id))

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

    async def is_heartbeat_stale(self, component: str, *, max_age_seconds: float) -> bool:
        """Whether a heartbeat is older than threshold."""

        async with self._lock:
            ts = self._heartbeats.get(component)
        if ts is None:
            return True
        return utc_now() - ts > timedelta(seconds=max_age_seconds)

    async def total_absolute_exposure(self) -> Decimal:
        """Compute absolute quantity exposure across positions."""

        async with self._lock:
            positions = list(self._positions.values())
        return sum((abs(p.quantity) for p in positions), start=Decimal("0"))
