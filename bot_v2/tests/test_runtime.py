from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.bootstrap import LivePreflightError
from app.runtime import (
    BotRuntime,
    FatalRuntimeError,
    RuntimePhase,
    market_rotation_loop,
)
from app.shutdown import shutdown_app
from config.schema import AppConfig, Mode
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    LeaseStatus,
    LiveOperatingLease,
    OperationalIncident,
    OperationalState,
)
from persistence.operations import OperationsRepository
from models.risk import RiskAction, RiskCheckResult, RiskDecision
from risk.circuit_breaker import CircuitBreaker
from risk.runtime import RuntimeRiskEngine
from state.store import InMemoryStateStore


class FakeReconciliation:
    async def reconcile_startup(self) -> SimpleNamespace:
        return SimpleNamespace(ok=True, model_dump_json=lambda: "{}")


class FakeRuntimeReconciliation:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile_runtime(self) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(ok=True, deferred_positions=[])


class FakeWebSocketManager:
    def __init__(self) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True


class FakeSubmitter:
    def __init__(self, calls: list[str], *, fail: bool = False) -> None:
        self._calls = calls
        self._fail = fail

    async def cancel_all_open_orders(self) -> bool:
        self._calls.append("cancel_all")
        if self._fail:
            raise RuntimeError("cancel failed")
        return True


class FakeMarketRotator:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.heartbeat: object = None

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        heartbeat: object = None,
        heartbeat_interval_seconds: float = 15.0,
    ) -> None:
        # Rotation blocks for most of a market window, so the supervisor's
        # heartbeat has to be threaded in rather than sent after run() returns.
        self.heartbeat = heartbeat
        self.calls.append("rotation_run")
        if heartbeat is not None:
            self.calls.append("rotation_heartbeat_wired")
            await heartbeat()
        await stop_event.wait()

    async def stop(self) -> None:
        self.calls.append("rotation_stop")

    def mark_failed(self, reason: str) -> None:
        self.calls.append(f"rotation_failed:{reason}")

    def status(self) -> SimpleNamespace:
        return SimpleNamespace(current_market=None)


def fake_services(
    mode: Mode,
    calls: list[str],
    *,
    cancel_fails: bool = False,
    market_rotator: FakeMarketRotator | None = None,
) -> SimpleNamespace:
    config = AppConfig(
        bot={"mode": mode},
        execution={
            "allow_live_trading": mode == Mode.LIVE,
            "dry_run_force": mode != Mode.LIVE,
        },
    )
    return SimpleNamespace(
        config=config,
        state_store=InMemoryStateStore(mode=mode),
        reconciliation=FakeReconciliation(),
        ws_manager=FakeWebSocketManager(),
        submitter=FakeSubmitter(calls, fail=cancel_fails),
        market_rotator=market_rotator,
    )


async def idle_loop(services: object, stop_event: asyncio.Event) -> None:
    await stop_event.wait()


NOOP_LOOPS: dict[str, Any] = {}


def _noop_loops() -> dict[str, Any]:
    from typing import Any

    async def noop(services: object, stop_event: asyncio.Event) -> None:
        await stop_event.wait()

    return {
        "reconciliation-loop": noop,
        "runtime-risk-loop": noop,
        "position-exit-loop": noop,
        "strategy-timer-loop": noop,
        "snapshot-retention-loop": noop,
        "notification-delivery-loop": noop,
    }


def make_runtime(
    services: object,
    calls: list[str],
    **kwargs: object,
) -> BotRuntime:
    defaults: dict[str, object] = {
        "loop_factories": _noop_loops(),
        "event_emitter": lambda _services, _event: asyncio.sleep(0),
    }
    defaults.update(kwargs)
    return BotRuntime(**defaults)


