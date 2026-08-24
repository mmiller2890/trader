from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from config.schema import ReliabilityConfig
from models.operations import IncidentCategory, IncidentSeverity
from reliability.backoff import BackoffSchedule
from reliability.policy import RecoveryAction
from app.supervisor import RuntimeSupervisor, TaskSpec


def reliability_config() -> ReliabilityConfig:
    return ReliabilityConfig(
        task_restart_limit=3,
        task_restart_window_seconds=600,
        retry_initial_seconds=1,
        retry_max_seconds=30,
    )


def zero_backoff() -> BackoffSchedule:
    return BackoffSchedule(
        ReliabilityConfig(
            task_restart_limit=3,
            task_restart_window_seconds=600,
            retry_initial_seconds=0.001,
            retry_max_seconds=0.001,
            retry_jitter_ratio=0,
        )
    )


def make_supervisor(**overrides: Any) -> Any:
    now = datetime(2026, 8, 24, tzinfo=UTC)

    defaults: dict[str, Any] = {
        "config": reliability_config(),
        "incident_handler": lambda incident: asyncio.sleep(0, result=RecoveryAction.RETRY),
        "backoff": zero_backoff(),
        "now": lambda: now,
    }
    defaults.update(overrides)
    return RuntimeSupervisor(**defaults)


@pytest.mark.asyncio
async def test_clean_stop_is_not_counted_as_crash() -> None:
    stop_event = asyncio.Event()

    async def clean(_stop: asyncio.Event, heartbeat: Any) -> None:
        await heartbeat()
        stop_event.set()

    supervisor = make_supervisor()
    await supervisor.start([TaskSpec(name="loop", factory=clean)])
    fatal = None
    try:
        await asyncio.wait_for(supervisor.wait_fatal(), timeout=0.2)
    except (asyncio.TimeoutError, RuntimeError):
        pass
    health = {item.name: item for item in await supervisor.health()}
    assert health["loop"].consecutive_failures == 0
    await supervisor.stop()


@pytest.mark.asyncio
async def test_unexpected_normal_return_reports_incident() -> None:
    incidents: list[object] = []

    async def handler(incident: object) -> str:
        incidents.append(incident)
        return "retry"

    async def returning(_stop: asyncio.Event, heartbeat: Any) -> None:
        await heartbeat()
        return None

    supervisor = make_supervisor(incident_handler=handler)
    await supervisor.start([TaskSpec(name="loop", factory=returning)])
    await asyncio.sleep(0.05)
    assert len(incidents) >= 1
    assert incidents[0].reason == "task_returned_unexpectedly"
    await supervisor.stop()


@pytest.mark.asyncio
async def test_exception_reason_is_type_only() -> None:
    reasons: list[str] = []

    async def handler(incident: object) -> str:
        reasons.append(incident.reason)
        return "retry"

    async def crashing_once(stop: asyncio.Event, heartbeat: Any) -> None:
        await heartbeat()
        raise RuntimeError("secret remote detail should never appear")

    supervisor = make_supervisor(incident_handler=handler)
    await supervisor.start([TaskSpec(name="loop", factory=crashing_once)])
    await asyncio.sleep(0.1)
    await supervisor.stop()
    assert any(r == "task_crash:RuntimeError" for r in reasons)
    assert all("secret" not in r for r in reasons)


@pytest.mark.asyncio
async def test_fourth_task_crash_becomes_fatal_and_cannot_be_silent() -> None:
    crashes = 0

    async def crashing(_stop: asyncio.Event, heartbeat: Any) -> None:
        nonlocal crashes
        crashes += 1
        await heartbeat()
        raise RuntimeError("secret remote message")

    supervisor = make_supervisor()
    await supervisor.start([TaskSpec(name="reconciliation-loop", factory=crashing)])
    fatal = await asyncio.wait_for(supervisor.wait_fatal(), timeout=1)

    assert crashes == 4
    assert fatal.category == IncidentCategory.TASK_CRASH
    assert fatal.severity == IncidentSeverity.URGENT
    assert fatal.reason == "task_crash:RuntimeError"
    health = {item.name: item for item in await supervisor.health()}
    assert health["reconciliation-loop"].running is False
    assert health["reconciliation-loop"].restart_count == 3


@pytest.mark.asyncio
async def test_heartbeat_timeout_reports_task_crash() -> None:
    incidents: list[object] = []
    stuck = asyncio.Event()

    async def never_heartbeat(stop: asyncio.Event, heartbeat: Any) -> None:
        await stuck.wait()

    async def handler(incident: object) -> str:
        incidents.append(incident)
        return "halt"

    supervisor = make_supervisor(
        incident_handler=handler,
        config=ReliabilityConfig(
            task_restart_limit=3,
            task_restart_window_seconds=600,
            retry_initial_seconds=0.001,
            retry_max_seconds=0.001,
        ),
    )
    await supervisor.start([
        TaskSpec(
            name="stuck-loop",
            factory=never_heartbeat,
            heartbeat_timeout_seconds=0.05,
        )
    ])
    fatal = await asyncio.wait_for(supervisor.wait_fatal(), timeout=2)
    assert any(i.reason.startswith("heartbeat_timeout") for i in incidents)
    assert fatal.category == IncidentCategory.TASK_CRASH
    stuck.set()
    await supervisor.stop()


@pytest.mark.asyncio
async def test_stop_prevents_intentional_cancellation_from_counting() -> None:
    incidents: list[object] = []

    async def handler(incident: object) -> str:
        incidents.append(incident)
        return "retry"

    async def idle(stop: asyncio.Event, heartbeat: Any) -> None:
        await heartbeat()
        while not stop.is_set():
            await asyncio.sleep(0.01)

    supervisor = make_supervisor(incident_handler=handler)
    await supervisor.start([TaskSpec(name="loop", factory=idle)])
    await supervisor.stop()
    await asyncio.sleep(0.02)
    crash_incidents = [
        i for i in incidents if i.category == IncidentCategory.TASK_CRASH
    ]
    assert crash_incidents == []


def test_task_spec_defaults() -> None:
    async def factory(stop: asyncio.Event, heartbeat: Any) -> None:
        return None

    spec = TaskSpec(name="x", factory=factory)
    assert spec.restartable is True
    assert spec.heartbeat_timeout_seconds == pytest.approx(60.0)
