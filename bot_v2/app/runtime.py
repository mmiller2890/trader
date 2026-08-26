"""Controllable lifecycle for CLI and dashboard operation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.bootstrap import AppServices, LivePreflightError, bootstrap_app
from app.process_services import ProcessReliabilityServices
from app.shutdown import shutdown_app
from app.supervisor import RuntimeSupervisor, TaskSpec
from config.loader import load_config
from config.schema import AppConfig, Mode, ReliabilityConfig
from models.events import BotEvent, EventType
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
    OperationalState,
)
from models.risk import RiskCheckResult
from reliability.policy import FaultPolicy, RecoveryAction, RecoveryContext


logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


# Single source of truth for serialized runtime state values.
RuntimePhase = OperationalState


class RuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: RuntimePhase
    mode: Mode | None = None
    reason: str | None = None
    operational_reason: str | None = None
    degraded_since: datetime | None = None
    last_control_error: str | None = None


class ControlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    action: str
    reason: str


async def emit_event(services: AppServices, event: BotEvent) -> None:
    await services.journal.append(event)
    await services.event_bus.publish(event)


def _has_check(checks: list[RiskCheckResult], check_name: str) -> bool:
    return any(check.check_name == check_name and not check.passed for check in checks)


class FatalRuntimeError(RuntimeError):
    """Raised by headless runners after cleanup when the runtime FAILED."""


async def market_rotation_loop(
    services: AppServices,
    stop_event: asyncio.Event,
    heartbeat: Callable[[], Awaitable[None]] | None = None,
) -> OperationalIncident | None:
    """Supervise rotation; return an incident instead of halting directly."""

    rotator = services.market_rotator
    if rotator is None:
        return None
    try:
        if heartbeat is not None:
            await rotator.run(stop_event, heartbeat=heartbeat)
        else:
            await rotator.run(stop_event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = type(exc).__name__
        rotator.mark_failed(reason)
        from uuid import uuid4

        return OperationalIncident(
            incident_id=f"inc-{uuid4().hex}",
            fingerprint=f"market_rotation_failed:{reason}",
            component="market_rotation",
            category=IncidentCategory.MARKET_DISCOVERY,
            severity=IncidentSeverity.URGENT,
            reason=reason,
            first_seen_at=utc_now(),
            last_seen_at=utc_now(),
        )
    return None


Bootstrap = Callable[..., Awaitable[Any]]
Shutdown = Callable[[Any, list[asyncio.Task[object]]], Awaitable[None]]
ConfigLoader = Callable[[str | Path | None], AppConfig]
EventEmitter = Callable[[Any, BotEvent], Awaitable[None]]
LoopFactory = Callable[[AppServices, asyncio.Event], Awaitable[None]]
SupervisedLoopFactory = Callable[
    [AppServices, asyncio.Event, Callable[[], Awaitable[None]]],
    Awaitable[None],
]


class BotRuntime:
    """Own one bot service graph and serialize lifecycle transitions."""

    def __init__(
        self,
        *,
        bootstrap: Bootstrap = bootstrap_app,
        shutdown: Shutdown = shutdown_app,
        config_loader: ConfigLoader = load_config,
        event_emitter: EventEmitter = emit_event,
        loop_factories: dict[str, LoopFactory] | None = None,
        process_services: ProcessReliabilityServices | None = None,
    ) -> None:
        self._bootstrap = bootstrap
        self._shutdown = shutdown
        self._config_loader = config_loader
        self._event_emitter = event_emitter
        self._loop_factories = loop_factories
        self._process_services = process_services
        self._lock = asyncio.Lock()
        self._phase = RuntimePhase.STOPPED
        self._services: Any | None = None
        self._tasks: list[asyncio.Task[object]] = []
        self._stop_event = asyncio.Event()
        self._terminal_event = asyncio.Event()
        self._stop_requested = False
        self._mode: Mode | None = None
        self._reason: str | None = None
        self._operational_reason: str | None = None
        self._degraded_since: datetime | None = None
        self._last_control_error: str | None = None
        self._supervisor: RuntimeSupervisor | None = None
        self._supervisor_monitor: asyncio.Task[None] | None = None
        self._incident_first_seen: dict[str, datetime] = {}
        self._incident_counts: dict[str, int] = {}

    @property
    def services(self) -> Any | None:
        return self._services

    @property
    def is_running(self) -> bool:
        return self._phase in {RuntimePhase.RUNNING, RuntimePhase.HALTED}

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            phase=self._phase,
            mode=self._mode,
            reason=self._reason,
            operational_reason=self._operational_reason,
            degraded_since=self._degraded_since,
            last_control_error=self._last_control_error,
        )


    async def _alert_best_effort(self, alert_service: object, incident: object) -> None:
        """
        Send an incident alert without letting it affect control flow.

        handle_incident awaits this between latching the kill switch and
        cancelling open orders. An exception here -- a duplicate outbox row, a
        locked database -- used to abort the halt before the cancel-all, which
        left live orders resting during a safety halt. Alerting is never
        allowed to outrank the safety ordering.
        """

        if alert_service is None:
            return
        try:
            await alert_service.enqueue_incident(incident)
        except Exception:
            logger.critical(
                "failed to enqueue incident alert; continuing safety ordering",
                extra={
                    "component": "runtime",
                    "event_type": "incident_alert_enqueue_failed",
                    "reason": getattr(incident, "reason", None),
                },
            )

    async def handle_incident(
        self,
        incident: OperationalIncident,
        *,
        force_action: str | None = None,
        terminal_phase: RuntimePhase = RuntimePhase.HALTED,
    ) -> str:
        """Centralized halt/degrade ordering for every typed incident."""

        services = self._services
        stored_incident = incident
        repository = getattr(services, "operations_repository", None)
        if repository is not None:
            stored_incident = await repository.record_incident(incident)
        self._incident_first_seen.setdefault(
            stored_incident.fingerprint, stored_incident.first_seen_at
        )
        self._incident_counts[stored_incident.fingerprint] = max(
            stored_incident.consecutive_count,
            self._incident_counts.get(stored_incident.fingerprint, 0) + 1,
        )
        alert_service = getattr(services, "alert_service", None)
        policy = FaultPolicy(
            getattr(services.config, "reliability", ReliabilityConfig())
            if services is not None
            else ReliabilityConfig()
        )
        context = await self._build_recovery_context(stored_incident)
        action = force_action or policy.decide(stored_incident, context)

        if action == RecoveryAction.RETRY:
            await self._alert_best_effort(alert_service, stored_incident)
            return action

        if action == RecoveryAction.DEGRADE:
            if self._degraded_since is None:
                self._degraded_since = utc_now()
            if services is not None:
                await services.state_store.set_operational_state(
                    RuntimePhase.DEGRADED, reason=stored_incident.reason
                )
            self._operational_reason = stored_incident.reason
            await self._emit_degraded_event(
                services, stored_incident, recovered=False
            )
            await self._alert_best_effort(alert_service, stored_incident)
            return action

        # HALT — centralized safety ordering.
        self._phase = RuntimePhase.HALTING
        if services is not None:
            if repository is not None:
                await repository.revoke_active_lease(
                    reason=stored_incident.reason,
                    revoked_at=utc_now(),
                )
            activated = await services.state_store.activate_kill_switch(
                stored_incident.reason
            )
            snapshots = getattr(services, "snapshots", None)
            if snapshots is not None:
                try:
                    await snapshots.save_from_state(services.state_store)
                except Exception:
                    logger.critical(
                        "failed to persist kill switch snapshot",
                        extra={
                            "component": "runtime",
                            "reason": stored_incident.reason,
                        },
                    )
            if activated:
                await self._event_emitter(
                    services,
                    BotEvent(
                        event_type=EventType.KILL_SWITCH_TRIPPED,
                        component="runtime",
                        mode=services.config.bot.mode.value,
                        message="safety halt latched kill switch",
                        reason=stored_incident.reason,
                    ),
                )
            await self._alert_best_effort(alert_service, stored_incident)
            if services.config.bot.mode == Mode.LIVE:
                try:
                    await asyncio.wait_for(
                        services.submitter.cancel_all_open_orders(),
                        timeout=services.config.bot.shutdown_timeout_seconds,
                    )
                    self._last_control_error = None
                except Exception as exc:
                    self._last_control_error = "cancel_all_failed"
                    logger.critical(
                        "cancel-all failed during safety halt",
                        extra={
                            "component": "runtime",
                            "reason": f"cancel_all_failed:{type(exc).__name__}",
                        },
                    )
            self._forget_resting_quotes(services)
        self._phase = terminal_phase
        self._reason = stored_incident.reason
        self._terminal_event.set()
        return action

    @staticmethod
    def _forget_resting_quotes(services: object) -> None:
        """
        Drop locally tracked quotes after a halt cancelled them.

        Cancel-all removed the orders at the exchange, so anything the quoting
        strategy still believes is resting is stale. Reconciliation remains the
        authority on what actually survived.
        """

        market_maker = getattr(services, "market_maker", None)
        if market_maker is None:
            return
        for quote in market_maker.resting_quotes():
            market_maker.forget_quote(quote.client_order_id)

    async def _build_recovery_context(self, incident: OperationalIncident) -> RecoveryContext:
        services = self._services
        flat = True
        if services is not None:
            positions = await services.state_store.get_positions()
            flat = all(position.quantity <= 0 for position in positions)
        count = self._incident_counts.get(
            incident.fingerprint, incident.consecutive_count
        )
        first_seen = self._incident_first_seen.get(
            incident.fingerprint, incident.first_seen_at
        )
        unavailable_seconds = max(
            0.0, (utc_now() - first_seen).total_seconds()
        )
        disk_percent = 0.0
        data_dir = getattr(services, "data_dir", None)
        if data_dir is not None:
            import shutil

            total, _used, free = shutil.disk_usage(data_dir)
            if total > 0:
                disk_percent = (total - free) / total * 100.0
        return RecoveryContext(
            flat=flat,
            authoritative_unavailable_seconds=unavailable_seconds,
            repeated_authoritative_confirmations=count,
            task_crashes_in_window=count,
            disk_percent=disk_percent,
            required_for_safe_exit=not flat,
        )

    async def _emit_degraded_event(
        self, services: Any | None, incident: OperationalIncident, *, recovered: bool
    ) -> None:
        if services is None:
            return
        event_type = (
            EventType.RUNTIME_RECOVERED if recovered else EventType.RUNTIME_DEGRADED
        )
        try:
            await self._event_emitter(
                services,
                BotEvent(
                    event_type=event_type,
                    component="runtime",
                    mode=services.config.bot.mode.value,
                    message=(
                        "runtime recovered to running"
                        if recovered
                        else "runtime degraded; entries paused"
                    ),
                    reason=incident.reason,
                ),
            )
        except Exception:
            logger.warning("degraded event emit failed", exc_info=True)

    def _make_reporter(self) -> Callable[[OperationalIncident], Awaitable[str]]:
        async def report(incident: OperationalIncident) -> str:
            return await self.handle_incident(incident)

        return report

    def _default_loop_specs(self, services: Any) -> list[TaskSpec]:
        from app.loops import (
            health_report_loop,
            notification_delivery_loop,
            position_exit_loop,
            reconciliation_loop,
            runtime_risk_loop,
            snapshot_loop,
            strategy_timer_loop,
        )
        from uuid import uuid4

        reporter = self._make_reporter()
        reliability = getattr(getattr(services, "config", None), "reliability", None)
        heartbeat_timeout = max(
            30.0,
            float(
                getattr(
                    reliability,
                    "authoritative_state_halt_after_seconds",
                    300.0,
                )
            ),
        )
        factories: dict[str, SupervisedLoopFactory] = {
            "reconciliation-loop": lambda svc, stop, heartbeat: reconciliation_loop(
                svc, stop, heartbeat, reporter
            ),
            "runtime-risk-loop": lambda svc, stop, heartbeat: runtime_risk_loop(
                svc, stop, heartbeat, reporter
            ),
            "position-exit-loop": lambda svc, stop, heartbeat: position_exit_loop(
                svc, stop, heartbeat, reporter
            ),
            "strategy-timer-loop": lambda svc, stop, heartbeat: strategy_timer_loop(
                svc, stop, heartbeat, reporter
            ),
            "snapshot-retention-loop": lambda svc, stop, heartbeat: snapshot_loop(
                svc, stop, heartbeat, reporter
            ),
            "notification-delivery-loop": lambda svc, stop, heartbeat: notification_delivery_loop(
                svc, stop, heartbeat, reporter
            ),
        }
        if self._process_services is not None:
            factories.pop("notification-delivery-loop")

        async def health_report_with_runtime(
            _services: AppServices,
            stop_event: asyncio.Event,
            heartbeat: Callable[[], Awaitable[None]],
        ) -> None:
            await health_report_loop(
                services,
                stop_event,
                heartbeat,
                reporter,
                runtime=self,
            )

        factories["health-report-loop"] = health_report_with_runtime
        if self._loop_factories is not None:
            specs = [
                TaskSpec(
                    name=name,
                    factory=lambda stop, heartbeat, _f=factory, _s=services: _f(
                        _s, stop
                    ),
                    heartbeat_timeout_seconds=heartbeat_timeout,
                )
                for name, factory in self._loop_factories.items()
                if not (
                    self._process_services is not None
                    and name == "notification-delivery-loop"
                )
            ]
        else:
            specs = []
            for name, factory in factories.items():
                specs.append(
                    TaskSpec(
                        name=name,
                        factory=lambda stop, heartbeat, _f=factory, _s=services: _f(
                            _s, stop, heartbeat
                        ),
                        heartbeat_timeout_seconds=heartbeat_timeout,
                    )
                )
        rotator = getattr(services, "market_rotator", None)
        if rotator is not None:
            async def rotation_with_incident_handling(
                stop_event: asyncio.Event, heartbeat: Any
            ) -> None:
                while not stop_event.is_set():
                    # The heartbeat is threaded *into* rotation rather than
                    # sent after it returns: run() blocks for most of a market
                    # window, so a beat afterwards can be many minutes apart
                    # and the watchdog halts a healthy runtime first.
                    incident = await market_rotation_loop(
                        services, stop_event, heartbeat
                    )
                    if incident is not None:
                        await self.handle_incident(incident)
                        if self._phase in (RuntimePhase.HALTED, RuntimePhase.FAILED):
                            return
                    await heartbeat()
                    await asyncio.sleep(0.5)

            specs.append(
                TaskSpec(
                    name="market-rotation-loop",
                    factory=rotation_with_incident_handling,
                    restartable=False,
                    heartbeat_timeout_seconds=heartbeat_timeout,
                )
            )
        return specs

    async def _supervised_incident_handler(
        self, incident: OperationalIncident
    ) -> str:
        return await self.handle_incident(incident)

    async def _monitor_supervisor(self, supervisor: RuntimeSupervisor) -> None:
        """Turn an exhausted supervision budget into a terminal runtime state."""

        try:
            incident = await supervisor.wait_fatal()
        except asyncio.CancelledError:
            raise
        async with self._lock:
            if self._supervisor is not supervisor or self._phase in {
                RuntimePhase.STOPPED,
                RuntimePhase.STOPPING,
            }:
                return
            await self.handle_incident(
                incident,
                force_action=RecoveryAction.HALT,
                terminal_phase=RuntimePhase.FAILED,
            )

    async def start(
        self,
        config_dir: str | Path | None = None,
        *,
        allow_live: bool = True,
    ) -> RuntimeStatus:
        async with self._lock:
            if self.is_running:
                return self.status()
            self._phase = RuntimePhase.STARTING
            self._reason = None
            self._last_control_error = None
            self._terminal_event.clear()
            self._stop_event = asyncio.Event()
            if self._stop_requested:
                self._stop_event.set()
            services: Any | None = None
            supervisor: RuntimeSupervisor | None = None
            try:
                config = self._config_loader(config_dir)
                self._mode = config.bot.mode
                if config.bot.mode == Mode.LIVE and not allow_live:
                    self._phase = RuntimePhase.FAILED
                    self._reason = "live_start_disabled_pending_review"
                    return self.status()

                supervisor = RuntimeSupervisor(
                    config=getattr(config, "reliability", ReliabilityConfig()),
                    incident_handler=self._supervised_incident_handler,
                    backoff=_supervisor_backoff(config),
                )

                if self._process_services is None:
                    services = await self._bootstrap(config_dir)
                else:
                    services = await self._bootstrap(
                        config_dir,
                        process_services=self._process_services,
                    )
                report = await services.reconciliation.reconcile_startup()
                if services.config.bot.mode == Mode.LIVE and not report.ok:
                    raise RuntimeError("live_startup_reconciliation_failed")

                await services.state_store.update_heartbeat("app")
                await services.state_store.set_operational_state(
                    RuntimePhase.RUNNING, reason=None
                )
                await self._event_emitter(
                    services,
                    BotEvent(
                        event_type=EventType.BOT_STARTED,
                        component="app",
                        mode=services.config.bot.mode.value,
                        message="bot started",
                    ),
                )
                if services.config.bot.mode in {Mode.DRY_RUN, Mode.LIVE}:
                    await services.ws_manager.start()

                specs = self._default_loop_specs(services)
                self._services = services
                self._supervisor = supervisor
                self._tasks = []
                await supervisor.start(specs)
                self._supervisor_monitor = asyncio.create_task(
                    self._monitor_supervisor(supervisor),
                    name="runtime-supervisor-monitor",
                )
                self._phase = RuntimePhase.RUNNING
            except Exception as exc:
                if isinstance(exc, LivePreflightError):
                    logger.error("live preflight failed: %s", ",".join(exc.failed_checks))
                else:
                    logger.exception("runtime start failed")
                if services is not None:
                    try:
                        await self._shutdown(services, list(self._tasks))
                    except Exception as shutdown_exc:
                        self._last_control_error = (
                            f"startup_cleanup_failed:{type(shutdown_exc).__name__}"
                        )
                if supervisor is not None:
                    await supervisor.stop()
                if self._supervisor_monitor is not None:
                    self._supervisor_monitor.cancel()
                    await asyncio.gather(
                        self._supervisor_monitor, return_exceptions=True
                    )
                    self._supervisor_monitor = None
                self._services = None
                self._tasks = []
                self._phase = RuntimePhase.FAILED
                self._reason = (
                    "live_preflight_failed:" + ",".join(exc.failed_checks)
                    if isinstance(exc, LivePreflightError)
                    else type(exc).__name__
                )
            return self.status()

    async def stop(self) -> RuntimeStatus:
        async with self._lock:
            if self._phase == RuntimePhase.STOPPED:
                return self.status()
            services = self._services
            tasks = list(self._tasks)
            supervisor = self._supervisor
            supervisor_monitor = self._supervisor_monitor
            self._phase = RuntimePhase.STOPPING
            self._stop_event.set()
            self._terminal_event.set()
            shutdown_failed = False
            if services is not None:
                if supervisor_monitor is not None:
                    supervisor_monitor.cancel()
                    await asyncio.gather(supervisor_monitor, return_exceptions=True)
                    self._supervisor_monitor = None
                if supervisor is not None:
                    try:
                        await supervisor.stop()
                    except Exception:
                        pass
                    self._supervisor = None
                try:
                    await self._shutdown(services, tasks)
                except Exception as exc:
                    shutdown_failed = True
                    self._last_control_error = f"shutdown_failed:{type(exc).__name__}"
            else:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks = []
            self._services = None
            self._phase = (
                RuntimePhase.FAILED
                if shutdown_failed
                else RuntimePhase.STOPPED
            )
            self._reason = "shutdown_failed" if shutdown_failed else None
            self._stop_requested = False
            return self.status()

    async def wait(self) -> RuntimeStatus:
        """Await operator stop or a supervised fatal/terminal event."""

        await self._terminal_event.wait()
        return self.status()

    async def emergency_halt(self, confirmation: str) -> RuntimeStatus:
        if confirmation != "HALT":
            raise ValueError("confirmation must be exactly HALT")
        async with self._lock:
            if self._services is None or not self.is_running:
                raise RuntimeError("bot_not_running")
            await self._services.state_store.set_kill_switch(
                True,
                reason="operator_emergency_halt",
            )
            from uuid import uuid4

            now = utc_now()
            incident = OperationalIncident(
                incident_id=f"inc-{uuid4().hex}",
                fingerprint="operator:emergency_halt",
                component="operator",
                category=IncidentCategory.EXIT_SAFETY,
                severity=IncidentSeverity.URGENT,
                reason="operator_emergency_halt",
                first_seen_at=now,
                last_seen_at=now,
            )
            await self.handle_incident(
                incident,
                force_action=RecoveryAction.HALT,
            )
            return self.status()

    async def cancel_all(self, confirmation: str) -> ControlResult:
        if confirmation != "CANCEL ALL":
            raise ValueError("confirmation must be exactly CANCEL ALL")
        async with self._lock:
            if self._services is None or not self.is_running:
                raise RuntimeError("bot_not_running")
            if self._services.config.bot.mode != Mode.LIVE:
                raise RuntimeError("cancel_all_requires_live_mode")
            try:
                await self._services.submitter.cancel_all_open_orders()
            except Exception:
                self._last_control_error = "cancel_all_failed"
                return ControlResult(ok=False, action="cancel_all", reason="cancel_all_failed")
            self._last_control_error = None
            return ControlResult(ok=True, action="cancel_all", reason="orders_cancelled")

    async def shutdown_process(self) -> RuntimeStatus:
        """Host/process shutdown: graceful stop without lease revocation."""

        return await self.stop()

    def request_stop(self) -> None:
        self._stop_requested = True
        self._stop_event.set()
        self._terminal_event.set()


def _supervisor_backoff(config: AppConfig) -> BackoffSchedule:
    from reliability.backoff import BackoffSchedule

    reliability = getattr(config, "reliability", None)
    if reliability is None:
        reliability = ReliabilityConfig()
    return BackoffSchedule(reliability)
