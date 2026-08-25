"""Exchange tick-size and lot-size quantization.

Polymarket's CLOB rejects any order whose price is not an exact multiple of
the market's tick size, and whose size carries more than two decimal places.
Every price and size that leaves this process must pass through here.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from models.order import OrderSide

#: Tick sizes the CLOB accepts, mirroring ``py_clob_client_v2.TickSize``.
SUPPORTED_TICK_SIZES: tuple[Decimal, ...] = (
    Decimal("0.1"),
    Decimal("0.01"),
    Decimal("0.005"),
    Decimal("0.0025"),
    Decimal("0.001"),
    Decimal("0.0001"),
)

DEFAULT_TICK_SIZE = Decimal("0.01")

#: The CLOB rounds every order size to two decimals regardless of tick size.
SIZE_INCREMENT = Decimal("0.01")


class TickSizeError(ValueError):
    """Raised when a tick size is not one the exchange accepts."""


def normalize_tick_size(tick_size: Decimal | str | float) -> Decimal:
    """Return ``tick_size`` as a supported Decimal or raise."""

    candidate = Decimal(str(tick_size))
    for supported in SUPPORTED_TICK_SIZES:
        if candidate == supported:
            return supported
    raise TickSizeError(f"unsupported tick size: {tick_size}")


def quantize_price(
    price: Decimal,
    *,
    tick_size: Decimal,
    side: OrderSide,
    aggressive: bool = False,
) -> Decimal:
    """
    Snap ``price`` onto the tick grid, preserving the caller's intent.

    A passive (resting) quote rounds away from the market -- BUY down, SELL up
    -- so quantization can only make it less likely to cross. A marketable
    order rounds toward the market -- BUY up, SELL down -- so quantization
    cannot turn a taker into a quote that never fills.

    The result is clamped into ``[tick, 1 - tick]`` because the exchange
    rejects prices at or beyond the payout bounds.
    """

    tick = normalize_tick_size(tick_size)
    round_up_side = OrderSide.SELL if not aggressive else OrderSide.BUY
    rounding = ROUND_UP if side == round_up_side else ROUND_DOWN
    snapped = (Decimal(price) / tick).quantize(
        Decimal("1"), rounding=rounding
    ) * tick
    lowest = tick
    highest = Decimal("1") - tick
    if snapped < lowest:
        snapped = lowest
    if snapped > highest:
        snapped = highest
    return snapped.quantize(tick)


def quantize_size(size: Decimal) -> Decimal:
    """Round ``size`` down onto the exchange lot grid."""

    return Decimal(size).quantize(SIZE_INCREMENT, rounding=ROUND_DOWN)


def is_on_tick(price: Decimal, *, tick_size: Decimal) -> bool:
    """True when ``price`` sits exactly on the tick grid."""

    tick = normalize_tick_size(tick_size)
    return Decimal(price) % tick == 0


def ticks_between(left: Decimal, right: Decimal, *, tick_size: Decimal) -> Decimal:
    """Return the absolute distance between two prices measured in ticks."""

    tick = normalize_tick_size(tick_size)
    return (Decimal(left) - Decimal(right)).copy_abs() / tick
