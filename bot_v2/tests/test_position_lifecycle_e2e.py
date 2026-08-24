from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from config.schema import AppConfig, Mode
from execution.order_builder import OrderBuilder
from execution.router import ExecutionRouter
from execution.submitter import OrderSubmitter
from execution.tracker import OrderTracker
from models.events import BotEvent, EventType
from models.market import MarketSnapshot
from models.order import OrderResult, OrderSide, OrderStatus
from models.signal import SignalSide, TradeSignal
from notifications.events import EventBus
from persistence.snapshots import SnapshotStore
from portfolio.exit_manager import PositionExitManager
from portfolio.exit_policy import PositionExitPolicy
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore


NOW = datetime(2025, 1, 1, tzinfo=UTC)


class RecordingJournal:
    def __init__(self) -> None:
        self.events: list[BotEvent] = []

    async def append(self, event: BotEvent) -> None:
        self.events.append(event)


class RecordingSubmitter:
    def __init__(self, *, partial_fills: list[Decimal] | None = None, unknown: bool = False) -> None:
        self.orders: list[object] = []
        self._partial_fills = list(partial_fills or [])
        self._unknown = unknown

    async def submit(self, order: object) -> OrderResult:
        self.orders.append(order)
        if self._unknown and order.side == OrderSide.SELL:
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=OrderStatus.UNKNOWN,
                accepted=False,
                message="order submission outcome unknown: timeout",
                requested_size=order.size,
            )
        if self._partial_fills and order.side == OrderSide.SELL:
            filled = self._partial_fills.pop(0)
            status = (
                OrderStatus.FILLED
                if filled == order.size
                else OrderStatus.PARTIALLY_FILLED
            )
            return OrderResult(
                client_order_id=order.client_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                status=status,
                accepted=True,
                message="filled",
                requested_size=order.size,
                filled_size=filled,
                avg_fill_price=order.price,
            )
        return OrderResult(
            client_order_id=order.client_order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            status=OrderStatus.SIMULATED,
            accepted=True,
            message="simulated_submission",
            requested_size=order.size,
            filled_size=order.size,
            avg_fill_price=order.price,
        )


class DryRunLifecycleHarness:
    def __init__(
        self,
        *,
        tmp_path: Path,
        take_profit_bps: str = "300",
        submitter: RecordingSubmitter | None = None,
    ) -> None:
        self.clock = {"now": NOW}
        self.config = AppConfig(
            bot={"mode": Mode.DRY_RUN},
            execution={
                "default_order_size": "1",
                "min_order_size": "1",
                "max_order_size": "25",
                "time_in_force": "FOK",
            },
            position_management={
                "take_profit_bps": take_profit_bps,
                "stop_loss_bps": "200",
                "max_hold_seconds": 180,
                "exit_retry_interval_seconds": 2,
                "max_exit_attempts": 3,
            },
            risk={
                "min_top_of_book_liquidity": "1",
                "duplicate_signal_window_seconds": 0,
            },
        )
        self.state = InMemoryStateStore(mode=Mode.DRY_RUN)
        self.snapshots = SnapshotStore(tmp_path / "state.json")
        self.journal = RecordingJournal()
        self.event_bus = EventBus()
        self.submitter = submitter or RecordingSubmitter()
        self.tracker = OrderTracker(
            self.state,
            snapshots=self.snapshots,
            confirmation_grace_seconds=30,
        )
        self.router = ExecutionRouter(
            config=self.config,
            state_store=self.state,
            risk_engine=PreTradeRiskEngine(
                config=self.config,
                state_store=self.state,
                now=lambda: self.clock["now"],
            ),
            order_builder=OrderBuilder(self.config),
            submitter=self.submitter,
            tracker=self.tracker,
            journal=self.journal,
            event_bus=self.event_bus,
        )
        self.exit_policy = PositionExitPolicy(
            self.config.position_management,
            min_order_size=self.config.execution.min_order_size,
            max_data_age_seconds=self.config.risk.max_data_staleness_seconds,
        )
        self.exit_manager = PositionExitManager(
            config=self.config,
            state_store=self.state,
            snapshots=self.snapshots,
            policy=self.exit_policy,
            now=lambda: self.clock["now"],
            on_event=self.journal.append,
        )

    @classmethod
    async def start(
        cls,
        tmp_path: Path,
        *,
        take_profit_bps: str = "300",
        submitter: RecordingSubmitter | None = None,
    ) -> "DryRunLifecycleHarness":
        return cls(
            tmp_path=tmp_path,
            take_profit_bps=take_profit_bps,
            submitter=submitter,
        )

    async def publish(self, snapshot: MarketSnapshot) -> None:
        await self.state.update_market_snapshot(snapshot)
        for exit_signal in await self.exit_manager.on_market_update(
            snapshot, market_end_at=None
        ):
            await self.router.route_signal(exit_signal, snapshot=snapshot)

    async def route(self, signal: TradeSignal) -> None:
        await self.router.route_signal(signal, snapshot=await self.state.get_market_snapshot(signal.market_id, signal.token_id))


