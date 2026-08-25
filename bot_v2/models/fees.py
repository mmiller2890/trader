"""
Polymarket trading fees.

Takers pay a per-category fee that peaks at the midpoint and falls to zero at
the price extremes. Makers pay nothing and earn a rebate funded by taker fees,
which is why the maker/taker distinction dominates the economics of any
short-horizon strategy on these markets.
"""

from __future__ import annotations

from decimal import Decimal

#: Per-category taker fee rates, as published by Polymarket in 2026.
CATEGORY_FEE_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "politics": Decimal("0.04"),
    "finance": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "mentions": Decimal("0.04"),
    "geopolitics": Decimal("0"),
}

#: This bot trades crypto up/down markets.
DEFAULT_FEE_RATE = CATEGORY_FEE_RATES["crypto"]


def taker_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Fee in dollars for crossing the spread, per the published formula."""

    return shares * fee_rate * price * (Decimal("1") - price)


def taker_fee_bps(price: Decimal, fee_rate: Decimal) -> Decimal:
    """
    Taker fee as basis points of notional.

    Dividing the dollar fee by notional cancels the share count and one factor
    of price, leaving ``fee_rate * (1 - price)``. Fees are therefore cheapest
    on lopsided markets and most expensive at even odds.
    """

    if price <= 0:
        return Decimal("0")
    return fee_rate * (Decimal("1") - price) * Decimal("10000")


def maker_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Makers pay nothing. Present so callers need not special-case the side."""

    _ = (shares, price, fee_rate)
    return Decimal("0")
