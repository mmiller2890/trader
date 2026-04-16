"""Risk policy interfaces."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from models.market import MarketSnapshot
from models.risk import RiskDecision
from models.signal import TradeSignal


class PreTradeRiskPolicy(Protocol):
    """Policy contract for pre-trade risk evaluation."""

    async def evaluate(
        self,
        *,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_size: Decimal,
        proposed_price: Decimal,
    ) -> RiskDecision:
        """Return decision for the proposed order intent."""


class RuntimeRiskPolicy(Protocol):
    """Policy contract for periodic runtime risk checks."""

    async def evaluate_runtime(self) -> RiskDecision:
        """Return runtime risk decision."""
