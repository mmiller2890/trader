"""Position sizing helpers."""

from __future__ import annotations

from decimal import Decimal

from config.schema import ExecutionConfig
from models.position import Balance


def clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    """Clamp decimal value between inclusive bounds."""

    if minimum > maximum:
        raise ValueError("minimum must be <= maximum")
    return max(minimum, min(value, maximum))


def fixed_size(config: ExecutionConfig) -> Decimal:
    """Return deterministic configured default order size."""

    return clamp(config.default_order_size, config.min_order_size, config.max_order_size)


def balance_percent_cap(balance: Balance | None, percent: Decimal) -> Decimal:
    """Compute notional cap as a percent of available balance."""

    if percent < 0 or percent > Decimal("1"):
        raise ValueError("percent must be within [0, 1]")
    if balance is None:
        return Decimal("0")
    return balance.available * percent
