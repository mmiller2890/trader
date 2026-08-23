from __future__ import annotations

from decimal import Decimal

from backtest.conftest import (
    LATER,
    NOW,
    fill_report,
    filled_buy,
    filled_sell,
    market_snapshot as snapshot,
    partial_buy,
)
from backtest.portfolio import PortfolioLedger
from config.schema import BacktestConfig
from models.order import OrderSide


def test_buy_reduces_cash_by_notional_and_fee() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="10"))
    report = filled_buy(size="5", price="0.50", fee_bps="10")
    allowed, reason = ledger.can_apply(report)
    assert (allowed, reason) == (True, "funded")
    ledger.apply(report, NOW)
    ledger.mark(snapshot(mid="0.50", at=NOW))
    state = ledger.snapshot(NOW)
    assert state.cash == Decimal("97.4975")
    assert state.position_value == Decimal("2.50")
    assert state.equity == Decimal("99.9975")
    assert state.fees_paid == Decimal("0.0025")
    assert state.net_pnl == Decimal("-0.0025")


def test_partial_fill_only_books_executed_size() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="0"))
    report = partial_buy(requested="10", filled="3", price="0.50")
    ledger.apply(report, NOW)
    assert ledger.positions[("m1", "t1")].quantity == Decimal("3")
    assert ledger.cash == Decimal("98.50")


def test_synthetic_short_reserves_full_payout_liability() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="10", taker_fee_bps="0", allow_short_positions=True))
    report = filled_sell(size="5", price="0.60", fee_bps="0")
    assert ledger.can_apply(report) == (True, "funded")
    ledger.apply(report, NOW)
    state = ledger.snapshot(NOW)
    assert state.cash == Decimal("13")
    assert state.reserved_cash == Decimal("5")
    assert state.available_cash == Decimal("8")


def test_insufficient_short_collateral_is_rejected_without_mutation() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="1", taker_fee_bps="0", allow_short_positions=True))
    report = filled_sell(size="5", price="0.10", fee_bps="0")
    assert ledger.can_apply(report) == (False, "insufficient_short_collateral")
    assert ledger.cash == Decimal("1")
    assert ledger.positions == {}


def test_short_is_rejected_when_disabled() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", allow_short_positions=False))
    assert ledger.can_apply(filled_sell(size="1", price="0.50")) == (False, "short_positions_disabled")


def test_closing_short_releases_reserved_collateral() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="10", taker_fee_bps="0", allow_short_positions=True))
    ledger.apply(filled_sell(size="9", price="0.60"), NOW)
    assert ledger.snapshot(NOW).reserved_cash == Decimal("9")
    flip = fill_report(
        side=OrderSide.BUY, requested="9", filled="9", price="1.00"
    )
    assert ledger.can_apply(flip) == (True, "funded")
    ledger.apply(flip, LATER)
    state = ledger.snapshot(LATER)
    assert state.reserved_cash == Decimal("0")
    assert state.positions[0].quantity == Decimal("0")
    assert state.realized_pnl == Decimal("-3.60")


def test_long_round_trip_realizes_gross_pnl_and_net_pnl_includes_fees() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="10"))
    ledger.apply(filled_buy(size="5", price="0.50", fee_bps="10"), NOW)
    ledger.apply(filled_sell(size="5", price="0.60", fee_bps="10"), LATER)
    state = ledger.snapshot(LATER)
    assert state.realized_pnl == Decimal("0.50")
    assert state.fees_paid == Decimal("0.0055")
    assert state.net_pnl == Decimal("0.4945")
    assert state.positions[0].quantity == Decimal("0")
