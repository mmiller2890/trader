"""Minimal replay boundary for v1."""

from __future__ import annotations

from dataclasses import dataclass, field

from backtest.metrics import ReplayMetrics, summarize
from models.market import MarketSnapshot
from models.order import OrderResult
from models.signal import TradeSignal
from strategies.base import StrategyBase


@dataclass(slots=True)
class ReplayResult:
    """Collected replay outputs."""

    signals: list[TradeSignal] = field(default_factory=list)
    order_results: list[OrderResult] = field(default_factory=list)
    metrics: ReplayMetrics | None = None


class ReplayEngine:
    """Feed historical snapshots through a strategy boundary."""

    async def run(self, *, strategy: StrategyBase, snapshots: list[MarketSnapshot]) -> ReplayResult:
        """Replay snapshots through a strategy and collect emitted signals."""

        result = ReplayResult()
        for snapshot in snapshots:
            result.signals.extend(await strategy.on_market_update(snapshot))
        result.metrics = summarize(result.signals, result.order_results)
        return result
