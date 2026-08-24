from __future__ import annotations

import pytest

from config.schema import ReliabilityConfig
from models.operations import IncidentCategory, IncidentSeverity, OperationalIncident
from reliability.backoff import BackoffSchedule
from reliability.incidents import IncidentFactory
from reliability.policy import FaultPolicy, RecoveryContext, RecoveryAction


def policy() -> FaultPolicy:
    return FaultPolicy(ReliabilityConfig())


def incident(category: IncidentCategory) -> OperationalIncident:
    return OperationalIncident(
        incident_id="incident-12345678",
        fingerprint=f"{category.value}:test",
        component="test",
        category=category,
        severity=IncidentSeverity.WARNING,
        reason="test_reason",
        first_seen_at=__import__("datetime").datetime(2026, 8, 24, tzinfo=__import__("datetime").UTC),
        last_seen_at=__import__("datetime").datetime(2026, 8, 24, tzinfo=__import__("datetime").UTC),
    )


@pytest.mark.parametrize(
    ("category", "context", "expected"),
    [
        (IncidentCategory.TRANSIENT_TRANSPORT, RecoveryContext(flat=True), RecoveryAction.RETRY),
        (IncidentCategory.AUTHENTICATION, RecoveryContext(flat=True), RecoveryAction.HALT),
        (IncidentCategory.COMPLIANCE, RecoveryContext(flat=True), RecoveryAction.HALT),
        (IncidentCategory.ACCOUNTING, RecoveryContext(flat=True), RecoveryAction.HALT),
        (IncidentCategory.EXIT_SAFETY, RecoveryContext(flat=False), RecoveryAction.HALT),
        (IncidentCategory.TASK_CRASH, RecoveryContext(flat=True, task_crashes_in_window=3), RecoveryAction.RETRY),
        (IncidentCategory.TASK_CRASH, RecoveryContext(flat=True, task_crashes_in_window=4), RecoveryAction.HALT),
        (IncidentCategory.DISK, RecoveryContext(flat=True, disk_percent=89), RecoveryAction.RETRY),
        (IncidentCategory.DISK, RecoveryContext(flat=True, disk_percent=90), RecoveryAction.DEGRADE),
        (IncidentCategory.DISK, RecoveryContext(flat=True, disk_percent=95), RecoveryAction.HALT),
    ],
)
def test_fault_policy_matrix(category: IncidentCategory, context: RecoveryContext, expected: RecoveryAction) -> None:
    assert policy().decide(incident(category), context) == expected


def test_authoritative_state_degrades_flat_and_halts_exposed_after_budget() -> None:
    decided = policy().decide(
        incident(IncidentCategory.AUTHORITATIVE_STATE), RecoveryContext(flat=True)
    )
    assert decided == RecoveryAction.DEGRADE
    halted = policy().decide(
        incident(IncidentCategory.AUTHORITATIVE_STATE),
        RecoveryContext(flat=False, authoritative_unavailable_seconds=301),
    )
    assert halted == RecoveryAction.HALT
    still_degraded = policy().decide(
        incident(IncidentCategory.AUTHORITATIVE_STATE),
        RecoveryContext(flat=False, authoritative_unavailable_seconds=299),
    )
    assert still_degraded == RecoveryAction.DEGRADE


def test_account_divergence_degrades_once_and_halts_on_second_confirmation() -> None:
    first = policy().decide(
        incident(IncidentCategory.ACCOUNT_DIVERGENCE),
        RecoveryContext(flat=False, repeated_authoritative_confirmations=1),
    )
    second = policy().decide(
        incident(IncidentCategory.ACCOUNT_DIVERGENCE),
        RecoveryContext(flat=False, repeated_authoritative_confirmations=2),
    )
    assert first == RecoveryAction.DEGRADE
    assert second == RecoveryAction.HALT


