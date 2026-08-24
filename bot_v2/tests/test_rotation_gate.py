from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.bootstrap import _rotation_safe
from config.schema import AppConfig, Mode
from models.position import Position, PositionLifecycle
from state.store import InMemoryStateStore


NOW = datetime(2025, 1, 1, tzinfo=UTC)


def discovered_like_market() -> object:
    """Shape-compatible stand-in carrying both Gamma and CLOB identifiers."""

    class Market:
        market_id = "gamma-market-123"
        condition_id = "0xconditionabc"
        end_at = NOW + timedelta(minutes=5)

    return Market()


def state_with_position(*, market_id: str, quantity: str) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    state._positions[(market_id, "t1")] = Position(
        market_id=market_id,
        token_id="t1",
        quantity=Decimal(quantity),
        average_entry_price=Decimal("0.40"),
    )
    state._lifecycles[(market_id, "t1")] = PositionLifecycle(
        market_id=market_id,
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
    )
    return state


async def test_rotation_blocked_by_clob_condition_id_match() -> None:
    state = state_with_position(market_id="0xconditionabc", quantity="2")
    assert await _rotation_safe(state, discovered_like_market(), AppConfig()) is False


async def test_rotation_allowed_when_only_gamma_ids_differ() -> None:
    state = state_with_position(market_id="0xdifferentmarket", quantity="2")
    assert await _rotation_safe(state, discovered_like_market(), AppConfig()) is True


async def test_rotation_allowed_for_dust_in_ending_market() -> None:
    state = state_with_position(market_id="0xconditionabc", quantity="0.5")
    assert await _rotation_safe(state, discovered_like_market(), AppConfig()) is True
