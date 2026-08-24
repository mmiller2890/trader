"""Exposure utilities."""

from __future__ import annotations

from decimal import Decimal

from models.position import Position


def marked_notional(position: Position) -> Decimal:
    """Return current notional, conservatively using max payout without a mark."""

    price = position.mark_price if position.mark_price is not None else Decimal("1")
    return abs(position.quantity) * price


def total_marked_exposure(positions: list[Position]) -> Decimal:
    """Sum current marked notional across positions."""

    return sum((marked_notional(position) for position in positions), start=Decimal("0"))


def total_absolute_exposure(positions: list[Position]) -> Decimal:
    """Sum absolute position sizes."""

    return sum((abs(position.quantity) for position in positions), start=Decimal("0"))


def exposure_by_market(positions: list[Position]) -> dict[str, Decimal]:
    """Aggregate absolute exposure by market id."""

    out: dict[str, Decimal] = {}
    for position in positions:
        out[position.market_id] = out.get(position.market_id, Decimal("0")) + abs(position.quantity)
    return out
