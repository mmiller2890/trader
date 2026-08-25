from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from config.schema import AppConfig, Mode
from execution.order_builder import OrderBuilder
from execution.router import ExecutionRouter
from execution.tracker import OrderTracker
from models.market import MarketSnapshot
from models.order import OrderRequest, OrderResult, OrderStatus
from models.position import ExitReason, Position, PositionLifecycle
from models.signal import SignalSide, SignalType, TradeSignal
from notifications.events import EventBus
from persistence.snapshots import SnapshotStore
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore


class RecordingSubmitter:
    def __init__(self) -> None:
        self.orders: list[OrderRequest] = []

    async def submit(self, order: OrderRequest) -> OrderResult:
        self.orders.append(order)
        return OrderResult(
            client_order_id=order.client_order_id,
            market_id=order.market_id,
            token_id=order.token_id,
            side=order.side,
            status=OrderStatus.SUBMITTED,
            accepted=True,
            requested_size=order.size,
        )


class RecordingJournal:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, event: object) -> None:
        self.events.append(event)


def live_config(*, cap: str = "1", max_position: str = "2") -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={
            "allow_live_trading": True,
            "dry_run_force": False,
            "default_order_size": "5",
            "min_order_size": "1",
            "min_live_buy_notional": str(min(Decimal("1"), Decimal(cap))),
            "max_live_order_notional": cap,
            "time_in_force": "FOK",
        },
        risk={
            "max_single_position_size": max_position,
            "max_total_exposure": max_position,
            "min_top_of_book_liquidity": "1",
        },
    )


def snapshot(*, ask: str = "0.50") -> MarketSnapshot:
    now = datetime.now(tz=UTC)
    ask_price = Decimal(ask)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=ask_price - Decimal("0.01"),
        best_ask=ask_price,
        mid_price=ask_price - Decimal("0.005"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=now,
        received_ts=now,
    )


def signal() -> TradeSignal:
    return TradeSignal(
        signal_id="signal12345678",
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.50"),
        observed_move_bps=100,
        reason="test",
    )


def router(config: AppConfig, submitter: RecordingSubmitter) -> ExecutionRouter:
    state = InMemoryStateStore(mode=Mode.LIVE)
    return ExecutionRouter(
        config=config,
        state_store=state,
        risk_engine=PreTradeRiskEngine(config=config, state_store=state),
        order_builder=OrderBuilder(config),
        submitter=submitter,
        tracker=OrderTracker(state),
        journal=RecordingJournal(),
        event_bus=EventBus(),
    )


@pytest.mark.asyncio
async def test_router_risk_checks_and_submits_cap_aware_size() -> None:
    submitter = RecordingSubmitter()
    execution_router = router(live_config(), submitter)

    await execution_router.route_signal(signal(), snapshot=snapshot())

    assert len(submitter.orders) == 1
    assert submitter.orders[0].size == Decimal("2.000000")


@pytest.mark.asyncio
async def test_router_rejects_unfundable_minimum_without_crashing() -> None:
    submitter = RecordingSubmitter()
    execution_router = router(
        live_config(cap="0.5", max_position="5"),
        submitter,
    )

    await execution_router.route_signal(signal(), snapshot=snapshot(ask="0.90"))

    assert submitter.orders == []


@pytest.mark.asyncio
async def test_router_does_not_reemit_an_already_latched_kill_switch() -> None:
    config = live_config()
    state = InMemoryStateStore(mode=Mode.LIVE, kill_switch_active=True)
    submitter = RecordingSubmitter()
    journal = RecordingJournal()
    execution_router = ExecutionRouter(
        config=config,
        state_store=state,
        risk_engine=PreTradeRiskEngine(config=config, state_store=state),
        order_builder=OrderBuilder(config),
        submitter=submitter,
        tracker=OrderTracker(state),
        journal=journal,
        event_bus=EventBus(),
    )

    await execution_router.route_signal(signal(), snapshot=snapshot())
    await execution_router.route_signal(
        signal().model_copy(update={"signal_id": "signal87654321"}),
        snapshot=snapshot(),
    )

    halt_events = [
        event for event in journal.events
        if event.event_type.value == "kill_switch_tripped"
    ]
    assert halt_events == []
    assert submitter.orders == []


