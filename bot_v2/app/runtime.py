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
from reliability.policy import FaultPolicy, RecoveryAction


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
    services: AppServices, stop_event: asyncio.Event
) -> OperationalIncident | None:
    """Supervise rotation; return an incident instead of halting directly."""

    rotator = services.market_rotator
    if rotator is None:
        return None
    try:
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


Bootstrap = Callable[[str | Path | None], Awaitable[Any]]
Shutdown = Callable[[Any, list[asyncio.Task[object]]], Awaitable[None]]
ConfigLoader = Callable[[str | Path | None], AppConfig]
EventEmitter = Callable[[Any, BotEvent], Awaitable[None]]
LoopFactory = Callable[[AppServices, asyncio.Event], Awaitable[None]]


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
    ) -> None:
        self._bootstrap = bootstrap
        self._shutdown = shutdown
        self._config_loader = config_loader
        self._event_emitter = event_emitter
        self._loop_factories = loop_factories
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

    async def handle_incident(self, incident: OperationalIncident) -> str:
        """Centralized halt/degrade ordering for every typed incident."""

        services = self._services
        policy = FaultPolicy(
            getattr(services.config, "reliability", ReliabilityConfig())
            if services is not None
            else ReliabilityConfig()
        )
        context = await self._build_recovery_context(incident)
        action = policy.decide(incident, context)

        if action == RecoveryAction.RETRY:
            return action

        if action == RecoveryAction.DEGRADE:
            if self._degraded_since is None:
                self._degraded_since = utc_now()
            if services is not None:
                await services.state_store.set_operational_state(
                    RuntimePhase.DEGRADED, reason=incident.reason
                )
            self._operational_reason = incident.reason
            await self._emit_degraded_event(services, incident, recovered=False)
            return action

        # HALT — centralized safety ordering.
        self._phase = RuntimePhase.HALTING
        if services is not None:
            activated = await services.state_store.activate_kill_switch(
                incident.reason
            )
            try:
                await services.snapshots.save_from_state(services.state_store)
            except Exception:
                logger.critical(
                    "failed to persist kill switch snapshot",
                    extra={"component": "runtime", "reason": incident.reason},
                )
            if activated:
                await self._event_emitter(
                    services,
                    BotEvent(
                        event_type=EventType.KILL_SWITCH_TRIPPED,
                        component="runtime",
                        mode=services.config.bot.mode.value,
                        message="safety halt latched kill switch",
                        reason=incident.reason,
                    ),
                )
            if services.config.bot.mode == Mode.LIVE:
                try:
                    await asyncio.wait_for(
                        services.submitter.cancel_all_open_orders(),
                        timeout=services.config.bot.shutdown_timeout_seconds,
                    )
                except Exception as exc:
                    logger.critical(
                        "cancel-all failed during safety halt",
                        extra={
                            "component": "runtime",
                            "reason": f"cancel_all_failed:{type(exc).__name__}",
                        },
                    )
        self._phase = RuntimePhase.HALTED
        self._reason = incident.reason
        self._terminal_event.set()

    async def _build_recovery_context(self, incident: OperationalIncident) -> RecoveryContext:
        services = self._services
        flat = True
        if services is not None:
            positions = await services.state_store.get_positions()
            flat = all(position.quantity <= 0 for position in positions)
        return RecoveryContext(flat=flat)

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
            await self.handle_incident(incident)
            return RecoveryAction.RETRY

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
        factories: dict[str, LoopFactory] = {
            "reconciliation-loop": lambda svc, stop: reconciliation_loop(
                svc, stop, lambda: asyncio.sleep(0), reporter
            ),
            "runtime-risk-loop": lambda svc, stop: runtime_risk_loop(
                svc, stop, lambda: asyncio.sleep(0), reporter
            ),
            "position-exit-loop": lambda svc, stop: position_exit_loop(
                svc, stop, lambda: asyncio.sleep(0), reporter
            ),
            "strategy-timer-loop": lambda svc, stop: strategy_timer_loop(
                svc, stop, lambda: asyncio.sleep(0), reporter
            ),
            "snapshot-retention-loop": lambda svc, stop: snapshot_loop(
                svc, stop, lambda: asyncio.sleep(0), reporter
            ),
            "notification-delivery-loop": lambda svc, stop: notification_delivery_loop(
                svc, stop, lambda: asyncio.sleep(0), reporter
            ),
        }

        async def health_report_with_runtime(
            stop_event: asyncio.Event, heartbeat: Any
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
            factories = dict(self._loop_factories)
        specs: list[TaskSpec] = []
        for name, factory in factories.items():
            specs.append(
                TaskSpec(
                    name=name,
                    factory=lambda stop, hb, _f=factory, _s=services: _f(_s, stop),
                    heartbeat_timeout_seconds=heartbeat_timeout,
                )
            )
        rotator = getattr(services, "market_rotator", None)
        if rotator is not None:
            async def rotation_with_incident_handling(
                stop_event: asyncio.Event, heartbeat: Any
            ) -> None:
                while not stop_event.is_set():
                    incident = await market_rotation_loop(services, stop_event)
                    if incident is not None:
                        await self.handle_incident(incident)
                        if self._phase in (RuntimePhase.HALTED, RuntimePhase.FAILED):
                            return
                    await asyncio.sleep(0.5)

            specs.append(
                TaskSpec(
                    name="market-rotation-loop",
                    factory=rotation_with_incident_handling,
                    restartable=False,
                )
            )
        return specs

    async def _supervised_incident_handler(
        self, incident: OperationalIncident
    ) -> str:
        await self.handle_incident(incident)
        return RecoveryAction.RETRY

        return report

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

                services = await self._bootstrap(config_dir)
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
                await supervisor.start(specs)

                self._services = services
                self._supervisor = supervisor
                self._tasks = []
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
            self._phase = RuntimePhase.STOPPING
            self._stop_event.set()
            self._terminal_event.set()
            shutdown_failed = False
            if services is not None:
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
            self._phase = RuntimePhase.HALTED
            if self._services.config.bot.mode == Mode.LIVE:
                try:
                    await self._services.submitter.cancel_all_open_orders()
                    self._last_control_error = None
                except Exception:
                    self._last_control_error = "cancel_all_failed"
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
