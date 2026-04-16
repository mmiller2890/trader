"""Risk evaluation domain models."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def decision_id() -> str:
    """Generate unique risk decision id."""

    return uuid4().hex


class RiskAction(str, Enum):
    """Risk engine action values."""

    APPROVE = "approve"
    REJECT = "reject"
    HALT = "halt"


class RiskCheckResult(BaseModel):
    """Result for an individual risk check."""

    model_config = ConfigDict(extra="forbid")

    check_name: str = Field(min_length=1)
    passed: bool
    reason: str = Field(min_length=1)


class RiskDecision(BaseModel):
    """Aggregated risk evaluation decision."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(default_factory=decision_id, min_length=8)
    action: RiskAction
    approved: bool
    checks: list[RiskCheckResult] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    signal_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