@pytest.mark.asyncio
async def test_builder_failure_releases_exit_reservation() -> None:
    config = live_config(cap="0.01", max_position="5")
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("2"),
            average_entry_price=Decimal("0.40"),
        )
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=datetime.now(tz=UTC),
        last_fill_at=datetime.now(tz=UTC),
    )
    reserved = await state.reserve_exit(
        "m1",
        "t1",
        client_order_id="pm-bot-signal12345678",
        reason=ExitReason.TAKE_PROFIT,
        attempted_at=datetime.now(tz=UTC),
    )
    assert reserved is True

    submitter = RecordingSubmitter()
    execution_router = ExecutionRouter(
        config=config,
        state_store=state,
        risk_engine=PreTradeRiskEngine(config=config, state_store=state),
        order_builder=OrderBuilder(config),
        submitter=submitter,
        tracker=OrderTracker(state),
        journal=RecordingJournal(),
        event_bus=EventBus(),
    )
    exit_sell = signal().model_copy(
        update={
            "side": SignalSide.SELL,
            "signal_type": SignalType.POSITION_EXIT,
            "reduce_only": True,
            "requested_size": Decimal("2"),
            "reason": "position_exit:take_profit",
        }
    )

    await execution_router.route_signal(exit_sell, snapshot=snapshot())

    assert submitter.orders == []
    lifecycle = await state.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.pending_exit_client_order_id is None


@pytest.mark.asyncio
async def test_exit_reservation_release_is_saved_immediately(tmp_path) -> None:
    config = live_config(cap="0.01", max_position="5")
    state = InMemoryStateStore(mode=Mode.LIVE)
    now = datetime.now(tz=UTC)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("2"),
            average_entry_price=Decimal("0.40"),
        )
    )
    await state.restore_position_lifecycle(
        PositionLifecycle(
            market_id="m1",
            token_id="t1",
            opened_at=now,
            last_fill_at=now,
        )
    )
    await state.reserve_exit(
        "m1",
        "t1",
        client_order_id="pm-bot-signal12345678",
        reason=ExitReason.TAKE_PROFIT,
        attempted_at=now,
    )
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(state)
    submitter = RecordingSubmitter()
    execution_router = ExecutionRouter(
        config=config,
        state_store=state,
        risk_engine=PreTradeRiskEngine(config=config, state_store=state),
        order_builder=OrderBuilder(config),
        submitter=submitter,
        tracker=OrderTracker(state, snapshots=snapshots),
        journal=RecordingJournal(),
        event_bus=EventBus(),
        snapshots=snapshots,
    )
    exit_sell = signal().model_copy(
        update={
            "side": SignalSide.SELL,
            "signal_type": SignalType.POSITION_EXIT,
            "reduce_only": True,
            "requested_size": Decimal("2"),
            "reason": "position_exit:take_profit",
        }
    )

    await execution_router.route_signal(exit_sell, snapshot=snapshot())

    restored = InMemoryStateStore(mode=Mode.LIVE)
    assert await snapshots.restore_into_state(restored) is True
    lifecycle = await restored.get_position_lifecycle("m1", "t1")
    assert lifecycle is not None
    assert lifecycle.pending_exit_client_order_id is None


@pytest.mark.asyncio
async def test_signal_is_risked_against_its_own_book_not_the_callers() -> None:
    """
    Regression: complement-routed signals were risked against the wrong token.

    The spike strategy observes token A and may route the order to token B.
    Bootstrap passes A's snapshot to the router, so every price, spread, and
    liquidity check was evaluated on a book the order never touches -- a
    tight book vouching for a broken one.
    """

    config = AppConfig(
        bot={"mode": Mode.DRY_RUN},
        risk={"min_top_of_book_liquidity": "1", "max_entry_spread_bps": 600},
    )
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    now = datetime.now(tz=UTC)

    def book(token: str, bid: str, ask: str) -> MarketSnapshot:
        return MarketSnapshot(
            market_id="m1", token_id=token,
            best_bid=Decimal(bid), best_ask=Decimal(ask),
            mid_price=(Decimal(bid) + Decimal(ask)) / 2,
            top_bid_size=Decimal("500"), top_ask_size=Decimal("500"),
            received_ts=now, source_ts=now,
        )

    tight = book("t1", "0.49", "0.50")
    broken = book("t2", "0.09", "0.91")
    await state.update_market_snapshot(broken)

    submitter = RecordingSubmitter()
    journal = RecordingJournal()
    router = ExecutionRouter(
        config=config,
        state_store=state,
        risk_engine=PreTradeRiskEngine(config=config, state_store=state),
        order_builder=OrderBuilder(config),
        submitter=submitter,
        tracker=OrderTracker(state),
        journal=journal,
        event_bus=EventBus(),
    )

    signal = TradeSignal(
        strategy_name="spike", market_id="m1", token_id="t2",
        side=SignalSide.BUY, reference_price=Decimal("0.50"),
        target_price=Decimal("0.91"), observed_move_bps=100,
        reason="spike_up_via_complement",
    )

    # Caller hands over t1's tight book while the order trades t2's broken one.
    await router.route_signal(signal, snapshot=tight)

    assert submitter.orders == []
    reasons = [getattr(e, "reason", "") or "" for e in journal.events]
    assert any("entry_spread_too_wide" in r for r in reasons)
