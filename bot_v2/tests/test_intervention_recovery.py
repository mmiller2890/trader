from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from config.schema import Mode
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)
from models.order import OrderResult, OrderSide, OrderStatus
from models.position import Position, PositionLifecycle
from persistence.operations import OperationsRepository
from persistence.snapshots import SnapshotStore, StateSnapshot
from reliability.recovery import InterventionRecoveryService


NOW = datetime(2026, 8, 24, 5, 0, tzinfo=UTC)
INCIDENT_ID = "inc-12345678abcd"
CONFIRMATION = f"CLEAR HALT {INCIDENT_ID[-8:]}"


async def seed_halted_process(
    repository: OperationsRepository,
    snapshot_store: SnapshotStore,
    *,
    incident_id: str = INCIDENT_ID,
) -> None:
    await snapshot_store.save(
        StateSnapshot(
            mode=Mode.DRY_RUN,
            kill_switch_active=True,
            kill_switch_reason="accounting_invariant",
            saved_at=NOW - timedelta(minutes=1),
        )
    )
    await repository.record_incident(
        OperationalIncident(
            incident_id=incident_id,
            fingerprint="incident:halt",
            component="runtime",
            category=IncidentCategory.ACCOUNTING,
            severity=IncidentSeverity.URGENT,
            reason="accounting_invariant",
            first_seen_at=NOW - timedelta(minutes=2),
            last_seen_at=NOW - timedelta(minutes=1),
        )
    )


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.repository = OperationsRepository(tmp_path / "bot.sqlite3")
        self.snapshot_store = SnapshotStore(tmp_path / "snapshots" / "state.json")
        self.calls: dict[str, int] = {"invalidate": 0}
        self.overrides: dict[str, object] = {}

    def with_override(self, name: str, value: object) -> "Harness":
        self.overrides[name] = value
        return self

    def service(self) -> InterventionRecoveryService:
        values: dict[str, object] = {
            "snapshot_store": self.snapshot_store,
            "repository": self.repository,
            "preflight_ok": lambda: True,
            "reconcile_ok": self._async_true,
            "disk_percent": lambda: 50.0,
            "max_disk_percent": 80.0,
            "open_orders_reader": self._async_empty,
            "position_safety_reader": self._async_safe_positions,
            "invalidate_preflight": self._record_invalidate,
            "now": lambda: NOW,
        }
        values.update(self.overrides)
        return InterventionRecoveryService(**values)  # type: ignore[arg-type]

    async def _async_true(self) -> bool:
        return True

    async def _async_empty(self) -> list[object]:
        return []

    async def _async_safe_positions(
        self,
    ) -> tuple[list[Position], dict[tuple[str, str], PositionLifecycle]]:
        lifecycle = PositionLifecycle(
            market_id="m1",
            token_id="t1",
            opened_at=NOW - timedelta(minutes=5),
            last_fill_at=NOW - timedelta(minutes=4),
            market_end_at=NOW + timedelta(minutes=10),
        )
        position = Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal("2"),
            average_entry_price=Decimal("0.40"),
        )
        return [position], {("m1", "t1"): lifecycle}

    def _record_invalidate(self) -> None:
        self.calls["invalidate"] += 1


def make_open_order() -> OrderResult:
    return OrderResult(
        client_order_id="client-open-0001",
        exchange_order_id="0xopenorder001",
        status=OrderStatus.SUBMITTED,
        accepted=True,
        requested_size=Decimal("1"),
        side=OrderSide.BUY,
    )


@pytest.mark.asyncio
async def test_clear_halt_success_in_process(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await seed_halted_process(harness.repository, harness.snapshot_store)

    before = await harness.snapshot_store.load()
    assert before is not None and before.kill_switch_active is True

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is True
    checks = {check.check_name for check in result.checks}
    assert {
        "active_halt_incident",
        "kill_switch_latched",
        "fresh_preflight",
        "authoritative_reconciliation",
        "persistence_and_outbox_writable",
        "disk_below_warning",
        "no_unsafe_open_orders",
        "positions_have_safe_exit_paths",
    } <= checks
    after = await harness.snapshot_store.load()
    assert after is not None and after.kill_switch_active is False
    assert (await harness.repository.get_active_lease()) is None
    incidents = await harness.repository.recent_incidents(limit=5)
    resolved = next(i for i in incidents if i.incident_id == INCIDENT_ID)
    assert resolved.resolved_at == NOW
    assert harness.calls["invalidate"] == 1


@pytest.mark.asyncio
async def test_restarted_process_clears_from_historical_snapshot(
    tmp_path: Path,
) -> None:
    first = Harness(tmp_path)
    await seed_halted_process(first.repository, first.snapshot_store)

    restarted = Harness(tmp_path)
    result = await restarted.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is True
    after = await restarted.snapshot_store.load()
    assert after is not None and after.kill_switch_active is False


@pytest.mark.asyncio
async def test_wrong_confirmation_text_is_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await seed_halted_process(harness.repository, harness.snapshot_store)

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation="CLEAR HALT deadbeef"
    )

    assert result.cleared is False
    assert result.reason == "invalid_confirmation"


