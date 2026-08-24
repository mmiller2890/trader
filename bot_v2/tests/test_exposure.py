from __future__ import annotations

from decimal import Decimal

from models.position import Position
from portfolio.exposure import marked_notional, total_marked_exposure


def test_marked_notional_uses_current_price() -> None:
    position = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("100"),
        mark_price=Decimal("0.25"),
    )

    assert marked_notional(position) == Decimal("25.00")


def test_marked_notional_uses_max_payout_when_mark_is_missing() -> None:
    position = Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal("3"),
        mark_price=None,
    )

    assert marked_notional(position) == Decimal("3")


def test_total_marked_exposure_sums_long_and_short_notional() -> None:
    positions = [
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("10"),
            mark_price=Decimal("0.25"),
        ),
        Position(
            market_id="m2",
            token_id="t2",
            quantity=Decimal("-4"),
            mark_price=Decimal("0.50"),
        ),
    ]

    assert total_marked_exposure(positions) == Decimal("4.50")