@pytest.mark.asyncio
async def test_runtime_starts_and_stops_dry_run() -> None:
    calls: list[str] = []
    services = fake_services(Mode.DRY_RUN, calls)

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        calls.append("bootstrap")
        return services

    async def shutdown(current: object, tasks: list[asyncio.Task[object]]) -> None:
        calls.append("shutdown")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        shutdown=shutdown,
        config_loader=lambda _: services.config,
    )

    assert (await runtime.start()).phase == RuntimePhase.RUNNING
    assert services.ws_manager.started is True
    assert (await runtime.start()).phase == RuntimePhase.RUNNING
    assert calls.count("bootstrap") == 1

    assert (await runtime.stop()).phase == RuntimePhase.STOPPED
    assert (await runtime.stop()).phase == RuntimePhase.STOPPED
    assert calls.count("shutdown") == 1


@pytest.mark.asyncio
async def test_runtime_forwards_process_services_to_bootstrap() -> None:
    calls: list[str] = []
    services = fake_services(Mode.DRY_RUN, calls)
    process_services = object()

    async def bootstrap(
        config_dir: object = None,
        *,
        process_services: object,
    ) -> SimpleNamespace:
        calls.append("bootstrap")
        assert process_services is expected_process_services
        return services

    async def shutdown(
        current: object, tasks: list[asyncio.Task[object]]
    ) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    expected_process_services = process_services
    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        shutdown=shutdown,
        config_loader=lambda _: services.config,
        process_services=process_services,
    )

    assert (await runtime.start()).phase == RuntimePhase.RUNNING
    assert calls == ["bootstrap"]
    await runtime.stop()


@pytest.mark.asyncio
async def test_default_health_loop_receives_stop_event_not_services() -> None:
    services = SimpleNamespace(
        config=AppConfig(),
        health_store=object(),
        market_rotator=None,
    )
    runtime = BotRuntime()
    spec = next(
        item
        for item in runtime._default_loop_specs(services)
        if item.name == "health-report-loop"
    )
    stop_event = asyncio.Event()
    stop_event.set()

    await spec.factory(stop_event, lambda: asyncio.sleep(0))


def test_process_owned_notifications_are_not_duplicated_by_runtime() -> None:
    services = SimpleNamespace(
        config=AppConfig(),
        health_store=object(),
        market_rotator=None,
    )
    runtime = BotRuntime(process_services=object())

    names = {spec.name for spec in runtime._default_loop_specs(services)}

    assert "notification-delivery-loop" not in names


@pytest.mark.asyncio
async def test_runtime_wait_returns_failed_after_supervisor_budget_exhaustion() -> None:
    calls: list[str] = []
    services = fake_services(Mode.DRY_RUN, calls)
    services.config = services.config.model_copy(
        update={
            "reliability": services.config.reliability.model_copy(
                update={
                    "task_restart_limit": 1,
                    "retry_initial_seconds": 0.001,
                    "retry_max_seconds": 0.001,
                    "retry_jitter_ratio": 0,
                }
            )
        }
    )

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        return services

    async def crashing(_services: object, _stop_event: asyncio.Event) -> None:
        raise RuntimeError("sensitive detail")

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        config_loader=lambda _: services.config,
        loop_factories={"critical-loop": crashing},
    )

    async def retry_incident(_incident: object) -> str:
        return "retry"

    runtime._supervised_incident_handler = retry_incident

    assert (await runtime.start()).phase == RuntimePhase.RUNNING
    status = await asyncio.wait_for(runtime.wait(), timeout=1)

    assert status.phase == RuntimePhase.FAILED
    assert status.reason == "task_crash:RuntimeError"
    await runtime.stop()


