from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.loops import (
    position_exit_loop,
    reconciliation_loop,
    runtime_risk_loop,
    snapshot_loop,
    strategy_timer_loop,
)
from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.risk import RiskAction, RiskCheckResult, RiskDecision
from models.signal import SignalSide, TradeSignal
from state.store import InMemoryStateStore


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.50"),
        mid_price=Decimal("0.495"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=NOW,
        received_ts=NOW,
    )


def make_services(
    *,
    mode: Mode = Mode.DRY_RUN,
    reconciliation: object | None = None,
    runtime_risk: object | None = None,
) -> SimpleNamespace:
    if reconciliation is None:
        class Reconciliation:
            async def reconcile_runtime(self) -> SimpleNamespace:
                return SimpleNamespace(ok=True, deferred_positions=[])

        reconciliation = Reconciliation()

    if runtime_risk is None:
        class RuntimeRisk:
            async def evaluate_runtime(self) -> RiskDecision:
                return RiskDecision(
                    action=RiskAction.APPROVE,
                    approved=True,
                    checks=[],
                    reason="runtime_checks_passed",
                )

        runtime_risk = RuntimeRisk()

    incidents: list[object] = []

    async def report(incident: object) -> str:
        incidents.append(incident)
        return "retry"

    return SimpleNamespace(
        config=AppConfig(bot={"mode": mode}),
        state_store=InMemoryStateStore(mode=mode),
        reconciliation=reconciliation,
        runtime_risk=runtime_risk,
        exit_manager=None,
        strategy=None,
        router=None,
        snapshots=None,
        report=report,
        incidents=incidents,
    )


async def run_one_cycle(loop_factory: object, services: SimpleNamespace) -> None:
    stop_event = asyncio.Event()

    async def heartbeat() -> None:
        return None

    task = asyncio.create_task(
        loop_factory(services, stop_event, heartbeat, services.report)
    )
    await asyncio.sleep(0.02)
    stop_event.set()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconciliation_loop_reports_authoritative_incident_on_failure() -> None:
    class Failing:
        async def reconcile_runtime(self) -> object:
            raise RuntimeError("data api down")

    services = make_services(reconciliation=Failing())
    await run_one_cycle(reconciliation_loop, services)

    assert len(services.incidents) >= 1
    incident = services.incidents[0]
    assert incident.category.value == "authoritative_state"


@pytest.mark.asyncio
async def test_reconciliation_loop_heartbeats_after_success() -> None:
    beats: list[bool] = []

    class Ok:
        async def reconcile_runtime(self) -> SimpleNamespace:
            return SimpleNamespace(ok=True, deferred_positions=[])

    services = make_services(reconciliation=Ok())

    async def heartbeat() -> None:
        beats.append(True)

    stop_event = asyncio.Event()
    task = asyncio.create_task(
        reconciliation_loop(services, stop_event, heartbeat, services.report)
    )
    await asyncio.sleep(0.02)
    stop_event.set()
    await asyncio.gather(task, return_exceptions=True)
    assert beats


@pytest.mark.asyncio
async def test_successful_cycle_constructs_heartbeat_once() -> None:
    calls = 0
    services = make_services()
    stop_event = asyncio.Event()

    def heartbeat() -> Awaitable[None]:
        nonlocal calls
        calls += 1
        return asyncio.sleep(0)

    task = asyncio.create_task(
        reconciliation_loop(services, stop_event, heartbeat, services.report)
    )
    await asyncio.sleep(0.02)
    stop_event.set()
    await task

    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_risk_loop_reports_typed_safety_category() -> None:
    class HaltRisk:
        async def evaluate_runtime(self) -> RiskDecision:
            check = RiskCheckResult(
                check_name="daily_loss",
                passed=False,
                reason="daily_loss_limit:-9>1",
            )
            return RiskDecision(
                action=RiskAction.HALT,
                approved=False,
                checks=[check],
                reason="daily_loss_limit",
            )

    services = make_services(runtime_risk=HaltRisk())
    await run_one_cycle(runtime_risk_loop, services)

    assert len(services.incidents) >= 1
    assert services.incidents[0].category.value == "accounting"


@pytest.mark.asyncio
async def test_position_exit_loop_routes_only_exit_signals() -> None:
    routed: list[object] = []

    class ExitManager:
        async def on_timer(self, *, market_end_lookup: object) -> list[object]:
            routed.append("exit")
            return [
                TradeSignal(
                    strategy_name="position_exit",
                    market_id="m1", token_id="t1", side=SignalSide.SELL,
                    reference_price=Decimal("0.4"), target_price=Decimal("0.5"),
                    observed_move_bps=0, reason="position_exit:max_hold",
                )
            ]

    class Router:
        async def route_signal(self, signal: object, **_: object) -> None:
            routed.append(("route", signal.strategy_name))

    services = make_services()
    services.exit_manager = ExitManager()
    services.router = Router()

    async def market_end_lookup(market_id: str) -> None:
        return None

    services.market_end_lookup = market_end_lookup
    await run_one_cycle(position_exit_loop, services)

    assert routed == ["exit", ("route", "position_exit")]


