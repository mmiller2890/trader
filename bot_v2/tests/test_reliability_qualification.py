from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from reliability.qualification import (
    QualificationEvaluator,
    RequiredFault,
    RunMode,
)


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def healthy_counters() -> dict[str, object]:
    return {
        "markets_completed": 500,
        "duration_hours": 1.0,
        "orders_submitted": 120,
        "fills_accounted": 118,
        "duplicate_orders": 0,
        "orphan_open_orders": 0,
        "accounting_errors": 0,
        "injected_faults": {fault.value: 3 for fault in RequiredFault},
        "recovered_faults": {fault.value: 3 for fault in RequiredFault},
        "urgent_alerts_expected": 2,
        "urgent_alerts_delivered": 2,
        "max_memory_mib": 180.0,
        "final_memory_mib": 175.0,
    }


def evaluate_accelerated(**overrides: object):
    values = healthy_counters()
    values.update(overrides)
    return QualificationEvaluator(
        mode=RunMode.ACCELERATED,
        required_faults=list(RequiredFault),
        memory_ceiling_mib=512.0,
    ).evaluate(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "needle"),
    [
        ("duplicate_orders", "duplicate"),
        ("orphan_open_orders", "orphan"),
        ("accounting_errors", "accounting"),
    ],
)
def test_any_safety_counter_failure_fails_the_report(field: str, needle: str) -> None:
    report = evaluate_accelerated(**{field: 1})
    assert report.passed is False
    assert any(needle in failure for failure in report.failures)


@pytest.mark.parametrize("field", ["duplicate_orders", "orphan_open_orders", "accounting_errors"])
def test_zero_safety_counters_pass(field: str) -> None:
    assert evaluate_accelerated(**{field: 0}).passed is True


def test_missing_expected_urgent_alert_fails() -> None:
    report = evaluate_accelerated(urgent_alerts_delivered=1)
    assert report.passed is False
    assert any("urgent" in failure for failure in report.failures)


def test_memory_growth_beyond_ceiling_fails() -> None:
    report = evaluate_accelerated(max_memory_mib=900.0)
    assert report.passed is False
    assert any("memory" in failure for failure in report.failures)


def test_incomplete_required_fault_injection_fails() -> None:
    faults = {fault.value: 3 for fault in RequiredFault}
    del faults[RequiredFault.WEBSOCKET_DISCONNECT.value]
    report = evaluate_accelerated(injected_faults=faults)
    assert report.passed is False
    assert any("websocket_disconnect" in failure for failure in report.failures)


def test_accelerated_requires_five_hundred_rotations() -> None:
    assert evaluate_accelerated(markets_completed=499).passed is False
    assert evaluate_accelerated(markets_completed=500).passed is True


def test_wall_clock_release_requires_72h_and_288_markets() -> None:
    evaluator = QualificationEvaluator(
        mode=RunMode.WALL_CLOCK,
        required_faults=list(RequiredFault),
        memory_ceiling_mib=512.0,
    )
    base = {
        "markets_completed": 300,
        "duration_hours": 73.0,
        "orders_submitted": 90,
        "fills_accounted": 88,
        "duplicate_orders": 0,
        "orphan_open_orders": 0,
        "accounting_errors": 0,
        "injected_faults": {fault.value: 5 for fault in RequiredFault},
        "recovered_faults": {fault.value: 5 for fault in RequiredFault},
        "urgent_alerts_expected": 1,
        "urgent_alerts_delivered": 1,
        "max_memory_mib": 200.0,
        "final_memory_mib": 190.0,
    }
    assert evaluator.evaluate(**base).passed is True  # type: ignore[arg-type]

    short = dict(base, duration_hours=71.9)
    few_markets = dict(base, markets_completed=287)
    short_report = evaluator.evaluate(**short)  # type: ignore[arg-type]
    few_report = evaluator.evaluate(**few_markets)  # type: ignore[arg-type]
    assert short_report.passed is False
    assert few_report.passed is False


def test_report_records_identity_and_window() -> None:
    report = evaluate_accelerated()
    assert report.mode == "accelerated"
    assert report.started_at <= report.completed_at
    assert report.run_id