@pytest.mark.asyncio
async def test_accounting_incident_persists_and_completes_durable_live_halt(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls)
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    await repository.create_lease(
        LiveOperatingLease(
            lease_id="lease-12345678",
            issued_at=datetime(2026, 8, 24, tzinfo=UTC),
            expires_at=datetime(2026, 8, 27, tzinfo=UTC),
            config_fingerprint="a" * 64,
            status=LeaseStatus.ACTIVE,
        )
    )

    class Snapshots:
        async def save_from_state(self, state_store: object) -> None:
            calls.append("snapshot")

    class Alerts:
        async def enqueue_incident(self, incident: OperationalIncident) -> None:
            calls.append(f"alert:{incident.incident_id}")

    services.operations_repository = repository
    services.alert_service = Alerts()
    services.snapshots = Snapshots()
    runtime = make_runtime(services, calls, config_loader=lambda _: services.config)
    runtime._services = services
    runtime._phase = RuntimePhase.RUNNING
    incident = OperationalIncident(
        incident_id="incident-accounting-0001",
        fingerprint="runtime_risk:accounting:invariant",
        component="runtime_risk",
        category=IncidentCategory.ACCOUNTING,
        severity=IncidentSeverity.URGENT,
        reason="accounting_invariant",
        first_seen_at=datetime(2026, 8, 24, tzinfo=UTC),
        last_seen_at=datetime(2026, 8, 24, tzinfo=UTC),
    )

    action = await runtime.handle_incident(incident)

    assert action == "halt"
    assert runtime.status().phase == RuntimePhase.HALTED
    assert await services.state_store.is_kill_switch_active() is True
    assert await repository.get_active_lease() is None
    stored = await repository.get_incident("incident-accounting-0001")
    assert stored is not None and stored.reason == "accounting_invariant"
    assert calls == [
        "snapshot",
        "alert:incident-accounting-0001",
        "cancel_all",
    ]


@pytest.mark.asyncio
async def test_runtime_owns_named_market_rotation_task() -> None:
    calls: list[str] = []
    rotator = FakeMarketRotator(calls)
    services = fake_services(Mode.DRY_RUN, calls, market_rotator=rotator)
    services.snapshots = SimpleNamespace(save_from_state=lambda s: asyncio.sleep(0))

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        return services

    async def shutdown(current: object, tasks: list[asyncio.Task[object]]) -> None:
        names = {task.get_name() for task in tasks}
        assert "market-rotation" in names or calls.count("rotation_run") >= 1
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        shutdown=shutdown,
        config_loader=lambda _: services.config,
    )

    assert (await runtime.start()).phase == RuntimePhase.RUNNING
    await asyncio.sleep(0.05)
    assert "rotation_run" in calls
    # Regression: rotation ran but never beat, so the watchdog halted a healthy
    # runtime after its timeout elapsed.
    assert "rotation_heartbeat_wired" in calls
    await runtime.stop()


@pytest.mark.asyncio
async def test_shutdown_stops_rotator_before_websocket() -> None:
    calls: list[str] = []

    class Snapshots:
        async def save_from_state(self, state_store: object) -> None:
            calls.append("snapshot")

    class Websocket:
        async def stop(self) -> None:
            calls.append("websocket_stop")

    services = fake_services(
        Mode.DRY_RUN,
        calls,
        market_rotator=FakeMarketRotator(calls),
    )
    services.snapshots = Snapshots()
    services.ws_manager = Websocket()

    await shutdown_app(services, [])

    assert calls == ["snapshot", "rotation_stop", "websocket_stop"]


@pytest.mark.asyncio
async def test_shutdown_redacts_cancel_all_failure_reason() -> None:
    calls: list[str] = []
    persisted: list[object] = []

    class Journal:
        async def append(self, event: object) -> None:
            persisted.append(event)

    class Snapshots:
        async def save_from_state(self, state_store: object) -> None:
            calls.append("snapshot")

    class Websocket:
        async def stop(self) -> None:
            calls.append("websocket_stop")

    services = fake_services(Mode.LIVE, calls, cancel_fails=True)
    services.journal = Journal()
    services.snapshots = Snapshots()
    services.ws_manager = Websocket()

    await shutdown_app(services, [])

    assert calls == ["cancel_all", "snapshot", "websocket_stop"]
    assert len(persisted) == 1
    assert persisted[0].reason == "cancel_all_failed:RuntimeError"


