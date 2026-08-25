"""Tick and lot quantization behaviour."""

from __future__ import annotations

from decimal import Decimal

import pytest

from models.tick import (
    DEFAULT_TICK_SIZE,
    TickSizeError,
    is_on_tick,
    normalize_tick_size,
    quantize_price,
    quantize_size,
    ticks_between,
)
from models.order import OrderSide


def test_normalize_accepts_every_exchange_tick() -> None:
    for raw in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"):
        assert normalize_tick_size(raw) == Decimal(raw)


def test_normalize_rejects_unsupported_tick() -> None:
    with pytest.raises(TickSizeError):
        normalize_tick_size("0.02")


def test_buy_price_rounds_down_onto_grid() -> None:
    price = quantize_price(
        Decimal("0.6349"), tick_size=DEFAULT_TICK_SIZE, side=OrderSide.BUY
    )
    assert price == Decimal("0.63")
    assert is_on_tick(price, tick_size=DEFAULT_TICK_SIZE)


def test_sell_price_rounds_up_onto_grid() -> None:
    price = quantize_price(
        Decimal("0.6301"), tick_size=DEFAULT_TICK_SIZE, side=OrderSide.SELL
    )
    assert price == Decimal("0.64")
    assert is_on_tick(price, tick_size=DEFAULT_TICK_SIZE)


def test_quantization_never_crosses_the_market() -> None:
    # A buy may only become cheaper; a sell may only become dearer.
    raw = Decimal("0.5555")
    assert quantize_price(raw, tick_size=Decimal("0.01"), side=OrderSide.BUY) <= raw
    assert quantize_price(raw, tick_size=Decimal("0.01"), side=OrderSide.SELL) >= raw


def test_price_is_clamped_inside_payout_bounds() -> None:
    assert quantize_price(
        Decimal("0.0001"), tick_size=Decimal("0.01"), side=OrderSide.BUY
    ) == Decimal("0.01")
    assert quantize_price(
        Decimal("0.9999"), tick_size=Decimal("0.01"), side=OrderSide.SELL
    ) == Decimal("0.99")


def test_fine_grained_tick_is_respected() -> None:
    price = quantize_price(
        Decimal("0.12345"), tick_size=Decimal("0.0025"), side=OrderSide.BUY
    )
    assert price == Decimal("0.1225")
    assert is_on_tick(price, tick_size=Decimal("0.0025"))


def test_size_rounds_down_to_lot() -> None:
    assert quantize_size(Decimal("12.3456")) == Decimal("12.34")
    assert quantize_size(Decimal("100")) == Decimal("100.00")


def test_ticks_between_measures_absolute_distance() -> None:
    assert ticks_between(
        Decimal("0.55"), Decimal("0.52"), tick_size=Decimal("0.01")
    ) == Decimal("3")
    assert ticks_between(
        Decimal("0.52"), Decimal("0.55"), tick_size=Decimal("0.01")
    ) == Decimal("3")
