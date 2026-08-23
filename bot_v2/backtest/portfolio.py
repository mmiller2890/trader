"""Cash, collateral, position, and fee ledger for the paper exchange."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from backtest.models import ExecutionReport, PortfolioSnapshot
from config.schema import BacktestConfig
from models.market import MarketSnapshot
from models.order import OrderSide
from models.position import Position


def _project_position(
    existing: Position | None,
    report: ExecutionReport,
    timestamp: datetime,
) -> Position:
    """Project the position after ``report`` without mutating state."""
    if existing is None:
        quantity = Decimal("0")
        entry_price = Decimal("0")
        realized_pnl = Decimal("0")
    else:
        quantity = existing.quantity
        entry_price = existing.average_entry_price
        realized_pnl = existing.realized_pnl

    fill_price = report.average_fill_price
    if fill_price is None:
        fill_price = Decimal("0")
    delta = report.filled_size if report.order.side == OrderSide.BUY else -report.filled_size

    if quantity == 0 or quantity * delta > 0:
        new_quantity = quantity + delta
        new_entry_price = (
            (abs(quantity) * entry_price + abs(delta) * fill_price) / abs(new_quantity)
            if new_quantity != 0
            else Decimal("0")
        )
    else:
        closed_size = min(abs(quantity), abs(delta))
        realized_pnl += (
            (fill_price - entry_price) * closed_size
            if quantity > 0
            else (entry_price - fill_price) * closed_size
        )
        new_quantity = quantity + delta
        new_entry_price = (
            entry_price
            if new_quantity != 0 and quantity * new_quantity > 0
            else fill_price
        )

    return Position(
        market_id=report.order.market_id,
        token_id=report.order.token_id,
        quantity=new_quantity,
        average_entry_price=new_entry_price if new_quantity != 0 else Decimal("0"),
        mark_price=fill_price if new_quantity != 0 else None,
        realized_pnl=realized_pnl,
        unrealized_pnl=Decimal("0"),
        updated_at=timestamp,
    )


class PortfolioLedger:
    """Source of truth for simulated cash, collateral, positions, and fees."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.starting_cash = config.starting_cash
        self.cash = config.starting_cash
        self.total_fees = Decimal("0")
        self.positions: dict[tuple[str, str], Position] = {}
        self.marks: dict[tuple[str, str], Decimal] = {}

    def can_apply(self, report: ExecutionReport) -> tuple[bool, str]:
        if report.filled_size <= 0:
            return False, "no_fills"
        key = (report.order.market_id, report.order.token_id)
        projected_position = _project_position(
            self.positions.get(key), report, report.order.created_at
        )
        projected_cash = self._projected_cash(report)
        if (
            projected_position.quantity < 0
            and not self.config.allow_short_positions
        ):
            return False, "short_positions_disabled"
        projected_positions = dict(self.positions)
        projected_positions[key] = projected_position
        reserved = self._reserved_cash(projected_positions.values())
        if projected_cash < reserved:
            if projected_position.quantity < 0:
                return False, "insufficient_short_collateral"
            return False, "insufficient_cash"
        return True, "funded"

    def apply(self, report: ExecutionReport, timestamp: datetime) -> Position:
        allowed, reason = self.can_apply(report)
        if not allowed:
            raise ValueError(reason)
        key = (report.order.market_id, report.order.token_id)
        self.cash = self._projected_cash(report)
        self.total_fees += report.total_fees
        position = _project_position(self.positions.get(key), report, timestamp)
        self.positions[key] = position
        self.marks[key] = report.average_fill_price or position.average_entry_price
        return position

    def mark(self, snapshot: MarketSnapshot) -> None:
        key = (snapshot.market_id, snapshot.token_id)
        position = self.positions.get(key)
        if position is None or position.quantity == 0:
            return
        if position.quantity > 0:
            unrealized = (snapshot.mid_price - position.average_entry_price) * position.quantity
        else:
            unrealized = (
                position.average_entry_price - snapshot.mid_price
            ) * abs(position.quantity)
        self.positions[key] = position.model_copy(
            update={
                "mark_price": snapshot.mid_price,
                "unrealized_pnl": unrealized,
                "updated_at": snapshot.received_ts,
            }
        )
        self.marks[key] = snapshot.mid_price

    def snapshot(self, timestamp: datetime) -> PortfolioSnapshot:
        reserved = self._reserved_cash(self.positions.values())
        position_value = sum(
            (
                position.quantity * self.marks.get(key, position.average_entry_price)
                for key, position in self.positions.items()
            ),
            start=Decimal("0"),
        )
        realized = sum(
            (position.realized_pnl for position in self.positions.values()),
            start=Decimal("0"),
        )
        unrealized = sum(
            (position.unrealized_pnl for position in self.positions.values()),
            start=Decimal("0"),
        )
        equity = self.cash + position_value
        net_pnl = equity - self.starting_cash
        return PortfolioSnapshot(
            timestamp=timestamp,
            cash=self.cash,
            reserved_cash=reserved,
            available_cash=self.cash - reserved,
            position_value=position_value,
            equity=equity,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            gross_pnl=realized + unrealized,
            fees_paid=self.total_fees,
            net_pnl=net_pnl,
            positions=sorted(
                self.positions.values(),
                key=lambda position: (position.market_id, position.token_id),
            ),
        )

    def _projected_cash(self, report: ExecutionReport) -> Decimal:
        if report.order.side == OrderSide.BUY:
            return self.cash - report.total_notional - report.total_fees
        return self.cash + report.total_notional - report.total_fees

    def _reserved_cash(self, positions: Iterable[Position]) -> Decimal:
        return sum(
            (
                abs(min(position.quantity, Decimal("0")))
                * self.config.max_payout_per_share
                for position in positions
            ),
            start=Decimal("0"),
        )
