"""Guarded human-intervention recovery for latched safety halts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)
from models.order import OrderResult
from models.position import Position, PositionLifecycle
from models.risk import RiskCheckResult
from persistence.operations import OperationsRepository
from persistence.snapshots import SnapshotStore
from pydantic import BaseModel, ConfigDict

HaltCategories = {
    IncidentCategory.ACCOUNTING,
    IncidentCategory.AUTHENTICATION,
    IncidentCategory.COMPLIANCE,
    IncidentCategory.EXIT_SAFETY,
    IncidentCategory.TASK_CRASH,
    IncidentCategory.PERSISTENCE,
    IncidentCategory.DISK,
}

OpenOrderReader = Callable[[], Awaitable[list[OrderResult]]]
PositionSafetyReader = Callable[
    [],
    Awaitable[tuple[list[Position], dict[tuple[str, str], PositionLifecycle]]],
]
ReconcileCheck = Callable[[], Awaitable[bool]]
InvalidatePreflight = Callable[[], None]


class RecoveryResult(BaseModel):
    """Outcome of one guarded clear-halt attempt."""

    model_config = ConfigDict(extra="forbid")

    cleared: bool
    incident_id: str
    checks: list[RiskCheckResult]
    reason: str


class InterventionRecoveryService:
    """Clears a latched halt only after every named guard passes."""

    def __init__(
        self,
        *,
        snapshot_store: SnapshotStore,
        repository: OperationsRepository,
        preflight_ok: Callable[[], bool],
        reconcile_ok: ReconcileCheck,
        disk_percent: Callable[[], float],
        open_orders_reader: OpenOrderReader,
        position_safety_reader: PositionSafetyReader,
        invalidate_preflight: InvalidatePreflight | None = None,
        max_disk_percent: float = 80.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        from datetime import UTC

        self._snapshot_store = snapshot_store
        self._repository = repository
        self._preflight_ok = preflight_ok
        self._reconcile_ok = reconcile_ok
        self._disk_percent = disk_percent
        self._open_orders_reader = open_orders_reader
        self._position_safety_reader = position_safety_reader
        self._invalidate_preflight = invalidate_preflight or (lambda: None)
        self._max_disk_percent = max_disk_percent
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._utc = UTC

    async def clear_halt(
        self, *, incident_id: str, confirmation: str
    ) -> RecoveryResult:
        checks: list[RiskCheckResult] = []

        def fail(reason: str) -> RecoveryResult:
            return RecoveryResult(
                cleared=False, incident_id=incident_id, checks=checks, reason=reason
            )

        expected_confirmation = f"CLEAR HALT {incident_id[-8:]}"
        if confirmation != expected_confirmation:
            return fail("invalid_confirmation")

        incident = await self._repository.get_incident(incident_id)
        if incident is None:
            return fail("unknown_incident")

        is_active_halt = await self._is_active_halt_incident(incident)
        checks.append(
            RiskCheckResult(
                check_name="active_halt_incident",
                passed=is_active_halt,
                reason="selected" if is_active_halt else "not_active_halt",
            )
        )
        if not is_active_halt:
            return fail("not_active_halt")

        snapshot = await self._snapshot_store.load()
        latched = snapshot is not None and snapshot.kill_switch_active
        checks.append(
            RiskCheckResult(
                check_name="kill_switch_latched",
                passed=latched,
                reason="latched" if latched else "kill_switch_not_latched",
            )
        )
        if not latched:
            return fail("kill_switch_not_latched")

        preflight_passed = bool(self._preflight_ok())
        checks.append(
            RiskCheckResult(
                check_name="fresh_preflight",
                passed=preflight_passed,
                reason="passed" if preflight_passed else "fresh_preflight_required",
            )
        )
        if not preflight_passed:
            return fail("fresh_preflight_required")

        try:
            reconciliation_passed = bool(await self._reconcile_ok())
        except Exception:
            reconciliation_passed = False
        checks.append(
            RiskCheckResult(
                check_name="authoritative_reconciliation",
                passed=reconciliation_passed,
                reason=(
                    "passed"
                    if reconciliation_passed
                    else "authoritative_reconciliation_failed"
                ),
            )
        )
        if not reconciliation_passed:
            return fail("authoritative_reconciliation_failed")

        try:
            await self._repository.outbox_stats(now=self._now())
            writable = True
        except Exception:
            writable = False
        checks.append(
            RiskCheckResult(
                check_name="persistence_and_outbox_writable",
                passed=writable,
                reason="writable" if writable else "persistence_unwritable",
            )
        )
        if not writable:
            return fail("persistence_unwritable")

        disk_value = float(self._disk_percent())
        disk_safe = disk_value < self._max_disk_percent
        checks.append(
            RiskCheckResult(
                check_name="disk_below_warning",
                passed=disk_safe,
                reason=f"{disk_value:.1f}pct" if disk_safe else "disk_above_warning",
            )
        )
        if not disk_safe:
            return fail("disk_above_warning")

        try:
            open_orders = list(await self._open_orders_reader())
        except Exception:
            open_orders = []
        orders_safe = len(open_orders) == 0
        checks.append(
            RiskCheckResult(
                check_name="no_unsafe_open_orders",
                passed=orders_safe,
                reason="clear" if orders_safe else "unsafe_open_orders_present",
            )
        )
        if not orders_safe:
            return fail("unsafe_open_orders_present")

        try:
            positions, lifecycles = await self._position_safety_reader()
        except Exception:
            positions, lifecycles = [], {}
        positions_safe = all(
            (position.market_id, position.token_id) in lifecycles
            and lifecycles[(position.market_id, position.token_id)].market_end_at
            is not None
            and lifecycles[(position.market_id, position.token_id)]
            .pending_exit_client_order_id
            is None
            and lifecycles[(position.market_id, position.token_id)].confirmation_deadline
            is None
            for position in positions
        )
        checks.append(
            RiskCheckResult(
                check_name="positions_have_safe_exit_paths",
                passed=positions_safe,
                reason=(
                    "verified" if positions_safe else "unsafe_position_present"
                ),
            )
        )
        if not positions_safe:
            return fail("unsafe_position_present")

        assert snapshot is not None
        try:
            cleared_snapshot = snapshot.model_copy(
                update={"kill_switch_active": False, "kill_switch_reason": None}
            )
            await self._snapshot_store.save(cleared_snapshot)
            await self._repository.resolve_incident(
                incident_id, resolved_at=self._now()
            )
        except Exception as exc:
            return fail(f"persistence_failed:{type(exc).__name__}")

        self._invalidate_preflight()
        return RecoveryResult(
            cleared=True,
            incident_id=incident_id,
            checks=checks,
            reason="halt_cleared",
        )

    async def _is_active_halt_incident(self, incident: OperationalIncident) -> bool:
        if incident.resolved_at is not None:
            return False
        if incident.severity != IncidentSeverity.URGENT:
            return False
        if incident.category not in HaltCategories:
            return False
        candidates = await self._repository.recent_incidents(limit=100)
        unresolved_urgent_halts = [
            candidate
            for candidate in candidates
            if candidate.resolved_at is None
            and candidate.severity == IncidentSeverity.URGENT
            and candidate.category in HaltCategories
        ]
        if not unresolved_urgent_halts:
            return False
        newest = max(unresolved_urgent_halts, key=lambda item: item.last_seen_at)
        return newest.incident_id == incident.incident_id
