from __future__ import annotations

from decimal import Decimal

from backtest.conftest import NOW, filled_buy, market_snapshot as snapshot
from backtest.metrics import summarize
from backtest.portfolio import PortfolioLedger
from config.schema import BacktestConfig


def test_summarize_recovers_starting_cash_from_first_equity_snapshot() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="10"))
    report = filled_buy(size="5", price="0.50", fee_bps="10")
    ledger.apply(report, NOW)
    ledger.mark(snapshot(mid="0.50", at=NOW))
    snapshots = [ledger.snapshot(NOW)]
    metrics = summarize([], [], [], [report], snapshots)
    assert metrics.starting_cash == Decimal("100")
    assert metrics.ending_cash == Decimal("97.4975")
    assert metrics.ending_equity == Decimal("99.9975")
    assert metrics.net_pnl == Decimal("-0.0025")
    assert metrics.total_pnl == Decimal("-0.0025")
    assert metrics.fees_paid == Decimal("0.0025")
    assert metrics.fill_rate == Decimal("1")