def snapshot(*, mid: str, bid: str, ask: str) -> MarketSnapshot:
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=Decimal(mid),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=NOW,
        received_ts=NOW,
    )


def buy_signal() -> TradeSignal:
    return TradeSignal(
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.50"),
        observed_move_bps=100,
        created_at=NOW,
        reason="test",
    )


@pytest.mark.asyncio
async def test_dry_run_buy_then_take_profit_closes_position(tmp_path: Path) -> None:
    harness = await DryRunLifecycleHarness.start(
        tmp_path, take_profit_bps="300"
    )
    await harness.publish(snapshot(mid="0.50", bid="0.49", ask="0.50"))
    await harness.route(buy_signal())
    opened = await harness.state.get_position("m1", "t1")
    assert opened is not None and opened.quantity > 0

    await harness.publish(snapshot(mid="0.52", bid="0.515", ask="0.52"))
    assert await harness.state.get_position("m1", "t1") is None
    lifecycle_events = [event.event_type for event in harness.journal.events if event.event_type in {
        EventType.POSITION_UPDATED, EventType.EXIT_TRIGGERED, EventType.POSITION_CLOSED,
    }]
    assert lifecycle_events == [
        EventType.POSITION_UPDATED, EventType.EXIT_TRIGGERED, EventType.POSITION_CLOSED,
    ]


@pytest.mark.asyncio
async def test_partial_exit_retries_only_remaining_inventory(tmp_path: Path) -> None:
    submitter = RecordingSubmitter(partial_fills=[Decimal("1"), Decimal("1")])
    harness = await DryRunLifecycleHarness.start(
        tmp_path, take_profit_bps="300", submitter=submitter
    )
    await harness.publish(snapshot(mid="0.50", bid="0.49", ask="0.50"))
    await harness.route(buy_signal())
    harness.clock["now"] = NOW + timedelta(seconds=1)
    await harness.route(
        buy_signal().model_copy(
            update={
                "signal_id": "signal87654321",
                "created_at": NOW + timedelta(seconds=1),
            }
        )
    )
    position = await harness.state.get_position("m1", "t1")
    assert position is not None and position.quantity == Decimal("2")

    await harness.publish(snapshot(mid="0.52", bid="0.515", ask="0.52"))
    position = await harness.state.get_position("m1", "t1")
    assert position is not None and position.quantity == Decimal("1")

    exit_orders = [
        order for order in submitter.orders
        if order.side == OrderSide.SELL
    ]
    assert len(exit_orders) == 1
    assert exit_orders[0].size == Decimal("2")

    harness.clock["now"] = NOW + timedelta(seconds=3)
    await harness.publish(snapshot(mid="0.52", bid="0.515", ask="0.52"))
    assert await harness.state.get_position("m1", "t1") is None
    exit_orders = [
        order for order in submitter.orders
        if order.side == OrderSide.SELL
    ]
    assert len(exit_orders) == 2
    assert exit_orders[1].size == Decimal("1")


@pytest.mark.asyncio
async def test_unknown_exit_outcome_halts_without_retry(tmp_path: Path) -> None:
    submitter = RecordingSubmitter(unknown=True)
    harness = await DryRunLifecycleHarness.start(
        tmp_path, take_profit_bps="300", submitter=submitter
    )
    await harness.publish(snapshot(mid="0.50", bid="0.49", ask="0.50"))
    await harness.route(buy_signal())
    position = await harness.state.get_position("m1", "t1")
    assert position is not None and position.quantity == Decimal("1")

    await harness.publish(snapshot(mid="0.52", bid="0.515", ask="0.52"))
    position = await harness.state.get_position("m1", "t1")
    assert position is not None and position.quantity == Decimal("1")
    assert await harness.state.is_kill_switch_active() is True
    lifecycle = await harness.state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.pending_exit_client_order_id is not None

    exit_orders = [
        order for order in submitter.orders
        if order.side == OrderSide.SELL
    ]
    assert len(exit_orders) == 1

    harness.exit_manager.set_clock(lambda: NOW + timedelta(seconds=10))
    await harness.publish(snapshot(mid="0.52", bid="0.515", ask="0.52"))
    assert len(exit_orders) == 1
