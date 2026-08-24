"""Typed operational models for multi-day unattended operations."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


UtcDatetime = Annotated[datetime, Field()]


class OperationalState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    HALTING = "halting"
    HALTED = "halted"
    STOPPING = "stopping"
    FAILED = "failed"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    DEGRADE = "degrade"
    HALT = "halt"


class IncidentCategory(str, Enum):
    TRANSIENT_TRANSPORT = "transient_transport"
    MARKET_DISCOVERY = "market_discovery"
    AUTHORITATIVE_STATE = "authoritative_state"
    ACCOUNT_DIVERGENCE = "account_divergence"
    AUTHENTICATION = "authentication"
    COMPLIANCE = "compliance"
    FUNDING = "funding"
    ACCOUNTING = "accounting"
    EXIT_SAFETY = "exit_safety"
    TASK_CRASH = "task_crash"
    PERSISTENCE = "persistence"
    DISK = "disk"
    NOTIFICATION = "notification"


class IncidentSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    URGENT = "urgent"


class LeaseStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


ReasonText = Annotated[str, Field(min_length=1, max_length=512)]


class OperationalIncident(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str = Field(min_length=8)
    fingerprint: str = Field(min_length=8)
    component: str = Field(min_length=1)
    category: IncidentCategory
    severity: IncidentSeverity
    reason: ReasonText
    first_seen_at: datetime
    last_seen_at: datetime
    consecutive_count: int = Field(default=1, ge=1)
    market_id: str | None = None
    token_id: str | None = None
    client_order_id: str | None = None
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_utc_timestamps(self) -> OperationalIncident:
        _require_utc(self.first_seen_at)
        _require_utc(self.last_seen_at)
        if self.resolved_at is not None:
            _require_utc(self.resolved_at)
        return self


class TaskHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    running: bool
    started_at: datetime | None = None
    last_heartbeat: datetime | None = None
    last_exit_at: datetime | None = None
    restart_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_utc_timestamps(self) -> TaskHealth:
        for value in (self.started_at, self.last_heartbeat, self.last_exit_at):
            if value is not None:
                _require_utc(value)
        return self


class LiveOperatingLease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_id: str = Field(min_length=8)
    issued_at: datetime
    expires_at: datetime
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: LeaseStatus
    revoked_at: datetime | None = None
    revocation_reason: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_lease(self) -> LiveOperatingLease:
        _require_utc(self.issued_at)
        _require_utc(self.expires_at)
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expiration must be after issuance")
        if self.status == LeaseStatus.REVOKED:
            if self.revoked_at is None or self.revocation_reason is None:
                raise ValueError(
                    "revoked leases require revoked_at and revocation_reason"
                )
            _require_utc(self.revoked_at)
        elif self.revoked_at is not None or self.revocation_reason is not None:
            raise ValueError(
                "revocation fields are only valid on revoked leases"
            )
        return self


class OutboxAlert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_id: str = Field(min_length=8)
    incident_fingerprint: str = Field(min_length=1)
    severity: IncidentSeverity
    text: ReasonText
    created_at: datetime
    next_attempt_at: datetime
    attempt_count: int = Field(default=0, ge=0)
    occurrence_count: int = Field(default=1, ge=1)
    delivered_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_alert(self) -> OutboxAlert:
        _require_utc(self.created_at)
        _require_utc(self.next_attempt_at)
        if self.delivered_at is None:
            return self
        _require_utc(self.delivered_at)
        return self