def test_funding_halts_only_when_required_for_safe_exit() -> None:
    degraded = policy().decide(
        incident(IncidentCategory.FUNDING),
        RecoveryContext(flat=False, required_for_safe_exit=False),
    )
    halted = policy().decide(
        incident(IncidentCategory.FUNDING),
        RecoveryContext(flat=False, required_for_safe_exit=True),
    )
    assert degraded == RecoveryAction.DEGRADE
    assert halted == RecoveryAction.HALT


def test_exit_safety_degrades_when_flat_and_halts_when_exposed() -> None:
    flat = policy().decide(incident(IncidentCategory.EXIT_SAFETY), RecoveryContext(flat=True))
    exposed = policy().decide(incident(IncidentCategory.EXIT_SAFETY), RecoveryContext(flat=False))
    assert flat == RecoveryAction.DEGRADE
    assert exposed == RecoveryAction.HALT


def test_market_discovery_retries_while_flat() -> None:
    assert policy().decide(
        incident(IncidentCategory.MARKET_DISCOVERY), RecoveryContext(flat=True)
    ) == RecoveryAction.RETRY


def test_backoff_base_delays_are_exponential_with_cap() -> None:
    schedule = BackoffSchedule(
        ReliabilityConfig(retry_initial_seconds=1, retry_max_seconds=30, retry_jitter_ratio=0)
    )
    delays = [schedule.delay(attempt) for attempt in range(1, 8)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_backoff_jitter_stays_within_ratio() -> None:
    schedule = BackoffSchedule(
        ReliabilityConfig(retry_initial_seconds=1, retry_max_seconds=30, retry_jitter_ratio=0.2)
    )
    for attempt in range(1, 7):
        exponent = attempt - 1
        base = min(30.0, 1.0 * (2**exponent))
        low = schedule.delay(attempt, random_source=lambda: 0.0)
        high = schedule.delay(attempt, random_source=lambda: 1.0)
        midpoint = (low + high) / 2
        assert abs(midpoint - base) < 1e-9
        assert base * (1 - 0.2) - 1e-9 <= low <= high <= base * (1 + 0.2) + 1e-9


def test_backoff_rejects_negative_attempt() -> None:
    schedule = BackoffSchedule(ReliabilityConfig())
    with pytest.raises(ValueError, match="attempt"):
        schedule.delay(-1)


def test_incident_factory_maps_known_exceptions_without_raw_messages() -> None:
    factory = IncidentFactory()
    transport = factory.from_exception(
        component="reconciliation",
        error=TimeoutError("data api timeout"),
        category=IncidentCategory.TRANSIENT_TRANSPORT,
    )
    assert transport.category == IncidentCategory.TRANSIENT_TRANSPORT
    assert transport.reason == "TimeoutError"
    assert "timeout" not in transport.reason.lower() or transport.reason == "TimeoutError"

    unknown = ValueError("raw secret detail should never appear")
    mapped = factory.from_exception(
        component="runtime", error=unknown, category=IncidentCategory.PERSISTENCE
    )
    assert mapped.reason == "ValueError"
    assert "secret" not in mapped.reason


def test_incident_factory_assigns_stable_fingerprints() -> None:
    factory = IncidentFactory()
    first = factory.from_exception(
        component="reconciliation",
        error=TimeoutError("x"),
        category=IncidentCategory.TRANSIENT_TRANSPORT,
    )
    second = factory.from_exception(
        component="reconciliation",
        error=TimeoutError("y"),
        category=IncidentCategory.TRANSIENT_TRANSPORT,
    )
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.startswith("incident:")

    other_component = factory.from_exception(
        component="runtime",
        error=TimeoutError("x"),
        category=IncidentCategory.TRANSIENT_TRANSPORT,
    )
    other_category = factory.from_exception(
        component="reconciliation",
        error=TimeoutError("x"),
        category=IncidentCategory.AUTHORITATIVE_STATE,
    )
    assert other_component.fingerprint != first.fingerprint
    assert other_category.fingerprint != first.fingerprint
