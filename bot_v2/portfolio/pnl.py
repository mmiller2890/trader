"""PnL helpers."""

from __future__ import annotations

from decimal import Decimal

from models.position import Position


def total_realized_pnl(positions: list[Position]) -> Decimal:
    """Sum realized PnL across positions."""

    return sum((position.realized_pnl for position in positions), start=Decimal("0"))


def total_unrealized_pnl(positions: list[Position]) -> Decimal:
    """Sum unrealized PnL across positions."""

    return sum((position.unrealized_pnl for position in positions), start=Decimal("0"))


def total_pnl(positions: list[Position]) -> Decimal:
    """Sum total PnL across positions."""

    return total_realized_pnl(positions) + total_unrealized_pnl(positions)
