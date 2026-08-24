from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    LeaseStatus,
    LiveOperatingLease,
    OperationalIncident,
    OperationalState,
    OutboxAlert,
)


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def test_live_lease_is_typed_and_contains_no_secret_fields() -> None:
    lease = LiveOperatingLease(
        lease_id="lease-12345678",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=72),
        config_fingerprint="a" * 64,
        status=LeaseStatus.ACTIVE,
    )
    assert lease.status == LeaseStatus.ACTIVE
    assert set(lease.model_dump()) == {
        "lease_id", "issued_at", "expires_at", "config_fingerprint",
        "status", "revoked_at", "revocation_reason",
    }


def test_incident_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        OperationalIncident(
            incident_id="incident-12345678",
            fingerprint="reconciliation:data_api_timeout",
            component="reconciliation",
            category=IncidentCategory.TRANSIENT_TRANSPORT,
            severity=IncidentSeverity.WARNING,
            reason="data_api_timeout",
            first_seen_at=datetime(2026, 8, 24),
            last_seen_at=NOW,
        )


def test_operational_state_has_required_non_running_states() -> None:
    assert tuple(state.value for state in OperationalState) == (
        "stopped", "starting", "running", "degraded", "halting",
        "halted", "stopping", "failed",
    )


def test_lease_expiration_must_follow_issuance() -> None:
    with pytest.raises(ValidationError):
        LiveOperatingLease(
            lease_id="lease-12345678",
            issued_at=NOW,
            expires_at=NOW - timedelta(seconds=1),
            config_fingerprint="a" * 64,
            status=LeaseStatus.ACTIVE,
        )


def test_revocation_fields_are_required_only_for_revoked_leases() -> None:
    with pytest.raises(ValidationError):
        LiveOperatingLease(
            lease_id="lease-12345678",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=72),
            config_fingerprint="a" * 64,
            status=LeaseStatus.ACTIVE,
            revocation_reason="should_not_be_here",
        )
    revoked = LiveOperatingLease(
        lease_id="lease-12345678",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=72),
        config_fingerprint="a" * 64,
        status=LeaseStatus.REVOKED,
        revoked_at=NOW,
        revocation_reason="safety_fault",
    )
    assert revoked.revoked_at == NOW


def test_delivered_and_pending_alerts_are_both_valid() -> None:
    pending = OutboxAlert(
        alert_id="alert-12345678",
        incident_fingerprint="fp",
        severity=IncidentSeverity.URGENT,
        text="body",
        created_at=NOW,
        next_attempt_at=NOW,
    )
    assert pending.delivered_at is None
    delivered = OutboxAlert(
        alert_id="alert-12345678",
        incident_fingerprint="fp",
        severity=IncidentSeverity.URGENT,
        text="body",
        created_at=NOW,
        next_attempt_at=NOW,
        delivered_at=NOW,
    )
    assert delivered.delivered_at == NOW
