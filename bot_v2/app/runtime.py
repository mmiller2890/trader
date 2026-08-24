"""Controllable lifecycle for CLI and dashboard operation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.bootstrap import AppServices, LivePreflightError, bootstrap_app
from app.shutdown import shutdown_app
from config.loader import load_config
from config.schema import AppConfig, Mode
from models.events import BotEvent, EventType
from models.risk import RiskCheckResult


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class RuntimePhase(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"
    FAILED = "failed"


class RuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: RuntimePhase
    mode: Mode | None = None
    reason: str | None = None
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


async def housekeeping_loop(services: AppServices, stop_event: asyncio.Event) -> None:
    """Periodic runtime checks, timer hooks, and snapshots."""

    last_snapshot_at = utc_now()
    while not stop_event.is_set():
        await services.state_store.update_heartbeat("housekeeping")
        await services.state_store.update_heartbeat("app")

        exit_manager = getattr(services, "exit_manager", None)
        if exit_manager is not None:
            market_rotator = getattr(services, "market_rotator", None)

            def market_end_lookup(market_id: str) -> datetime | None:
                if market_rotator is None:
                    return None
                current = market_rotator.status().current_market
                if current is None:
                    return None
                matches_market = (
                    market_id in {current.market_id, current.condition_id}
                )
                return current.end_at if matches_market else None

            for exit_signal in await exit_manager.on_timer(
                market_end_lookup=market_end_lookup
            ):
                await services.router.route_signal(exit_signal)

        if services.config.bot.mode == Mode.LIVE:
            reconciliation = await services.reconciliation.reconcile_runtime()
            if not reconciliation.ok:
                activated = await services.state_store.activate_kill_switch(
                    "runtime_reconciliation_failed"
                )
                if activated:
                    await services.snapshots.save_from_state(
                        services.state_store
                    )
                    await emit_event(
                        services,
                        BotEvent(
                            event_type=EventType.KILL_SWITCH_TRIPPED,
                            component="reconciliation",
                            mode=services.config.bot.mode.value,
                            message="runtime reconciliation failure",
                            reason=reconciliation.model_dump_json(),
                        ),
                    )

        runtime_decision = await services.runtime_risk.evaluate_runtime()
        if not runtime_decision.approved:
            activated = await services.state_store.activate_kill_switch(
                runtime_decision.reason
            )
            if activated:
                await services.snapshots.save_from_state(services.state_store)
                event_type = (
                    EventType.REPEATED_FAILURES
                    if _has_check(runtime_decision.checks, "repeated_failures")
                    else EventType.KILL_SWITCH_TRIPPED
                )
                await emit_event(
                    services,
                    BotEvent(
                        event_type=event_type,
                        component="runtime_risk",
                        mode=services.config.bot.mode.value,
                        message="runtime risk failure",
                        reason=runtime_decision.reason,
                    ),
                )
                if services.config.bot.mode == Mode.LIVE:
                    try:
                        await services.submitter.cancel_all_open_orders()
                    except Exception as exc:
                        await emit_event(
                            services,
                            BotEvent(
                                event_type=EventType.KILL_SWITCH_TRIPPED,
                                component="submitter",
                                mode=services.config.bot.mode.value,
                                message="cancel-all failed after halt",
                                reason=f"cancel_all_failed:{type(exc).__name__}",
                            ),
                        )

        timer_signals = await services.strategy.on_timer()
        for signal_item in timer_signals:
            await services.router.route_signal(signal_item)

        if (
            utc_now() - last_snapshot_at
        ).total_seconds() >= services.config.bot.snapshot_interval_seconds:
            await services.snapshots.save_from_state(services.state_store)
            last_snapshot_at = utc_now()

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=services.config.bot.housekeeping_interval_seconds,
            )
        except TimeoutError:
            pass


async def market_rotation_loop(
    services: AppServices, stop_event: asyncio.Event
) -> None:
    """Supervise unexpected rotation failures through the existing kill switch."""

    rotator = services.market_rotator
    if rotator is None:
        return
    try:
        await rotator.run(stop_event)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = type(exc).__name__
        rotator.mark_failed(reason)
        activated = await services.state_store.activate_kill_switch(
            f"market_rotation_failed:{reason}"
        )
        if activated:
            await services.snapshots.save_from_state(services.state_store)
            await emit_event(
                services,
                BotEvent(
                    event_type=EventType.KILL_SWITCH_TRIPPED,
                    component="market_rotation",
                    mode=services.config.bot.mode.value,
                    message="automatic market rotation failed",
                    reason=reason,
                ),
            )


Bootstrap = Callable[[str | Path | None], Awaitable[Any]]
Shutdown = Callable[[Any, list[asyncio.Task[object]]], Awaitable[None]]
Housekeeping = Callable[[Any, asyncio.Event], Awaitable[None]]
ConfigLoader = Callable[[str | Path | None], AppConfig]
EventEmitter = Callable[[Any, BotEvent], Awaitable[None]]


class BotRuntime:
    """Own one bot service graph and serialize lifecycle transitions."""

    def __init__(
        self,
        *,
        bootstrap: Bootstrap = bootstrap_app,
        shutdown: Shutdown = shutdown_app,
        housekeeping: Housekeeping = housekeeping_loop,
        config_loader: ConfigLoader = load_config,
        event_emitter: EventEmitter = emit_event,
    ) -> None:
        self._bootstrap = bootstrap
        self._shutdown = shutdown
        self._housekeeping = housekeeping
        self._config_loader = config_loader
        self._event_emitter = event_emitter
        self._lock = asyncio.Lock()
        self._phase = RuntimePhase.STOPPED
        self._services: Any | None = None
        self._tasks: list[asyncio.Task[object]] = []
        self._stop_event = asyncio.Event()
        self._stop_requested = False
        self._mode: Mode | None = None
        self._reason: str | None = None
        self._last_control_error: str | None = None

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
            last_control_error=self._last_control_error,
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
            self._stop_event = asyncio.Event()
            if self._stop_requested:
                self._stop_event.set()
            services: Any | None = None
            try:
                config = self._config_loader(config_dir)
                self._mode = config.bot.mode
                if config.bot.mode == Mode.LIVE and not allow_live:
                    self._phase = RuntimePhase.FAILED
                    self._reason = "live_start_disabled_pending_review"
                    return self.status()

                services = await self._bootstrap(config_dir)
                report = await services.reconciliation.reconcile_startup()
                if services.config.bot.mode == Mode.LIVE and not report.ok:
                    raise RuntimeError("live_startup_reconciliation_failed")

                await services.state_store.update_heartbeat("app")
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
                task = asyncio.create_task(
                    self._housekeeping(services, self._stop_event),
                    name="housekeeping",
                )
                tasks: list[asyncio.Task[object]] = [task]
                if getattr(services, "market_rotator", None) is not None:
                    tasks.append(
                        asyncio.create_task(
                            market_rotation_loop(services, self._stop_event),
                            name="market-rotation",
                        )
                    )
                self._services = services
                self._tasks = tasks
                self._phase = RuntimePhase.RUNNING
            except Exception as exc:
                if services is not None:
                    try:
                        await self._shutdown(services, list(self._tasks))
                    except Exception as shutdown_exc:
                        self._last_control_error = (
                            f"startup_cleanup_failed:{type(shutdown_exc).__name__}"
                        )
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
            self._phase = RuntimePhase.STOPPING
            self._stop_event.set()
            shutdown_failed = False
            if services is not None:
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
            await self._services.snapshots.save_from_state(
                self._services.state_store
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

    def request_stop(self) -> None:
        self._stop_requested = True
        self._stop_event.set()

    async def wait(self) -> None:
        await self._stop_event.wait()