@pytest.mark.asyncio
async def test_strategy_timer_loop_routes_timer_signals() -> None:
    routed: list[object] = []

    class Strategy:
        async def on_timer(self) -> list[object]:
            return [SimpleNamespace(reason="timer")]

    class Router:
        async def route_signal(self, signal: object, **_: object) -> None:
            routed.append(signal)

    services = make_services()
    services.strategy = Strategy()
    services.router = Router()
    await run_one_cycle(strategy_timer_loop, services)
    assert len(routed) == 1


@pytest.mark.asyncio
async def test_snapshot_loop_persists_without_other_work() -> None:
    saved: list[bool] = []
    other: list[str] = []

    class Snapshots:
        async def save_from_state(self, state: object) -> None:
            saved.append(True)

    class Reconciliation:
        async def reconcile_runtime(self) -> object:
            other.append("reconcile")

    services = make_services(reconciliation=Reconciliation())
    services.snapshots = Snapshots()
    await run_one_cycle(snapshot_loop, services)

    assert saved == [True]
    assert other == []


@pytest.mark.asyncio
async def test_snapshot_loop_runs_startup_retention_with_active_markets() -> None:
    calls: list[set[tuple[str, str]]] = []

    class Rotator:
        def status(self) -> object:
            market = SimpleNamespace(
                condition_id="cond-1",
                market_id="mkt-1",
                asset_ids=["tok-a", "tok-b"],
            )
            return SimpleNamespace(current_market=market)

    class Retention:
        def set_reporter(self, reporter: object) -> None:
            pass

        async def run_once(
            self,
            *,
            state_store: object,
            active_market_keys: set[tuple[str, str]],
            now: object,
        ) -> object:
            calls.append(set(active_market_keys))
            return SimpleNamespace()

    services = make_services()
    services.market_rotator = Rotator()
    services.retention_manager = Retention()
    await run_one_cycle(snapshot_loop, services)

    assert len(calls) == 1
    assert ("cond-1", "tok-a") in calls[0]
    assert ("mkt-1", "tok-b") in calls[0]


@pytest.mark.asyncio
async def test_snapshot_loop_reports_persistence_incident_on_retention_failure() -> (
    None
):
    class FailingRetention:
        def set_reporter(self, reporter: object) -> None:
            pass

        async def run_once(self, **kwargs: object) -> object:
            raise RuntimeError("retention exploded")

    services = make_services()
    services.retention_manager = FailingRetention()
    await run_one_cycle(snapshot_loop, services)

    categories = [incident.category.value for incident in services.incidents]
    assert "persistence" in categories
    reasons = [incident.reason for incident in services.incidents]
    assert any("RuntimeError" in reason for reason in reasons)


@pytest.mark.asyncio
async def test_notification_delivery_failure_does_not_kill_others() -> None:
    from app.loops import notification_delivery_loop

    attempts: list[int] = []

    class ExplodingWorker:
        async def deliver_due_once(self) -> int:
            attempts.append(1)
            raise RuntimeError("telegram down")

    services = make_services()
    services.notification_worker = ExplodingWorker()
    await run_one_cycle(notification_delivery_loop, services)

    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_market_rotation_loop_spec_advances_its_heartbeat() -> None:
    """
    A supervised task that never heartbeats gets the whole runtime halted.

    The rotation loop spends most of its life idle -- waiting for a market
    window to end -- so silence is its normal state, not a fault signal.
    """

    from app.runtime import BotRuntime

    beats: list[int] = []

    class IdleRotator:
        """Rotation with nothing to do yet -- returns rather than blocking."""

        def status(self) -> SimpleNamespace:
            return SimpleNamespace(current_market=None)

        async def run(self, stop_event: asyncio.Event) -> None:
            return None

        def mark_failed(self, reason: str) -> None:
            return None

    rotator = IdleRotator()
    services = SimpleNamespace(
        config=SimpleNamespace(
            bot=SimpleNamespace(
                housekeeping_interval_seconds=0.01,
                snapshot_interval_seconds=0.01,
            ),
            reliability=SimpleNamespace(
                authoritative_state_halt_after_seconds=300.0,
                retention_interval_seconds=3600.0,
            ),
        ),
        market_rotator=rotator,
    )
    runtime = BotRuntime()
    specs = runtime._default_loop_specs(services)

    rotation = next(s for s in specs if s.name == "market-rotation-loop")
    assert rotation.heartbeat_timeout_seconds > 0

    stop = asyncio.Event()

    async def heartbeat() -> None:
        beats.append(1)
        if len(beats) >= 2:
            stop.set()

    task = asyncio.create_task(rotation.factory(stop, heartbeat))
    try:
        await asyncio.wait_for(stop.wait(), timeout=5)
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    assert len(beats) >= 2


@pytest.mark.asyncio
async def test_every_supervised_spec_declares_a_heartbeat_timeout() -> None:
    from app.runtime import BotRuntime

    services = SimpleNamespace(
        config=SimpleNamespace(
            bot=SimpleNamespace(
                housekeeping_interval_seconds=1,
                snapshot_interval_seconds=1,
            ),
            reliability=SimpleNamespace(
                authoritative_state_halt_after_seconds=300.0,
                retention_interval_seconds=3600.0,
            ),
        ),
        market_rotator=SimpleNamespace(
            status=lambda: SimpleNamespace(current_market=None)
        ),
    )
    specs = BotRuntime()._default_loop_specs(services)

    assert specs
    for spec in specs:
        assert spec.heartbeat_timeout_seconds >= 30, spec.name