@pytest.mark.asyncio
async def test_unexpected_rotation_failure_reports_incident_not_kill_switch() -> None:
    calls: list[str] = []

    class FailingRotator:
        def __init__(self) -> None:
            self.failed_reason: str | None = None

        async def run(self, stop_event: asyncio.Event) -> None:
            raise RuntimeError("remote secret")

        def mark_failed(self, reason: str) -> None:
            self.failed_reason = reason

    class Journal:
        async def append(self, event: object) -> None:
            pass

    class EventBus:
        async def publish(self, event: object) -> None:
            return None

    rotator = FailingRotator()
    services = fake_services(Mode.DRY_RUN, calls)
    services.market_rotator = rotator
    services.journal = Journal()
    services.event_bus = EventBus()

    incident = await market_rotation_loop(services, asyncio.Event())

    assert incident is not None
    assert incident.reason == "RuntimeError"
    assert rotator.failed_reason == "RuntimeError"
    assert await services.state_store.is_kill_switch_active() is False


@pytest.mark.asyncio
async def test_runtime_cleans_up_services_when_startup_fails() -> None:
    calls: list[str] = []
    services = fake_services(Mode.DRY_RUN, calls)

    async def failing_start() -> None:
        raise RuntimeError("websocket failed")

    services.ws_manager.start = failing_start

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        calls.append("bootstrap")
        return services

    async def shutdown(current: object, tasks: list[asyncio.Task[object]]) -> None:
        assert current is services
        assert tasks == []
        calls.append("shutdown")

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        shutdown=shutdown,
        config_loader=lambda _: services.config,
    )

    status = await runtime.start()

    assert status.phase == RuntimePhase.FAILED
    assert status.reason == "RuntimeError"
    assert calls == ["bootstrap", "shutdown"]
    assert runtime.services is None


@pytest.mark.asyncio
async def test_runtime_preserves_safe_live_preflight_check_names() -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls)

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        raise LivePreflightError(("collateral_sufficient", "geoblock_allowed"))

    runtime = make_runtime(services, calls, bootstrap=bootstrap, config_loader=lambda _: services.config)

    status = await runtime.start()

    assert status.phase == RuntimePhase.FAILED
    assert status.reason == "live_preflight_failed:collateral_sufficient,geoblock_allowed"


@pytest.mark.asyncio
async def test_runtime_reports_failed_when_shutdown_cleanup_fails() -> None:
    calls: list[str] = []
    services = fake_services(Mode.DRY_RUN, calls)

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        return services

    async def failing_shutdown(
        current: object, tasks: list[asyncio.Task[object]]
    ) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise RuntimeError("cleanup failed")

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        shutdown=failing_shutdown,
        config_loader=lambda _: services.config,
    )
    await runtime.start()

    status = await runtime.stop()

    assert status.phase == RuntimePhase.FAILED
    assert status.reason == "shutdown_failed"
    assert status.last_control_error == "shutdown_failed:RuntimeError"


@pytest.mark.asyncio
async def test_stop_request_during_startup_is_preserved() -> None:
    calls: list[str] = []
    services = fake_services(Mode.DRY_RUN, calls)
    bootstrap_entered = asyncio.Event()
    release_bootstrap = asyncio.Event()

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        bootstrap_entered.set()
        await release_bootstrap.wait()
        return services

    async def shutdown(current: object, tasks: list[asyncio.Task[object]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        shutdown=shutdown,
        config_loader=lambda _: services.config,
    )

    start_task = asyncio.create_task(runtime.start())
    await bootstrap_entered.wait()
    runtime.request_stop()
    release_bootstrap.set()

    assert (await start_task).phase == RuntimePhase.RUNNING
    await asyncio.wait_for(runtime.wait(), timeout=0.1)
    assert (await runtime.stop()).phase == RuntimePhase.STOPPED


@pytest.mark.asyncio
async def test_runtime_refuses_live_start_before_bootstrap() -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls)

    async def bootstrap(config_dir: object = None) -> SimpleNamespace:
        calls.append("bootstrap")
        return services

    runtime = make_runtime(
        services,
        calls,
        bootstrap=bootstrap,
        config_loader=lambda _: services.config,
    )

    status = await runtime.start(allow_live=False)

    assert status.phase == RuntimePhase.FAILED
    assert status.reason == "live_start_disabled_pending_review"
    assert calls == []


