from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from config.schema import AppConfig
from models.events import BotEvent, EventType
from models.order import OrderResult, OrderSide, OrderStatus
from models.operations import IncidentCategory, OperationalState
from persistence.operations import OperationsRepository
from reliability.qualification import RequiredFault, RunMode
from scripts.reliability_soak import AcceleratedHarness


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def apply_fill(
    harness: AcceleratedHarness,
    order_id: str,
    side: OrderSide,
    size: str,
    price: str,
    at: datetime,
) -> None:
    result = OrderResult(
        client_order_id=order_id,
        exchange_order_id=f"0x{order_id[-12:].zfill(12)}",
        market_id="m1",
        token_id="t1",
        side=side,
        status=OrderStatus.FILLED,
        accepted=True,
        requested_size=Decimal(size),
        filled_size=Decimal(size),
        avg_fill_price=Decimal(price),
    )
    harness.state.apply_confirmed_fill(
        result,
        market_end_at=None,
        confirmed_at=at,
        confirmation_grace_seconds=30,
    )


@pytest.mark.asyncio
async def test_accelerated_run_survives_injected_faults_and_stays_bounded(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    harness = AcceleratedHarness(
        config=config,
        repository=repository,
        markets=500,
        inject_faults=True,
        now=lambda: NOW,
    )

    report = await harness.run()

    assert report.markets_completed >= 500
    assert report.duplicate_orders == 0
    assert report.orphan_open_orders == 0
    assert report.accounting_errors == 0
    injected = {fault.value for fault in RequiredFault}
    assert injected <= set(report.injected_faults)
    assert all(count > 0 for count in report.injected_faults.values())
    recovered = {fault.value for fault in RequiredFault}
    assert recovered <= set(report.recovered_faults)
    assert report.urgent_alerts_delivered == report.urgent_alerts_expected
    assert report.passed is True
    assert (await repository.get_active_lease()) is not None


@pytest.mark.asyncio
async def test_accounting_failure_halts_and_blocks_auto_resume(tmp_path: Path) -> None:
    config = AppConfig()
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    await repository.create_lease(_active_lease())
    harness = AcceleratedHarness(
        config=config,
        repository=repository,
        markets=30,
        inject_faults=True,
        now=lambda: NOW,
        halt_after="accounting_invariant",
    )

    report = await harness.run()

    assert report.passed is False
    assert any("accounting" in failure for failure in report.failures)
    lease = await repository.get_active_lease()
    assert lease is None or lease.status.value == "revoked"


def _active_lease():
    from models.operations import LeaseStatus, LiveOperatingLease

    return LiveOperatingLease(
        lease_id="lease-soak-0001",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=72),
        config_fingerprint="a" * 64,
        status=LeaseStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_fourth_task_crash_halts_and_cannot_auto_resume(tmp_path: Path) -> None:
    from app.supervisor import RuntimeSupervisor, TaskSpec
    from reliability.backoff import BackoffSchedule
    from config.schema import ReliabilityConfig
    from models.operations import IncidentCategory
    from reliability.policy import RecoveryAction

    crashes = 0

    async def crashing(stop: asyncio.Event, heartbeat) -> None:
        nonlocal crashes
        crashes += 1
        await heartbeat()
        raise RuntimeError("crash")

    async def handler(incident) -> str:
        return RecoveryAction.RETRY

    supervisor = RuntimeSupervisor(
        config=ReliabilityConfig(
            task_restart_limit=3,
            task_restart_window_seconds=600,
            retry_initial_seconds=0.001,
            retry_max_seconds=0.001,
            retry_jitter_ratio=0,
        ),
        incident_handler=handler,
        backoff=BackoffSchedule(
            ReliabilityConfig(
                task_restart_limit=3,
                task_restart_window_seconds=600,
                retry_initial_seconds=0.001,
                retry_max_seconds=0.001,
                retry_jitter_ratio=0,
            ),
            random_source=lambda: 0.0,
        ),
        now=lambda: NOW,
    )
    await supervisor.start([TaskSpec(name="loop", factory=crashing)])
    fatal = await asyncio.wait_for(supervisor.wait_fatal(), timeout=5)
    await supervisor.stop()

    assert crashes == 4
    assert fatal.category == IncidentCategory.TASK_CRASH
    assert "restart budget" in fatal.reason or fatal.reason.startswith("task_crash")


@pytest.mark.asyncio
async def test_exposed_authoritative_outage_halts(tmp_path: Path) -> None:
    from reliability.policy import FaultPolicy, RecoveryAction, RecoveryContext
    from config.schema import ReliabilityConfig

    policy = FaultPolicy(ReliabilityConfig())
    incident = _incident(IncidentCategory.AUTHORITATIVE_STATE)
    exposed = policy.decide(
        incident,
        RecoveryContext(flat=False, authoritative_unavailable_seconds=301),
    )
    flat = policy.decide(
        incident,
        RecoveryContext(flat=True, authoritative_unavailable_seconds=301),
    )
    assert exposed == RecoveryAction.HALT
    assert flat == RecoveryAction.DEGRADE


@pytest.mark.asyncio
async def test_disk_at_ninety_five_percent_halts(tmp_path: Path) -> None:
    from reliability.policy import FaultPolicy, RecoveryAction, RecoveryContext
    from config.schema import ReliabilityConfig

    policy = FaultPolicy(ReliabilityConfig())
    incident = _incident(IncidentCategory.DISK)
    decision = policy.decide(incident, RecoveryContext(flat=True, disk_percent=95))
    assert decision == RecoveryAction.HALT


def _incident(category):
    from models.operations import IncidentSeverity, OperationalIncident

    return OperationalIncident(
        incident_id="inc-halttest00001",
        fingerprint=f"halt:{category.value}",
        component="harness",
        category=category,
        severity=IncidentSeverity.URGENT,
        reason="halt_test",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


@pytest.mark.asyncio
async def test_wall_clock_progress_resumes_without_erasing_evidence(
    tmp_path: Path,
) -> None:
    progress = tmp_path / "progress.json"
    progress.write_text(
        '{"run_id": "r1", "markets_completed": 10, "orders_submitted": 3}',
        encoding="utf-8",
    )
    from scripts.reliability_soak import load_progress

    state = load_progress(progress)
    assert state["markets_completed"] == 10
