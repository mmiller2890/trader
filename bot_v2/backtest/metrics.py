"""Backtest metrics computed from portfolio snapshots and execution reports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from backtest.models import ExecutionReport, PortfolioSnapshot
from models.order import OrderResult, OrderStatus
from models.position import Position
from models.signal import TradeSignal


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    """Summary metrics from replay/backtest runs."""

    signal_count: int
    simulated_order_count: int
    rejected_order_count: int
    filled_order_count: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    starting_cash: Decimal
    ending_cash: Decimal
    ending_equity: Decimal
    reserved_cash: Decimal
    fees_paid: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    fill_rate: Decimal
    max_drawdown: Decimal
    max_drawdown_pct: Decimal


def summarize(
    signals: list[TradeSignal],
    results: list[OrderResult],
    positions: list[Position] | None = None,
    execution_reports: list[ExecutionReport] | None = None,
    portfolio_snapshots: list[PortfolioSnapshot] | None = None,
    ledger: object | None = None,
) -> ReplayMetrics:
    """Aggregate capital-aware metrics from reports, snapshots, and the ledger."""

    final_positions = positions or []
    realized_pnl = sum(
        (position.realized_pnl for position in final_positions), start=Decimal("0")
    )
    unrealized_pnl = sum(
        (position.unrealized_pnl for position in final_positions), start=Decimal("0")
    )

    reports = execution_reports or []
    requested_total = sum(
        (report.requested_size for report in reports), start=Decimal("0")
    )
    filled_total = sum(
        (report.filled_size for report in reports), start=Decimal("0")
    )
    fill_rate = (
        filled_total / requested_total if requested_total > 0 else Decimal("0")
    )

    if hasattr(ledger, "starting_cash"):
        starting_cash: Decimal = ledger.starting_cash  # type: ignore[attr-defined]
    elif portfolio_snapshots:
        first = portfolio_snapshots[0]
        starting_cash = first.equity - first.net_pnl
    else:
        starting_cash = Decimal("0")

    last = _last_equity(portfolio_snapshots or [])
    ending_cash = last.cash if last is not None else starting_cash
    ending_equity = last.equity if last is not None else starting_cash
    reserved_cash = last.reserved_cash if last is not None else Decimal("0")
    fees_paid = last.fees_paid if last is not None else Decimal("0")
    gross_pnl = last.gross_pnl if last is not None else Decimal("0")
    net_pnl = last.net_pnl if last is not None else Decimal("0")

    peak = starting_cash
    max_drawdown = Decimal("0")
    max_drawdown_pct = Decimal("0")
    for snapshot in portfolio_snapshots or []:
        if snapshot.equity > peak:
            peak = snapshot.equity
        drawdown = peak - snapshot.equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = (
                drawdown / peak if peak > 0 else Decimal("0")
            )

    return ReplayMetrics(
        signal_count=len(signals),
        simulated_order_count=sum(
            1 for result in results if result.status == OrderStatus.SIMULATED
        ),
        rejected_order_count=sum(
            1 for result in results if result.status == OrderStatus.REJECTED
        ),
        filled_order_count=sum(
            1
            for result in results
            if result.status in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}
        ),
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_pnl=net_pnl,
        starting_cash=starting_cash,
        ending_cash=ending_cash,
        ending_equity=ending_equity,
        reserved_cash=reserved_cash,
        fees_paid=fees_paid,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        fill_rate=fill_rate,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
    )


def _last_equity(snapshots: list[PortfolioSnapshot]) -> PortfolioSnapshot | None:
    return snapshots[-1] if snapshots else None