@pytest.mark.asyncio
async def test_emergency_halt_sets_kill_switch_before_cancel_all() -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls)
    original_set = services.state_store.set_kill_switch

    async def ordered_set(
        enabled: bool,
        *,
        reason: str | None = None,
    ) -> None:
        calls.append("kill_switch")
        await original_set(enabled, reason=reason)

    services.state_store.set_kill_switch = ordered_set
    runtime = make_runtime(services, calls, config_loader=lambda _: services.config)
    runtime._services = services
    runtime._phase = RuntimePhase.RUNNING

    status = await runtime.emergency_halt("HALT")

    assert status.phase == RuntimePhase.HALTED
    assert calls == ["kill_switch", "cancel_all"]
    assert await services.state_store.is_kill_switch_active() is True


@pytest.mark.asyncio
async def test_emergency_halt_remains_active_when_cancel_all_fails() -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls, cancel_fails=True)
    runtime = make_runtime(services, calls, config_loader=lambda _: services.config)
    runtime._services = services
    runtime._phase = RuntimePhase.RUNNING

    status = await runtime.emergency_halt("HALT")

    assert status.phase == RuntimePhase.HALTED
    assert status.last_control_error == "cancel_all_failed"
    assert await services.state_store.is_kill_switch_active() is True


@pytest.mark.asyncio
async def test_emergency_halt_persists_incident_and_revokes_live_lease(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls)
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    await repository.create_lease(
        LiveOperatingLease(
            lease_id="lease-emergency-0001",
            issued_at=datetime(2026, 8, 24, tzinfo=UTC),
            expires_at=datetime(2026, 8, 27, tzinfo=UTC),
            config_fingerprint="b" * 64,
            status=LeaseStatus.ACTIVE,
        )
    )

    class Snapshots:
        async def save_from_state(self, state_store: object) -> None:
            calls.append("snapshot")

    class Alerts:
        async def enqueue_incident(self, incident: OperationalIncident) -> None:
            calls.append("alert")

    services.operations_repository = repository
    services.snapshots = Snapshots()
    services.alert_service = Alerts()
    runtime = make_runtime(services, calls, config_loader=lambda _: services.config)
    runtime._services = services
    runtime._phase = RuntimePhase.RUNNING

    status = await runtime.emergency_halt("HALT")

    assert status.phase == RuntimePhase.HALTED
    assert await repository.get_active_lease() is None
    incidents = await repository.recent_incidents(limit=10)
    assert len(incidents) == 1
    assert incidents[0].reason == "operator_emergency_halt"
    assert calls == ["snapshot", "alert", "cancel_all"]


@pytest.mark.asyncio
async def test_destructive_controls_require_exact_confirmation() -> None:
    calls: list[str] = []
    services = fake_services(Mode.LIVE, calls)
    runtime = make_runtime(services, calls, config_loader=lambda _: services.config)
    runtime._services = services
    runtime._phase = RuntimePhase.RUNNING

    with pytest.raises(ValueError, match="HALT"):
        await runtime.emergency_halt("halt")
    with pytest.raises(ValueError, match="CANCEL ALL"):
        await runtime.cancel_all("cancel all")


@pytest.mark.asyncio
async def test_runtime_risk_allows_market_data_startup_grace() -> None:
    now = [100.0]
    config = AppConfig(market_data={"heartbeat_timeout_seconds": 5})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    engine = RuntimeRiskEngine(
        config=config,
        state_store=state,
        circuit_breaker=CircuitBreaker(
            failure_threshold=3,
            window_seconds=60,
            cooldown_seconds=60,
        ),
        monotonic=lambda: now[0],
    )

    initial = await engine.evaluate_runtime()
    now[0] = 106.0
    expired = await engine.evaluate_runtime()

    assert initial.approved is True
    assert any(check.reason == "heartbeat_startup_grace" for check in initial.checks)
    assert expired.approved is False
    assert any(
        check.reason == "transport_heartbeat_stale"
        for check in expired.checks
    )
