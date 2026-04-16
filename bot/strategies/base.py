"""Strategy interface for signal generation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.market import MarketSnapshot
from models.order import OrderResult
from models.signal import TradeSignal


class StrategyBase(ABC):
    """Base class for strategy implementations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable strategy name."""

    @abstractmethod
    async def on_market_update(self, snapshot: MarketSnapshot) -> list[TradeSignal]:
        """Process market update and emit zero or more signals."""

    @abstractmethod
    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        """Process order lifecycle update and optionally emit signals."""

    @abstractmethod
    async def on_timer(self) -> list[TradeSignal]:
        """Periodic strategy hook for maintenance-driven signals."""