@pytest.mark.asyncio
async def test_unknown_incident_is_rejected(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await seed_halted_process(harness.repository, harness.snapshot_store)

    result = await harness.service().clear_halt(
        incident_id="inc-00000000ffff", confirmation="CLEAR HALT 0000ffff"
    )

    assert result.cleared is False
    assert result.reason == "unknown_incident"


@pytest.mark.asyncio
async def test_resolved_or_non_urgent_incident_is_not_the_active_halt(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    await seed_halted_process(harness.repository, harness.snapshot_store)
    await harness.repository.resolve_incident(INCIDENT_ID, resolved_at=NOW)

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is False
    assert result.reason == "not_active_halt"


@pytest.mark.asyncio
async def test_older_urgent_incident_is_not_the_active_halt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    newer_id = "inc-ffffffff1234"
    await seed_halted_process(harness.repository, harness.snapshot_store)
    await harness.repository.record_incident(
        OperationalIncident(
            incident_id=newer_id,
            fingerprint="incident:newer-halt",
            component="runtime",
            category=IncidentCategory.EXIT_SAFETY,
            severity=IncidentSeverity.URGENT,
            reason="exit_budget_exhausted",
            first_seen_at=NOW - timedelta(seconds=30),
            last_seen_at=NOW - timedelta(seconds=10),
        )
    )

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=f"CLEAR HALT {INCIDENT_ID[-8:]}"
    )

    assert result.cleared is False
    assert result.reason == "not_active_halt"


@pytest.mark.asyncio
async def test_unlatched_kill_switch_rejects_recovery(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await harness.repository.record_incident(
        OperationalIncident(
            incident_id=INCIDENT_ID,
            fingerprint="incident:halt",
            component="runtime",
            category=IncidentCategory.DISK,
            severity=IncidentSeverity.URGENT,
            reason="disk_halt",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
    )
    await harness.snapshot_store.save(StateSnapshot(mode=Mode.DRY_RUN))

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is False
    assert result.reason == "kill_switch_not_latched"


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ("preflight_fail", "fresh_preflight_required"),
        ("reconcile_fail", "authoritative_reconciliation_failed"),
        ("persistence_fail", "persistence_unwritable"),
        ("disk_high", "disk_above_warning"),
        ("open_orders", "unsafe_open_orders_present"),
        ("unsafe_position", "unsafe_position_present"),
    ],
)
@pytest.mark.asyncio
async def test_guard_conditions_block_recovery(
    tmp_path: Path, override: str, expected_reason: str
) -> None:
    harness = Harness(tmp_path)
    await seed_halted_process(harness.repository, harness.snapshot_store)

    if override == "preflight_fail":
        harness.with_override("preflight_ok", lambda: False)
    elif override == "reconcile_fail":
        async def false_async() -> bool:
            return False

        harness.with_override("reconcile_ok", false_async)
    elif override == "persistence_fail":
        class ExplodingRepository(OperationsRepository):
            async def outbox_stats(self, *, now: datetime) -> tuple[int, float | None]:
                raise RuntimeError("database_locked")

        harness.with_override("repository", ExplodingRepository(tmp_path / "bot.sqlite3"))
    elif override == "disk_high":
        harness.with_override("disk_percent", lambda: 85.0)
    elif override == "open_orders":
        async def with_order() -> list[OrderResult]:
            return [make_open_order()]

        harness.with_override("open_orders_reader", with_order)
    elif override == "unsafe_position":
        async def unsafe_positions(
        ) -> tuple[list[Position], dict[tuple[str, str], PositionLifecycle]]:
            position = Position(
                market_id="m9",
                token_id="t9",
                quantity=Decimal("3"),
                average_entry_price=Decimal("0.50"),
            )
            return [position], {}

        harness.with_override("position_safety_reader", unsafe_positions)

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is False
    assert result.reason == expected_reason
    snapshot = await SnapshotStore(
        tmp_path / "snapshots" / "state.json"
    ).load()
    assert snapshot is not None and snapshot.kill_switch_active is True


@pytest.mark.asyncio
async def test_failed_final_persistence_keeps_latch_and_reports(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    await seed_halted_process(harness.repository, harness.snapshot_store)

    class ExplodingSaveStore(SnapshotStore):
        async def save(self, snapshot: StateSnapshot) -> None:
            raise RuntimeError("disk_full")

    harness.with_override(
        "snapshot_store",
        ExplodingSaveStore(tmp_path / "snapshots" / "state.json"),
    )

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is False
    assert result.reason.startswith("persistence_failed:")
    stored = await harness.repository.recent_incidents(limit=5)
    selected = next(i for i in stored if i.incident_id == INCIDENT_ID)
    assert selected.resolved_at is None


@pytest.mark.asyncio
async def test_clearing_selected_incident_leaves_other_urgent_unresolved(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    other_id = "inc-aaaa1111bbbb"
    await seed_halted_process(harness.repository, harness.snapshot_store)
    await harness.repository.record_incident(
        OperationalIncident(
            incident_id=other_id,
            fingerprint="incident:older-warning",
            component="reconciliation",
            category=IncidentCategory.AUTHORITATIVE_STATE,
            severity=IncidentSeverity.WARNING,
            reason="transport_timeout",
            first_seen_at=NOW - timedelta(hours=1),
            last_seen_at=NOW - timedelta(hours=1),
        )
    )

    result = await harness.service().clear_halt(
        incident_id=INCIDENT_ID, confirmation=CONFIRMATION
    )

    assert result.cleared is True
    incidents = {i.incident_id: i for i in await harness.repository.recent_incidents(limit=10)}
    assert incidents[other_id].resolved_at is None
    assert incidents[INCIDENT_ID].resolved_at == NOW
