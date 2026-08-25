"""Cancel-before-replace routing for resting quotes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from config.schema import AppConfig, MarketMakerConfig, Mode
from execution.order_builder import OrderBuilder
from execution.router import ExecutionRouter
from execution.tracker import OrderTracker
from models.events import EventType
from models.market import MarketSnapshot
from models.order import (
    CancelIntent,
    CancelOutcome,
    CancelResult,
    OrderRequest,
    OrderResult,
    OrderStatus,
)
from models.position import Position
from models.tick import DEFAULT_TICK_SIZE
from notifications.events import EventBus
from persistence.snapshots import SnapshotStore
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore
from strategies.market_maker import MarketMakerStrategy

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


class StubSubmitter:
    """Records submissions and returns scripted cancellation outcomes."""

    def __init__(self, *, cancel_outcome: CancelOutcome = CancelOutcome.CANCELLED):
        self.orders: list[OrderRequest] = []
        self.cancels: list[CancelIntent] = []
        self._cancel_outcome = cancel_outcome

    async def submit(self, order: OrderRequest) -> OrderResult:
        self.orders.append(order)
        return OrderResult(
            client_order_id=order.client_order_id,
            exchange_order_id=f"0x{len(self.orders):04d}",
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            status=OrderStatus.SUBMITTED,
            accepted=True,
            requested_size=order.size,
        )

    async def cancel_order(self, intent: CancelIntent) -> CancelResult:
        self.cancels.append(intent)
        return CancelResult(
            client_order_id=intent.client_order_id,
            exchange_order_id=intent.exchange_order_id,
            outcome=self._cancel_outcome,
            message="scripted",
        )


class RecordingJournal:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, event: object) -> None:
        self.events.append(event)


def mm_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.DRY_RUN},
        execution={
            "default_order_size": "100",
            "min_order_size": "1",
            "max_order_size": "500",
            "time_in_force": "GTC",
        },
        risk={
            "max_single_position_size": "500",
            "max_total_exposure": "500",
            "max_open_orders": "10",
            "min_top_of_book_liquidity": "1",
        },
    )


def book(*, bid: str = "0.49", ask: str = "0.51") -> MarketSnapshot:
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=(Decimal(bid) + Decimal(ask)) / 2,
        top_bid_size=Decimal("500"),
        top_ask_size=Decimal("500"),
        received_ts=datetime.now(tz=UTC),
        source_ts=datetime.now(tz=UTC),
    )


class Harness:
    def __init__(self, *, cancel_outcome: CancelOutcome = CancelOutcome.CANCELLED):
        self.config = mm_config()
        self.state = InMemoryStateStore(mode=Mode.DRY_RUN)
        self.journal = RecordingJournal()
        self.submitter = StubSubmitter(cancel_outcome=cancel_outcome)
        self.router = ExecutionRouter(
            config=self.config,
            state_store=self.state,
            risk_engine=PreTradeRiskEngine(
                config=self.config, state_store=self.state
            ),
            order_builder=OrderBuilder(self.config),
            submitter=self.submitter,
            tracker=OrderTracker(self.state),
            journal=self.journal,
            event_bus=EventBus(),
        )
        self.clock = {"now": NOW}
        self.strategy = MarketMakerStrategy(
            MarketMakerConfig(
                enabled=True,
                base_quote_size=Decimal("100"),
                min_quote_size=Decimal("5"),
                max_position_size=Decimal("400"),
            ),
            position_reader=self.state.get_position,
            tick_size_provider=lambda token_id: DEFAULT_TICK_SIZE,
            now=lambda: self.clock["now"],
        )

    async def quote(self, snapshot: MarketSnapshot) -> None:
        plan = await self.strategy.plan_quotes(snapshot)
        await self.router.route_quote_plan(
            plan, strategy=self.strategy, snapshot=snapshot
        )

    def event_types(self) -> list[str]:
        return [event.event_type.value for event in self.journal.events]


@pytest.mark.asyncio
async def test_first_quote_is_submitted_and_tracked() -> None:
    harness = Harness()

    await harness.quote(book())

    assert len(harness.submitter.orders) == 1
    order = harness.submitter.orders[0]
    assert order.post_only is True
    assert order.price == Decimal("0.49")
    resting = harness.strategy.resting_quotes()
    assert len(resting) == 1
    assert resting[0].client_order_id == order.client_order_id
    assert EventType.QUOTE_PLACED.value in harness.event_types()


@pytest.mark.asyncio
async def test_refresh_cancels_the_old_order_before_posting_the_new_one() -> None:
    harness = Harness()
    await harness.quote(book())
    first = harness.submitter.orders[0]

    await harness.quote(book(bid="0.53", ask="0.55"))

    assert len(harness.submitter.cancels) == 1
    assert harness.submitter.cancels[0].client_order_id == first.client_order_id
    assert len(harness.submitter.orders) == 2
    assert harness.submitter.orders[1].price == Decimal("0.53")
    # Exactly one bid is live at the end.
    assert len(harness.strategy.resting_quotes()) == 1


@pytest.mark.asyncio
async def test_unconfirmed_cancel_withholds_the_replacement() -> None:
    harness = Harness(cancel_outcome=CancelOutcome.UNKNOWN)
    await harness.quote(book())
    assert len(harness.submitter.orders) == 1

    await harness.quote(book(bid="0.53", ask="0.55"))

    # The stale order may still be live, so no second quote is posted.
    assert len(harness.submitter.cancels) == 1
    assert len(harness.submitter.orders) == 1
    assert EventType.QUOTE_CANCEL_FAILED.value in harness.event_types()


@pytest.mark.asyncio
async def test_unconfirmed_cancel_restores_the_quote_for_the_next_attempt() -> None:
    harness = Harness(cancel_outcome=CancelOutcome.FAILED)
    await harness.quote(book())
    original = harness.strategy.resting_quotes()[0]

    await harness.quote(book(bid="0.53", ask="0.55"))

    resting = harness.strategy.resting_quotes()
    assert len(resting) == 1
    assert resting[0].client_order_id == original.client_order_id


@pytest.mark.asyncio
async def test_an_order_that_already_left_the_book_is_a_clean_cancel() -> None:
    harness = Harness(cancel_outcome=CancelOutcome.NOT_FOUND)
    await harness.quote(book())

    await harness.quote(book(bid="0.53", ask="0.55"))

    assert len(harness.submitter.orders) == 2
    assert EventType.QUOTE_CANCELLED.value in harness.event_types()


@pytest.mark.asyncio
async def test_withdrawal_cancels_without_requoting() -> None:
    harness = Harness()
    await harness.quote(book())

    plan = await harness.strategy.plan_withdrawal("halting")
    await harness.router.route_quote_plan(plan, strategy=harness.strategy)

    assert len(harness.submitter.cancels) == 1
    assert len(harness.submitter.orders) == 1
    assert harness.strategy.resting_quotes() == []


@pytest.mark.asyncio
async def test_quotes_are_blocked_by_the_kill_switch() -> None:
    harness = Harness()
    await harness.state.activate_kill_switch("manual halt")

    await harness.quote(book())

    assert harness.submitter.orders == []
    assert harness.strategy.resting_quotes() == []


@pytest.mark.asyncio
async def test_quote_rejected_by_risk_is_not_tracked_as_resting() -> None:
    harness = Harness()
    # Fill the position to the risk cap so the bid cannot be approved.
    await harness.state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("500"),
            average_entry_price=Decimal("0.50"),
            mark_price=Decimal("0.50"),
        )
    )

    await harness.quote(book())

    bids = [q for q in harness.strategy.resting_quotes() if q.side.value == "buy"]
    assert bids == []
