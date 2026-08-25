"""Polymarket per-category fee model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from models.fees import (
    CATEGORY_FEE_RATES,
    DEFAULT_FEE_RATE,
    maker_fee,
    taker_fee,
    taker_fee_bps,
)

CRYPTO = Decimal("0.07")


def test_taker_fee_matches_the_published_formula() -> None:
    # fee = shares * rate * p * (1 - p); 100 shares at 0.50 on crypto.
    assert taker_fee(Decimal("100"), Decimal("0.50"), CRYPTO) == Decimal("1.7500")


def test_taker_fee_peaks_at_the_midpoint() -> None:
    mid = taker_fee(Decimal("100"), Decimal("0.50"), CRYPTO)
    for price in ("0.10", "0.30", "0.70", "0.90"):
        assert taker_fee(Decimal("100"), Decimal(price), CRYPTO) < mid


def test_fee_bps_identity_equals_rate_times_one_minus_price() -> None:
    # fee/notional = (shares*rate*p*(1-p)) / (shares*p) = rate*(1-p)
    for price in ("0.10", "0.30", "0.50", "0.70", "0.90"):
        p = Decimal(price)
        shares = Decimal("100")
        dollars = taker_fee(shares, p, CRYPTO)
        expected = dollars / (shares * p) * Decimal("10000")
        assert abs(taker_fee_bps(p, CRYPTO) - expected) < Decimal("0.0001")


def test_fee_bps_at_the_prices_that_motivated_this_work() -> None:
    assert round(taker_fee_bps(Decimal("0.50"), CRYPTO)) == 350
    assert round(taker_fee_bps(Decimal("0.30"), CRYPTO)) == 490
    assert round(taker_fee_bps(Decimal("0.70"), CRYPTO)) == 210


def test_makers_pay_nothing() -> None:
    assert maker_fee(Decimal("100"), Decimal("0.50"), CRYPTO) == Decimal("0")


def test_zero_price_does_not_divide_by_zero() -> None:
    assert taker_fee_bps(Decimal("0"), CRYPTO) == Decimal("0")


def test_crypto_rate_is_the_documented_one() -> None:
    assert CATEGORY_FEE_RATES["crypto"] == CRYPTO
    assert DEFAULT_FEE_RATE == CRYPTO
