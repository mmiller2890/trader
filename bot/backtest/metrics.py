"""Simple backtest and replay metrics."""

from __future__ import annotations

from dataclasses import dataclass

from models.order import OrderResult, OrderStatus
from models.signal import TradeSignal


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    """Summary metrics from replay/backtest runs."""

    signal_count: int
    simulated_order_count: int
    rejected_order_count: int


def summarize(signals: list[TradeSignal], results: list[OrderResult]) -> ReplayMetrics:
    """Aggregate basic replay metrics."""

    return ReplayMetrics(
        signal_count=len(signals),
        simulated_order_count=sum(1 for result in results if result.status == OrderStatus.SIMULATED),
        rejected_order_count=sum(1 for result in results if result.status == OrderStatus.REJECTED),
    )
