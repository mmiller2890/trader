"""Supervisor owning every critical runtime task."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from config.schema import ReliabilityConfig
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
    TaskHealth,
)
from reliability.backoff import BackoffSchedule
from reliability.policy import RecoveryAction


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


Heartbeat = Callable[[], Awaitable[None]]
TaskFactory = Callable[[asyncio.Event, Heartbeat], Awaitable[None]]
IncidentHandler = Callable[[OperationalIncident], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    factory: TaskFactory
    restartable: bool = True
    heartbeat_timeout_seconds: float = 60.0


class _OwnedTask:
    def __init__(self, spec: TaskSpec) -> None:
        self.spec = spec
        self.started_at: datetime | None = None
        self.last_heartbeat: datetime | None = None
        self.last_exit_at: datetime | None = None
        self.restart_count = 0
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.running = False
        self.monotonic_heartbeat: float = 0.0


class RuntimeSupervisor:
    """Owns critical tasks, restarts within budget, escalates to fatal once."""

    def __init__(
        self,
        *,
        config: ReliabilityConfig,
        incident_handler: IncidentHandler,
        backoff: BackoffSchedule,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._incident_handler = incident_handler
        self._backoff = backoff
        self._now = now
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._owned: dict[str, _OwnedTask] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._watchdog: asyncio.Task[None] | None = None
        self._fatal_future: asyncio.Future[OperationalIncident] | None = None
        self._fatal_incident: OperationalIncident | None = None

    async def start(self, specs: list[TaskSpec]) -> None:
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        self._fatal_future = loop.create_future()
        for spec in specs:
            owned = _OwnedTask(spec)
            self._owned[spec.name] = owned
            task = asyncio.create_task(
                self._run_owned(owned), name=f"supervised:{spec.name}"
            )
            self._tasks.append(task)
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="supervisor-watchdog")

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
        self._tasks.clear()
        self._watchdog = None

    async def wait_fatal(self) -> OperationalIncident:
        if self._fatal_incident is not None:
            return self._fatal_incident
        if self._fatal_future is None or self._fatal_future.done():
            raise RuntimeError("no fatal incident was produced")
        return await self._fatal_future

    def is_alive(self) -> bool:
        """False once a fatal incident has terminated supervision."""

        return self._fatal_incident is None

    async def health(self) -> list[TaskHealth]:
        now = self._now()
        healths: list[TaskHealth] = []
        for name, owned in self._owned.items():
            healths.append(
                TaskHealth(
                    name=name,
                    running=owned.running,
                    started_at=owned.started_at,
                    last_heartbeat=owned.last_heartbeat,
                    last_exit_at=owned.last_exit_at,
                    restart_count=owned.restart_count,
                    consecutive_failures=owned.consecutive_failures,
                    last_error=owned.last_error,
                )
            )
        return healths

    def heartbeat_now(self) -> datetime:
        now = self._now()
        return now

    async def _run_owned(self, owned: _OwnedTask) -> None:
        while not self._stop_event.is_set():
            owned.running = True
            owned.started_at = self._now()
            owned.last_heartbeat = owned.started_at
            owned.monotonic_heartbeat = asyncio.get_running_loop().time()

            async def heartbeat() -> None:
                owned.last_heartbeat = self._now()
                owned.monotonic_heartbeat = asyncio.get_running_loop().time()

            try:
                await owned.spec.factory(self._stop_event, heartbeat)
            except asyncio.CancelledError:
                owned.running = False
                owned.last_exit_at = self._now()
                raise
            except Exception as exc:
                owned.running = False
                owned.last_exit_at = self._now()
                owned.consecutive_failures += 1
                owned.last_error = type(exc).__name__
                await self._report_crash(owned, type(exc).__name__)
            else:
                owned.running = False
                owned.last_exit_at = self._now()
                await self._report_unexpected_return(owned)

            if self._stop_event.is_set():
                return

            action = await self._decide(owned, "restart")
            if action != RecoveryAction.RETRY:
                return
            owned.restart_count += 1
            delay = self._backoff.delay(min(owned.restart_count + 1, 6))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=max(0.001, delay)
                )
                return
            except TimeoutError:
                continue

    async def _report_crash(self, owned: _OwnedTask, error_type: str) -> None:
        incident = OperationalIncident(
            incident_id=f"inc-{uuid.uuid4().hex}",
            fingerprint=f"task_crash:{owned.spec.name}:{error_type}",
            component=owned.spec.name,
            category=IncidentCategory.TASK_CRASH,
            severity=IncidentSeverity.WARNING,
            reason=f"task_crash:{error_type}",
            first_seen_at=self._now(),
            last_seen_at=self._now(),
        )
        await self._handle(incident)

    async def _report_unexpected_return(self, owned: _OwnedTask) -> None:
        incident = OperationalIncident(
            incident_id=f"inc-{uuid.uuid4().hex}",
            fingerprint=f"task_returned:{owned.spec.name}",
            component=owned.spec.name,
            category=IncidentCategory.TASK_CRASH,
            severity=IncidentSeverity.WARNING,
            reason="task_returned_unexpectedly",
            first_seen_at=self._now(),
            last_seen_at=self._now(),
        )
        await self._handle(incident)

    async def _handle(self, incident: OperationalIncident) -> None:
        action = await self._incident_handler(incident)
        if isinstance(action, str):
            action_value = action
        else:
            action_value = str(action)
        if action_value == RecoveryAction.HALT:
            urgent = incident.model_copy(update={"severity": IncidentSeverity.URGENT})
            self._set_fatal(urgent)

    def _decide_sync(self, owned: _OwnedTask, context_reason: str) -> str:
        crashes = owned.consecutive_failures
        if crashes >= self._config.task_restart_limit + 1:
            return RecoveryAction.HALT
        return RecoveryAction.RETRY

    async def _decide(self, owned: _OwnedTask, context_reason: str) -> str:
        if not owned.spec.restartable:
            return RecoveryAction.HALT
        if owned.consecutive_failures >= self._config.task_restart_limit + 1:
            error_type = owned.last_error or "unknown"
            incident = OperationalIncident(
                incident_id=f"inc-{uuid.uuid4().hex}",
                fingerprint=f"task_crash_budget:{owned.spec.name}",
                component=owned.spec.name,
                category=IncidentCategory.TASK_CRASH,
                severity=IncidentSeverity.URGENT,
                reason=f"task_crash:{error_type}",
                first_seen_at=self._now(),
                last_seen_at=self._now(),
            )
            self._set_fatal(incident)
            return RecoveryAction.HALT
        return RecoveryAction.RETRY

    def _set_fatal(self, incident: OperationalIncident) -> None:
        if self._fatal_incident is not None:
            return
        self._fatal_incident = incident
        if self._fatal_future is not None and not self._fatal_future.done():
            self._fatal_future.set_result(incident)

    async def _watchdog_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            timeout = min(
                (spec.heartbeat_timeout_seconds for spec in
                 (o.spec for o in self._owned.values())),
                default=5.0,
            )
            await asyncio.sleep(max(0.02, min(0.05, timeout / 2)))
            monotonic_now = loop.time()
            for name, owned in self._owned.items():
                if not owned.running or owned.spec.heartbeat_timeout_seconds <= 0:
                    continue
                stale_for = monotonic_now - owned.monotonic_heartbeat
                if stale_for > owned.spec.heartbeat_timeout_seconds:
                    incident = OperationalIncident(
                        incident_id=f"inc-{uuid.uuid4().hex}",
                        fingerprint=f"heartbeat_timeout:{name}",
                        component=name,
                        category=IncidentCategory.TASK_CRASH,
                        severity=IncidentSeverity.WARNING,
                        reason=f"heartbeat_timeout:{stale_for:.1f}s",
                        first_seen_at=self._now(),
                        last_seen_at=self._now(),
                    )
                    await self._handle(incident)
