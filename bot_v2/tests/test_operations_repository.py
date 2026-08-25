from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    LeaseStatus,
    LiveOperatingLease,
    OperationalIncident,
    OutboxAlert,
)
from persistence.operations import OperationsRepository


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def active_lease(*, lease_id: str = "lease-12345678") -> LiveOperatingLease:
    return LiveOperatingLease(
        lease_id=lease_id,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=72),
        config_fingerprint="a" * 64,
        status=LeaseStatus.ACTIVE,
    )


def incident(
    *,
    incident_id: str = "incident-12345678",
    fingerprint: str = "reconciliation:data_api_timeout",
) -> OperationalIncident:
    return OperationalIncident(
        incident_id=incident_id,
        fingerprint=fingerprint,
        component="reconciliation",
        category=IncidentCategory.TRANSIENT_TRANSPORT,
        severity=IncidentSeverity.WARNING,
        reason="data_api_timeout",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def alert(*, alert_id: str = "alert-12345678") -> OutboxAlert:
    return OutboxAlert(
        alert_id=alert_id,
        incident_fingerprint="reconciliation:data_api_timeout",
        severity=IncidentSeverity.WARNING,
        text="data api timeout while flat",
        created_at=NOW,
        next_attempt_at=NOW,
    )


@pytest.mark.asyncio
async def test_active_lease_survives_repository_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3"
    first = OperationsRepository(path)
    lease = active_lease()
    await first.create_lease(lease)

    restored = await OperationsRepository(path).get_active_lease()

    assert restored == lease


@pytest.mark.asyncio
async def test_revoke_active_lease_is_atomic_and_keeps_first_reason(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bot.sqlite3"
    repository = OperationsRepository(path)
    await repository.create_lease(active_lease())

    revoked_at = NOW + timedelta(hours=1)
    first = await repository.revoke_active_lease(
        reason="safety_fault", revoked_at=revoked_at
    )
    second = await repository.revoke_active_lease(
        reason="second_reason", revoked_at=revoked_at + timedelta(minutes=1)
    )

    assert first is not None
    assert first.status == LeaseStatus.REVOKED
    assert first.revocation_reason == "safety_fault"
    assert second.revocation_reason == "safety_fault"
    assert await repository.get_active_lease() is None


@pytest.mark.asyncio
async def test_enqueue_deduplicates_pending_incident(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    first = await repository.enqueue_alert(alert(alert_id="alert-00000001"), dedupe_after=NOW - timedelta(minutes=15))
    second = await repository.enqueue_alert(alert(alert_id="alert-00000002"), dedupe_after=NOW - timedelta(minutes=15))
    assert second.alert_id == first.alert_id
    assert second.occurrence_count == 2
    assert len(await repository.due_alerts(now=NOW, limit=20)) == 1


@pytest.mark.asyncio
async def test_delivered_alerts_are_excluded_from_due_queue(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    queued = await repository.enqueue_alert(alert(), dedupe_after=NOW)
    await repository.mark_alert_delivered(queued.alert_id, delivered_at=NOW)

    assert await repository.due_alerts(now=NOW + timedelta(minutes=5), limit=20) == []


@pytest.mark.asyncio
async def test_reschedule_updates_attempt_and_sanitizes_error(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    queued = await repository.enqueue_alert(alert(), dedupe_after=NOW)

    long_error = "x" * 500
    await repository.reschedule_alert(
        queued.alert_id,
        next_attempt_at=NOW + timedelta(seconds=30),
        error=long_error,
    )
    due = await repository.due_alerts(now=NOW + timedelta(seconds=31), limit=20)

    assert len(due) == 1
    assert due[0].attempt_count == 1
    assert len(due[0].last_error or "") <= 256
    assert (due[0].last_error or "").startswith("xxx")


@pytest.mark.asyncio
async def test_incidents_round_trip_and_resolve(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    created = incident()
    await repository.record_incident(created)

    restored_repo = OperationsRepository(tmp_path / "bot.sqlite3")
    recent = await restored_repo.recent_incidents(limit=10)
    assert len(recent) == 1
    assert recent[0] == created

    resolved_at = NOW + timedelta(minutes=2)
    resolved = await restored_repo.resolve_incident(
        created.incident_id, resolved_at=resolved_at
    )
    assert resolved.resolved_at == resolved_at
    assert (await restored_repo.recent_incidents(limit=10))[0].resolved_at == (
        resolved_at
    )


@pytest.mark.asyncio
async def test_repeated_unresolved_fingerprint_keeps_identity_and_increments(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    first = incident(incident_id="incident-00000001")
    repeated = incident(incident_id="incident-00000002").model_copy(
        update={"last_seen_at": NOW + timedelta(minutes=1)}
    )

    stored_first = await repository.record_incident(first)
    stored_repeated = await repository.record_incident(repeated)

    assert stored_first.incident_id == "incident-00000001"
    assert stored_repeated.incident_id == "incident-00000001"
    assert stored_repeated.first_seen_at == NOW
    assert stored_repeated.last_seen_at == NOW + timedelta(minutes=1)
    assert stored_repeated.consecutive_count == 2
    assert len(await repository.recent_incidents(limit=10)) == 1


@pytest.mark.asyncio
async def test_last_delivered_at_returns_latest_for_fingerprint(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    assert await repository.last_delivered_at("fp") is None

    first = await repository.enqueue_alert(
        alert(alert_id="alert-00000001").model_copy(update={"incident_fingerprint": "fp"}),
        dedupe_after=NOW - timedelta(minutes=15),
    )
    delivered_at = NOW + timedelta(minutes=1)
    await repository.mark_alert_delivered(first.alert_id, delivered_at=delivered_at)

    later = await repository.enqueue_alert(
        alert(alert_id="alert-00000002").model_copy(update={"incident_fingerprint": "fp"}),
        dedupe_after=delivered_at + timedelta(minutes=15),
    )
    later_delivery = delivered_at + timedelta(hours=1)
    await repository.mark_alert_delivered(later.alert_id, delivered_at=later_delivery)

    assert await repository.last_delivered_at("fp") == later_delivery


@pytest.mark.asyncio
async def test_prune_removes_old_delivered_and_incidents_but_not_pending(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    delivered = await repository.enqueue_alert(alert(alert_id="alert-00000003"), dedupe_after=NOW)
    await repository.mark_alert_delivered(delivered.alert_id, delivered_at=NOW)
    pending = await repository.enqueue_alert(
        alert(alert_id="alert-00000004"),
        dedupe_after=NOW - timedelta(minutes=15),
    )
    old_incident = incident()
    await repository.record_incident(old_incident)
    resolved_at = NOW + timedelta(minutes=5)
    await repository.resolve_incident(old_incident.incident_id, resolved_at=resolved_at)

    removed_alerts, removed_incidents = await repository.prune(
        delivered_before=NOW + timedelta(days=31),
        incidents_before=NOW + timedelta(days=91),
    )

    assert removed_alerts >= 1
    assert removed_incidents >= 1
    remaining = await repository.due_alerts(now=NOW + timedelta(days=32), limit=20)
    assert [row.alert_id for row in remaining] == [pending.alert_id]
    assert len(await repository.recent_incidents(limit=10)) == 0


@pytest.mark.asyncio
async def test_prune_never_removes_unresolved_incidents(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    unresolved = incident()
    await repository.record_incident(unresolved)

    await repository.prune(
        delivered_before=NOW + timedelta(days=31),
        incidents_before=NOW + timedelta(days=91),
    )

    assert len(await repository.recent_incidents(limit=10)) == 1


@pytest.mark.asyncio
async def test_outbox_stats_report_depth_and_age(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    depth, oldest_age = await repository.outbox_stats(now=NOW)
    assert depth == 0 and oldest_age is None

    await repository.enqueue_alert(alert(), dedupe_after=NOW - timedelta(minutes=15))
    depth, oldest_age = await repository.outbox_stats(now=NOW + timedelta(minutes=16))
    assert depth == 1
    assert oldest_age is not None and oldest_age >= (16 * 60)
