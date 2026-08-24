from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.bootstrap import _position_market_end, _rotation_safe
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


def test_position_deadline_lookup_uses_rotator_current_market() -> None:
    initial = discovered_like_market()

    class CurrentMarket:
        market_id = "gamma-market-456"
        condition_id = "0xconditiondef"
        end_at = NOW + timedelta(minutes=20)

        class Up:
            token_id = "up-token"

        class Down:
            token_id = "down-token"

        up = Up()
        down = Down()

        @property
        def asset_ids(self) -> list[str]:
            return [self.up.token_id, self.down.token_id]

    current = CurrentMarket()

    class Rotator:
        def status(self) -> object:
            class Status:
                current_market = current

            return Status()

    assert _position_market_end(
        initial,
        Rotator(),
        market_id="0xconditiondef",
        token_id="up-token",
    ) == current.end_at
    assert _position_market_end(
        initial,
        Rotator(),
        market_id="0xconditionabc",
        token_id="old-token",
    ) is None
