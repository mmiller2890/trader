from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.loops import sweep_stale_resting_orders
from config.schema import AppConfig, Mode
from execution.stale_orders import (
    ENTRY_TTL_REASON,
    MARKET_ENDED_REASON,
    stale_resting_orders,
)
from models.order import (
    CancelOutcome,
    CancelResult,
    OrderResult,
    OrderSide,
    OrderStatus,
)
from models.position import PositionLifecycle
from state.store import InMemoryStateStore

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)


def resting(
    *,
    client_order_id: str = "pm-bot-resting0001",
    age_seconds: float = 0.0,
    liquidity: str = "maker",
    market_id: str | None = "m1",
    side: OrderSide | None = OrderSide.BUY,
) -> OrderResult:
    return OrderResult(
        client_order_id=client_order_id,
        exchange_order_id="0x" + "a" * 64,
        market_id=market_id,
        token_id="t1",
        side=side,
        status=OrderStatus.SUBMITTED,
        accepted=True,
        requested_size=Decimal("5"),
        filled_size=Decimal("0"),
        liquidity=liquidity,
        created_at=NOW - timedelta(seconds=age_seconds),
    )


def test_order_past_its_ttl_is_cancelled() -> None:
    intents = stale_resting_orders(
        open_orders=[resting(age_seconds=45)], now=NOW, ttl_seconds=30
    )

    assert len(intents) == 1
    assert intents[0].reason == ENTRY_TTL_REASON
    assert intents[0].client_order_id == "pm-bot-resting0001"


def test_order_inside_its_ttl_is_left_alone() -> None:
    assert (
        stale_resting_orders(
            open_orders=[resting(age_seconds=5)], now=NOW, ttl_seconds=30
        )
        == []
    )


def test_resting_order_in_an_ended_market_is_cancelled_before_its_ttl() -> None:
    """
    A fill after the market ends lands inventory that cannot be traded out of.
    That outranks the TTL, which may still have time left on it.
    """

    intents = stale_resting_orders(
        open_orders=[resting(age_seconds=5)],
        now=NOW,
        ttl_seconds=3600,
        market_end_lookup=lambda market_id, token_id: NOW - timedelta(minutes=1),
    )

    assert len(intents) == 1
    assert intents[0].reason == MARKET_ENDED_REASON


def test_pending_exit_orders_are_never_swept_here() -> None:
    """
    PositionExitManager sweeps exits on its own deadline and then escalates to
    a taker cross. Cancelling one here would race that escalation and could
    release the reservation while it is mid-flight.
    """

    assert (
        stale_resting_orders(
            open_orders=[resting(client_order_id="pm-bot-exit00000001", age_seconds=999)],
            now=NOW,
            ttl_seconds=30,
            protected_client_order_ids={"pm-bot-exit00000001"},
        )
        == []
    )


def test_taker_orders_are_not_swept() -> None:
    assert (
        stale_resting_orders(
            open_orders=[resting(age_seconds=999, liquidity="taker")],
            now=NOW,
            ttl_seconds=30,
        )
        == []
    )


def test_orders_missing_identity_are_skipped_rather_than_guessed_at() -> None:
    """A cancel aimed at the wrong order is worse than a late one."""

    assert (
        stale_resting_orders(
            open_orders=[
                resting(age_seconds=999, market_id=None),
                resting(age_seconds=999, side=None),
            ],
            now=NOW,
            ttl_seconds=30,
        )
        == []
    )


def test_a_failing_market_end_lookup_does_not_block_ttl_sweeping() -> None:
    def broken(market_id: str, token_id: str) -> datetime:
        raise RuntimeError("lookup exploded")

    intents = stale_resting_orders(
        open_orders=[resting(age_seconds=45)],
        now=NOW,
        ttl_seconds=30,
        market_end_lookup=broken,
    )

    assert len(intents) == 1
    assert intents[0].reason == ENTRY_TTL_REASON


# --- wiring: the sweep must actually run, not just exist -------------------


class RecordingSubmitter:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_order(self, intent) -> CancelResult:  # type: ignore[no-untyped-def]
        self.cancelled.append(intent.client_order_id)
        return CancelResult(
            client_order_id=intent.client_order_id,
            outcome=CancelOutcome.CANCELLED,
        )


class Services:
    def __init__(self, state_store, submitter, config) -> None:  # type: ignore[no-untyped-def]
        self.state_store = state_store
        self.submitter = submitter
        self.config = config


async def _store_with_resting_order(age_seconds: float) -> InMemoryStateStore:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_order_status(resting(age_seconds=age_seconds))
    return state


@pytest.mark.asyncio
async def test_sweep_cancels_a_stale_order_and_clears_it_locally() -> None:
    state = await _store_with_resting_order(age_seconds=10_000)
    submitter = RecordingSubmitter()
    config = AppConfig(spike_strategy={"quote_ttl_seconds": 30.0})

    cancelled = await sweep_stale_resting_orders(
        Services(state, submitter, config), now=NOW
    )

    assert cancelled == ["pm-bot-resting0001"]
    assert submitter.cancelled == ["pm-bot-resting0001"]
    # Cleared locally so the next pass does not re-cancel an order already off
    # the book.
    assert await state.get_open_orders() == []


@pytest.mark.asyncio
async def test_sweep_leaves_a_pending_exit_order_alone() -> None:
    state = await _store_with_resting_order(age_seconds=10_000)
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
        pending_exit_client_order_id="pm-bot-resting0001",
    )
    submitter = RecordingSubmitter()
    config = AppConfig(spike_strategy={"quote_ttl_seconds": 30.0})

    cancelled = await sweep_stale_resting_orders(
        Services(state, submitter, config), now=NOW
    )

    assert cancelled == []
    assert submitter.cancelled == []
    assert len(await state.get_open_orders()) == 1


@pytest.mark.asyncio
async def test_market_ended_sweep_works_for_an_entry_with_no_position() -> None:
    """
    The market-ended branch exists for resting *entries*, which have no
    position yet -- that is what makes them entries. Building the market-end
    map from position lifecycles alone returns None for exactly those orders,
    so the branch can never fire for the case it was written for.

    The clock is injected so only the market-ended branch can cancel here; the
    TTL has plenty of time left on it.
    """

    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_order_status(resting(age_seconds=5))
    submitter = RecordingSubmitter()
    config = AppConfig(spike_strategy={"quote_ttl_seconds": 600.0})

    class Rotator:
        def status(self):  # type: ignore[no-untyped-def]
            class Market:
                condition_id = "m1"
                asset_ids = ["t1"]
                end_at = NOW - timedelta(minutes=1)

            class Status:
                current_market = Market()

            return Status()

    services = Services(state, submitter, config)
    services.market_rotator = Rotator()

    cancelled = await sweep_stale_resting_orders(services, now=NOW)

    assert cancelled == ["pm-bot-resting0001"]
    assert submitter.cancelled == ["pm-bot-resting0001"]
