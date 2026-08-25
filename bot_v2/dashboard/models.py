"""Secret-free API models for the local operator dashboard."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.runtime import RuntimeStatus
from clients.market_rotation import MarketRotationState
from config.schema import Mode
from models.events import BotEvent
from models.operations import TaskHealth
from models.order import OrderResult
from models.position import Balance, ExitReason, Position, PositionLifecycle


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


class ManagedPositionView(BaseModel):
    """Secret-free managed position view for the dashboard."""

    model_config = ConfigDict(extra="forbid")

    position: Position
    opened_at: datetime | None = None
    held_seconds: float | None = Field(default=None, ge=0)
    market_end_at: datetime | None = None
    return_bps: Decimal | None = None
    exit_pending: bool = False
    exit_reason: ExitReason | None = None
    exit_attempt_count: int = Field(default=0, ge=0)
    confirmation_deferred: bool = False
    dust: bool = False


class ClosedPositionView(BaseModel):
    """Bounded closed lifecycle record for the dashboard."""

    model_config = ConfigDict(extra="forbid")

    market_id: str
    token_id: str
    opened_at: datetime
    closed_at: datetime | None = None
    closed_exit_price: Decimal | None = None
    closed_realized_pnl: Decimal | None = None
    last_exit_reason: ExitReason | None = None


class CredentialReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    private_key_configured: bool
    l2_credentials_configured: bool
    funder_configured: bool
    rpc_configured: bool


class HeartbeatView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    recorded_at: datetime
    age_seconds: float = Field(ge=0)
    state: Literal["fresh", "stale"]


class ReadinessItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    reason: str


class EventTail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[BotEvent] = Field(default_factory=list)
    malformed_count: int = Field(default=0, ge=0)


class PreflightCheckView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    reason: str


class PreflightView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    status: Literal["not_run", "running", "passed", "failed"]
    reason: str
    checked_at: datetime | None = None
    checks: list[PreflightCheckView] = Field(default_factory=list)


class MarketRotationView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    state: MarketRotationState
    slug: str | None = None
    title: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    up_token_id: str | None = None
    down_token_id: str | None = None
    last_success_at: datetime | None = None
    reason: str


class DashboardState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime = Field(default_factory=utc_now)
    source: Literal["live", "historical"]
    runtime: RuntimeStatus
    mode: Mode
    kill_switch: bool
    kill_switch_reason: str | None = None
    websocket_connected: bool
    credentials: CredentialReadiness
    subscribed_token_ids: list[str]
    target_token_ids: list[str]
    preflight_fresh: bool = False
    preflight_expires_at: datetime | None = None
    live_armed: bool = False
    live_start_ready: bool = False
    market_rotation: MarketRotationView = Field(
        default_factory=lambda: MarketRotationView(
            enabled=False,
            state=MarketRotationState.DISABLED,
            reason="automatic_market_disabled",
        )
    )
    preflight: PreflightView = Field(
        default_factory=lambda: PreflightView(
            ok=False,
            status="not_run",
            reason="preflight_not_run",
        )
    )
    heartbeats: list[HeartbeatView] = Field(default_factory=list)
    open_orders: list[OrderResult] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    managed_positions: list[ManagedPositionView] = Field(default_factory=list)
    closed_positions: list[ClosedPositionView] = Field(default_factory=list)
    balances: list[Balance] = Field(default_factory=list)
    total_exposure: Decimal = Decimal("0")
    total_pnl: Decimal = Decimal("0")
    readiness: list[ReadinessItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    task_health: list[TaskHealth] = Field(default_factory=list)
    market_data_source: Literal["websocket", "rest_fallback", "unavailable"] = (
        "unavailable"
    )
    last_reconciliation_at: datetime | None = None
    outbox_pending: int = 0
    oldest_outbox_age_seconds: float | None = None
    disk_percent: float = 0.0
    lease_expires_at: datetime | None = None
    lease_remaining_seconds: float | None = None
    auto_resume_eligible: bool = False
    open_urgent_incidents: int = 0
