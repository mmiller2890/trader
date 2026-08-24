"""Single boundary that converts runtime failures into typed incidents."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class IncidentFactory:
    """Build typed incidents with stable fingerprints and sanitized reasons."""

    def from_exception(
        self,
        *,
        component: str,
        error: Exception,
        category: IncidentCategory,
        severity: IncidentSeverity = IncidentSeverity.WARNING,
        market_id: str | None = None,
        token_id: str | None = None,
        client_order_id: str | None = None,
    ) -> OperationalIncident:
        return self.build(
            component=component,
            category=category,
            severity=severity,
            reason=type(error).__name__,
            market_id=market_id,
            token_id=token_id,
            client_order_id=client_order_id,
        )

    def from_reason(
        self,
        *,
        component: str,
        reason: str,
        category: IncidentCategory,
        severity: IncidentSeverity = IncidentSeverity.WARNING,
        market_id: str | None = None,
        token_id: str | None = None,
        client_order_id: str | None = None,
    ) -> OperationalIncident:
        return self.build(
            component=component,
            category=category,
            severity=severity,
            reason=reason[:512],
            market_id=market_id,
            token_id=token_id,
            client_order_id=client_order_id,
        )

    def build(
        self,
        *,
        component: str,
        category: IncidentCategory,
        severity: IncidentSeverity,
        reason: str,
        market_id: str | None = None,
        token_id: str | None = None,
        client_order_id: str | None = None,
    ) -> OperationalIncident:
        sanitized = reason.replace("\n", " ").replace("\r", " ")[:512]
        fingerprint_source = f"{component}:{category.value}:{sanitized}"
        fingerprint = "incident:" + hashlib.sha256(
            fingerprint_source.encode("utf-8")
        ).hexdigest()[:32]
        now = _utc_now()
        return OperationalIncident(
            incident_id=f"inc-{uuid.uuid4().hex}",
            fingerprint=fingerprint,
            component=component,
            category=category,
            severity=severity,
            reason=sanitized,
            first_seen_at=now,
            last_seen_at=now,
            market_id=market_id,
            token_id=token_id,
            client_order_id=client_order_id,
        )
