"""Build the dashboard's bounded, secret-free view of bot state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.runtime import BotRuntime
from config.schema import AppConfig
from dashboard.models import (
    ClosedPositionView,
    CredentialReadiness,
    DashboardState,
    EventTail,
    HeartbeatView,
    ManagedPositionView,
    MarketRotationView,
    ReadinessItem,
)
from models.events import BotEvent
from models.position import Position, PositionLifecycle
from persistence.snapshots import SnapshotStore
from portfolio.exposure import total_marked_exposure


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def tail_events(
    path: str | Path,
    *,
    limit: int = 100,
    redactions: list[str] | None = None,
) -> EventTail:
    """Return recent valid events without echoing malformed journal content."""

    journal_path = Path(path)
    if not journal_path.exists() or limit <= 0:
        return EventTail()
    with journal_path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - 256 * 1024)
        handle.seek(start)
        chunk = handle.read()
    if start > 0 and b"\n" in chunk:
        chunk = chunk.split(b"\n", 1)[1]

    events: list[BotEvent] = []
    malformed = 0
    for raw_line in chunk.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = BotEvent.model_validate_json(raw_line)
            payload = event.model_dump(mode="python")
            for field, value in payload.items():
                if not isinstance(value, str):
                    continue
                for secret in redactions or []:
                    if secret:
                        value = value.replace(secret, "[REDACTED]")
                payload[field] = value
            events.append(BotEvent.model_validate(payload))
        except Exception:
            malformed += 1
    return EventTail(events=events[-limit:], malformed_count=malformed)


class DashboardReadModel:
    """Combine live memory or the last snapshot with safe config metadata."""

    def __init__(
        self,
        *,
        config: AppConfig,
        runtime: BotRuntime,
        data_dir: str | Path,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._data_dir = Path(data_dir)
        self._now = now

    async def build(self) -> DashboardState:
        services = self._runtime.services
        source = "live" if services is not None else "historical"
        warnings: list[str] = []

        if services is not None:
            state = services.state_store
            open_orders = await state.get_open_orders()
            positions = await state.get_positions()
            balances = await state.get_balances()
            heartbeats = await state.get_heartbeats()
            kill_switch = await state.is_kill_switch_active()
            kill_switch_reason = await state.get_kill_switch_reason()
            lifecycles = await state.get_position_lifecycles()
            closed_lifecycles = await state.get_closed_position_lifecycles()
            realized_pnl_by_day = await state.get_realized_pnl_by_day()
            ws_manager = getattr(services, "ws_manager", None)
            websocket_connected = bool(
                ws_manager is not None
                and getattr(ws_manager, "is_connected", False)
            )
        else:
            try:
                snapshot = await SnapshotStore(
                    self._data_dir / "snapshots" / "state.json"
                ).load()
            except Exception:
                snapshot = None
                warnings.append("snapshot_unreadable")
            open_orders = snapshot.open_orders if snapshot else []
            positions = snapshot.positions if snapshot else []
            balances = snapshot.balances if snapshot else []
            heartbeats = snapshot.heartbeats if snapshot else {}
            kill_switch = snapshot.kill_switch_active if snapshot else False
            kill_switch_reason = snapshot.kill_switch_reason if snapshot else None
            lifecycles = snapshot.position_lifecycles if snapshot else []
            closed_lifecycles = (
                snapshot.closed_position_lifecycles if snapshot else []
            )
            realized_pnl_by_day = snapshot.realized_pnl_by_day if snapshot else {}
            websocket_connected = False

        now = self._now()
        managed_positions, closed_positions = self._build_position_views(
            positions, lifecycles, closed_lifecycles, now, warnings
        )
        heartbeat_views: list[HeartbeatView] = []
        for component, recorded_at in sorted(heartbeats.items()):
            age = max(0.0, (now - recorded_at).total_seconds())
            timeout = (
                self._config.market_data.heartbeat_timeout_seconds
                if component == "market_data"
                else max(30.0, self._config.bot.housekeeping_interval_seconds * 2)
            )
            heartbeat_views.append(
                HeartbeatView(
                    component=component,
                    recorded_at=recorded_at,
                    age_seconds=age,
                    state="fresh" if age <= timeout else "stale",
                )
            )

        credentials = CredentialReadiness(
            private_key_configured=self._config.secrets.private_key is not None,
            l2_credentials_configured=all(
                (
                    self._config.secrets.clob_api_key,
                    self._config.secrets.clob_secret,
                    self._config.secrets.clob_passphrase,
                )
            ),
            funder_configured=(
                (
                    self._config.exchange.signature_type == 0
                    and self._config.secrets.private_key is not None
                )
                or self._config.secrets.polymarket_proxy_address is not None
            ),
            rpc_configured=self._config.secrets.rpc_url is not None,
        )
        automatic_enabled = self._config.market_data.automatic_market.enabled
        market_rotation = MarketRotationView(
            enabled=automatic_enabled,
            state="starting" if automatic_enabled else "disabled",
            reason=(
                "automatic_market_pending"
                if automatic_enabled
                else "automatic_market_disabled"
            ),
        )
        if automatic_enabled and services is not None:
            rotator = getattr(services, "market_rotator", None)
            if rotator is not None:
                rotation_status = rotator.status()
                current = rotation_status.current_market
                market_rotation = MarketRotationView(
                    enabled=True,
                    state=rotation_status.state.value,
                    slug=current.slug if current else None,
                    title=current.title if current else None,
                    start_at=current.start_at if current else None,
                    end_at=current.end_at if current else None,
                    up_token_id=current.up.token_id if current else None,
                    down_token_id=current.down.token_id if current else None,
                    last_success_at=rotation_status.last_success_at,
                    reason=rotation_status.reason,
                )
        automatic_tokens = [
            token_id
            for token_id in (
                market_rotation.up_token_id,
                market_rotation.down_token_id,
            )
            if token_id is not None
        ]
        subscribed_token_ids = (
            automatic_tokens
            if automatic_enabled
            else self._config.market_data.subscribed_token_ids
        )
        subscription_count = len(subscribed_token_ids)
        automatic_scope_healthy = (
            market_rotation.state == "healthy"
            and len(automatic_tokens) == 2
            and len(set(automatic_tokens)) == 2
        )
        subscription_passed = (
            automatic_scope_healthy
            if automatic_enabled
            else subscription_count > 0
        )
        single_market_passed = (
            automatic_scope_healthy
            if automatic_enabled
            else subscription_count == 1
        )
        readiness = [
            ReadinessItem(
                name="live_start",
                passed=False,
                reason="live_start_disabled_pending_review",
            ),
            ReadinessItem(
                name="subscription",
                passed=subscription_passed,
                reason=(
                    "automatic_market_ready"
                    if automatic_enabled and subscription_passed
                    else "automatic_market_pending"
                    if automatic_enabled
                    else "subscription_configured"
                    if subscription_passed
                    else "no_subscribed_token_ids"
                ),
            ),
            ReadinessItem(
                name="single_market_scope",
                passed=single_market_passed,
                reason=(
                    "automatic_single_market_scope"
                    if automatic_enabled and single_market_passed
                    else "automatic_market_pending"
                    if automatic_enabled
                    else "single_token_configured"
                    if single_market_passed
                    else "configure_exactly_one_token_for_first_live_run"
                ),
            ),
            ReadinessItem(
                name="credentials",
                passed=(
                    credentials.private_key_configured
                    and credentials.l2_credentials_configured
                    and credentials.funder_configured
                ),
                reason=(
                    "credentials_configured"
                    if credentials.private_key_configured
                    and credentials.l2_credentials_configured
                    and credentials.funder_configured
                    else "credentials_incomplete"
                ),
            ),
        ]
        total_exposure = total_marked_exposure(positions)
        total_pnl = sum(
            realized_pnl_by_day.values(),
            start=Decimal("0"),
        ) + sum(
            (position.unrealized_pnl for position in positions),
            start=Decimal("0"),
        )
        return DashboardState(
            source=source,
            runtime=self._runtime.status(),
            mode=self._config.bot.mode,
            kill_switch=kill_switch,
            kill_switch_reason=kill_switch_reason,
            websocket_connected=websocket_connected,
            credentials=credentials,
            subscribed_token_ids=subscribed_token_ids,
            target_token_ids=self._config.spike_strategy.target_token_ids,
            market_rotation=market_rotation,
            heartbeats=heartbeat_views,
            open_orders=open_orders,
            positions=positions,
            managed_positions=managed_positions,
            closed_positions=closed_positions,
            balances=balances,
            total_exposure=total_exposure,
            total_pnl=total_pnl,
            readiness=readiness,
            warnings=warnings,
        )

    def _build_position_views(
        self,
        positions: list[Position],
        lifecycles: list[PositionLifecycle],
        closed_lifecycles: list[PositionLifecycle],
        now: datetime,
        warnings: list[str],
    ) -> tuple[list[ManagedPositionView], list[ClosedPositionView]]:
        lifecycle_by_key = {
            (lifecycle.market_id, lifecycle.token_id): lifecycle
            for lifecycle in lifecycles
        }
        managed: list[ManagedPositionView] = []
        for position in positions:
            lifecycle = lifecycle_by_key.get(
                (position.market_id, position.token_id)
            )
            opened_at = lifecycle.opened_at if lifecycle is not None else None
            held_seconds = (
                max(0.0, (now - opened_at).total_seconds())
                if opened_at is not None
                else None
            )
            return_bps = None
            if (
                position.average_entry_price > 0
                and position.mark_price is not None
            ):
                return_bps = (
                    (position.mark_price - position.average_entry_price)
                    / position.average_entry_price
                ) * Decimal("10000")
            dust = (
                position.quantity > 0
                and position.quantity < self._config.execution.min_order_size
            )
            if dust:
                warnings.append(
                    f"position_dust:{position.market_id}:{position.token_id}:{position.quantity}"
                )
            managed.append(
                ManagedPositionView(
                    position=position,
                    opened_at=opened_at,
                    held_seconds=held_seconds,
                    market_end_at=(
                        lifecycle.market_end_at if lifecycle is not None else None
                    ),
                    return_bps=return_bps,
                    exit_pending=(
                        lifecycle.pending_exit_client_order_id is not None
                        if lifecycle is not None
                        else False
                    ),
                    exit_reason=(
                        lifecycle.last_exit_reason if lifecycle is not None else None
                    ),
                    exit_attempt_count=(
                        lifecycle.exit_attempt_count if lifecycle is not None else 0
                    ),
                    confirmation_deferred=(
                        lifecycle.confirmation_deadline is not None
                        if lifecycle is not None
                        else False
                    ),
                    dust=dust,
                )
            )
        closed = [
            ClosedPositionView(
                market_id=lifecycle.market_id,
                token_id=lifecycle.token_id,
                opened_at=lifecycle.opened_at,
                closed_at=lifecycle.closed_at,
                closed_exit_price=lifecycle.closed_exit_price,
                closed_realized_pnl=lifecycle.closed_realized_pnl,
                last_exit_reason=lifecycle.last_exit_reason,
            )
            for lifecycle in closed_lifecycles[-20:]
        ]
        return managed, closed
