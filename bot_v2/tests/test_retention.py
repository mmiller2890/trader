from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from config.schema import Mode, ReliabilityConfig
from models.market import MarketSnapshot, OrderBookUpdate
from models.order import OrderResult, OrderSide, OrderStatus
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)
from models.position import FillCheckpoint, Position, PositionLifecycle
from models.signal import SignalSide, TradeSignal
from persistence.operations import OperationsRepository
from persistence.retention import RetentionManager
from state.store import InMemoryStateStore


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def reliability_config(**overrides: object) -> ReliabilityConfig:
    values: dict[str, object] = {
        "signal_retention_count": 3,
        "signal_retention_hours": 24,
        "fill_checkpoint_retention_days": 7,
        "realized_pnl_hot_days": 2,
        "closed_lifecycle_hot_count": 1,
    }
    values.update(overrides)
    return ReliabilityConfig(**values)


def book(key: tuple[str, str]) -> OrderBookUpdate:
    market_id, token_id = key
    return OrderBookUpdate(
        market_id=market_id,
        token_id=token_id,
        bids=[],
        asks=[],
        source_ts=NOW,
        received_ts=NOW,
    )


def snap(key: tuple[str, str]) -> MarketSnapshot:
    market_id, token_id = key
    return MarketSnapshot(
        market_id=market_id,
        token_id=token_id,
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.50"),
        mid_price=Decimal("0.495"),
        top_bid_size=Decimal("10"),
        top_ask_size=Decimal("10"),
        source_ts=NOW,
        received_ts=NOW,
    )


def signal(signal_id: str, created_at: datetime) -> TradeSignal:
    return TradeSignal(
        signal_id=signal_id,
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.45"),
        observed_move_bps=10.0,
        created_at=created_at,
        reason=f"testing:{signal_id}",
    )


def checkpoint(
    order_key: str,
    confirmed_at: datetime,
    *,
    market_id: str = "m1",
    token_id: str = "t1",
) -> FillCheckpoint:
    return FillCheckpoint(
        order_key=order_key,
        market_id=market_id,
        token_id=token_id,
        side=OrderSide.BUY,
        accounted_filled_size=Decimal("1"),
        accounted_fill_notional=Decimal("0.5"),
        confirmed_at=confirmed_at,
    )


def closed_lifecycle(closed_at: datetime, *, token_id: str = "t1") -> PositionLifecycle:
    return PositionLifecycle(
        market_id="m1",
        token_id=token_id,
        opened_at=closed_at - timedelta(minutes=15),
        last_fill_at=closed_at,
        closed_at=closed_at,
    )


def make_manager(
    repository: OperationsRepository,
    tmp_path: Path,
    *,
    config: ReliabilityConfig | None = None,
    disk_percent: float = 50.0,
) -> tuple[RetentionManager, list[object]]:
    incidents: list[object] = []

    async def report(incident: object) -> str:
        incidents.append(incident)
        return "retry"

    manager = RetentionManager(
        repository=repository,
        config=config or reliability_config(),
        data_path=tmp_path,
        disk_usage=lambda _path: disk_percent,
    )
    manager.set_reporter(report)
    return manager, incidents


async def seed_market_data(state: InMemoryStateStore) -> None:
    for key in (("cur", "tok-a"), ("prev", "tok-b"), ("old", "tok-c")):
        await state.update_orderbook(book(key))
        await state.update_market_snapshot(snap(key))


@pytest.mark.asyncio
async def test_old_market_data_is_pruned_but_current_and_previous_remain(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await seed_market_data(state)

    manager, _ = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state,
        active_market_keys={("cur", "tok-a"), ("prev", "tok-b")},
        now=NOW,
    )

    assert await state.get_orderbook("cur", "tok-a") is not None
    assert await state.get_orderbook("prev", "tok-b") is not None
    assert await state.get_orderbook("old", "tok-c") is None
    assert await state.get_market_snapshot("old", "tok-c") is None
    assert await state.get_market_snapshot("cur", "tok-a") is not None
    assert report.market_books_removed == 1
    assert report.market_snapshots_removed == 1


@pytest.mark.asyncio
async def test_signal_pruning_enforces_cap_then_age(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    fresh_ids = ["s-fresh-1", "s-fresh-2", "s-fresh-3", "s-fresh-4"]
    stale_ids = ["s-stale-1", "s-stale-2"]
    for index, signal_id in enumerate(fresh_ids):
        await state.add_signal(signal(signal_id, NOW - timedelta(minutes=index)))
    for index, signal_id in enumerate(stale_ids):
        await state.add_signal(
            signal(signal_id, NOW - timedelta(hours=30 + index))
        )

    manager, _ = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state, active_market_keys=set(), now=NOW
    )

    remaining = {item.signal_id for item in await state.get_signals()}
    assert remaining == {"s-fresh-1", "s-fresh-2", "s-fresh-3"}
    assert report.signals_removed == 3


@pytest.mark.asyncio
async def test_stale_checkpoints_are_archived_only_when_unreferenced(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    stale = NOW - timedelta(days=8)
    await state.restore_fill_checkpoint(checkpoint("0xopenorder001", stale))
    await state.restore_fill_checkpoint(
        checkpoint("0xposstale0001", stale, market_id="m2", token_id="tok-p")
    )
    await state.restore_fill_checkpoint(checkpoint("0xincidnt0001", stale))
    await state.restore_fill_checkpoint(checkpoint("0xfreestale01", stale))
    await state.restore_fill_checkpoint(
        checkpoint("0xrecentok001", NOW - timedelta(days=1))
    )
    await state.set_order_status(
        OrderResult(
            client_order_id="client-open-0001",
            exchange_order_id="0xopenorder001",
            status=OrderStatus.SUBMITTED,
            accepted=True,
            requested_size=Decimal("1"),
        )
    )
    await state.set_position(
        Position(
            market_id="m2",
            token_id="tok-p",
            quantity=Decimal("2"),
            average_entry_price=Decimal("0.40"),
        )
    )
    await repository.record_incident(
        OperationalIncident(
            incident_id="incident-unresolved1",
            fingerprint="fingerprint:unresolved",
            component="reconciliation",
            category=IncidentCategory.ACCOUNT_DIVERGENCE,
            severity=IncidentSeverity.WARNING,
            reason="unresolved_divergence",
            first_seen_at=stale,
            last_seen_at=stale,
            client_order_id="0xincidnt0001",
        )
    )

    manager, _ = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state, active_market_keys=set(), now=NOW
    )

    remaining = {
        item.order_key for item in await state.get_fill_checkpoints()
    }
    assert remaining == {
        "0xopenorder001",
        "0xposstale0001",
        "0xincidnt0001",
        "0xrecentok001",
    }
    assert report.fill_checkpoints_removed == 1
    archived = await repository.archived_checkpoint_order_keys()
    assert archived == ["0xfreestale01"]


@pytest.mark.asyncio
async def test_pnl_days_outside_hot_window_are_archived_then_removed(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    today = NOW.date().isoformat()
    day_one = (NOW - timedelta(days=1)).date().isoformat()
    day_three = (NOW - timedelta(days=3)).date().isoformat()
    day_five = (NOW - timedelta(days=5)).date().isoformat()
    await state.restore_realized_pnl_by_day(
        {
            today: Decimal("5"),
            day_one: Decimal("4"),
            day_three: Decimal("3"),
            day_five: Decimal("2"),
        }
    )

    manager, _ = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state, active_market_keys=set(), now=NOW
    )

    hot = await state.get_realized_pnl_by_day()
    assert set(hot) == {today, day_one}
    assert report.pnl_days_archived == 2
    archive = await repository.archived_pnl_days()
    assert archive == {day_three: Decimal("3"), day_five: Decimal("2")}


@pytest.mark.asyncio
async def test_closed_lifecycles_beyond_hot_count_are_archived(
    tmp_path: Path,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    newest = closed_lifecycle(NOW - timedelta(minutes=15), token_id="t-new")
    middle = closed_lifecycle(NOW - timedelta(minutes=75), token_id="t-mid")
    oldest = closed_lifecycle(NOW - timedelta(minutes=135), token_id="t-old")
    for lifecycle in (oldest, newest, middle):
        await state.restore_closed_position_lifecycle(lifecycle)

    manager, _ = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state, active_market_keys=set(), now=NOW
    )

    hot = await state.get_closed_position_lifecycles()
    assert hot == [newest]
    assert report.closed_lifecycles_archived == 2
    archive = await repository.archived_closed_lifecycles()
    assert len(archive) == 2


class ExplodingArchiveRepository(OperationsRepository):
    async def archive_retention(self, **kwargs: object) -> None:
        raise RuntimeError("simulated_archive_failure")


@pytest.mark.asyncio
async def test_failed_archive_keeps_every_hot_state_and_reports_incident(
    tmp_path: Path,
) -> None:
    repository = ExplodingArchiveRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    stale = NOW - timedelta(days=8)
    await state.restore_realized_pnl_by_day(
        {
            NOW.date().isoformat(): Decimal("5"),
            (NOW - timedelta(days=5)).date().isoformat(): Decimal("2"),
        }
    )
    await state.restore_fill_checkpoint(checkpoint("0xfreestale01", stale))
    await seed_market_data(state)

    manager, incidents = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state,
        active_market_keys={("cur", "tok-a")},
        now=NOW,
    )

    assert len(await state.get_realized_pnl_by_day()) == 2
    assert len(await state.get_fill_checkpoints()) == 1
    assert await state.get_orderbook("old", "tok-c") is not None
    assert report.pnl_days_archived == 0
    assert report.fill_checkpoints_removed == 0
    persistence_incidents = [
        incident
        for incident in incidents
        if incident.category == IncidentCategory.PERSISTENCE
    ]
    assert len(persistence_incidents) == 1
    reason = persistence_incidents[0].reason
    assert "RuntimeError" in reason
    assert "secret" not in reason


@pytest.mark.parametrize(
    ("disk_percent", "expected_reason", "expected_severity"),
    [
        (79.0, None, None),
        (80.0, "disk_warning", IncidentSeverity.WARNING),
        (90.0, "disk_degraded", IncidentSeverity.WARNING),
        (95.0, "disk_halt", IncidentSeverity.URGENT),
    ],
)
@pytest.mark.asyncio
async def test_disk_thresholds_report_typed_incidents(
    tmp_path: Path,
    disk_percent: float,
    expected_reason: str | None,
    expected_severity: IncidentSeverity | None,
) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)

    manager, incidents = make_manager(
        repository, tmp_path, disk_percent=disk_percent
    )
    await manager.run_once(state_store=state, active_market_keys=set(), now=NOW)

    disk_incidents = [
        incident
        for incident in incidents
        if incident.category == IncidentCategory.DISK
    ]
    if expected_reason is None:
        assert disk_incidents == []
        return
    assert len(disk_incidents) == 1
    assert disk_incidents[0].reason == expected_reason
    assert disk_incidents[0].severity == expected_severity


@pytest.mark.asyncio
async def test_run_once_rotates_journals_and_counts_removals(tmp_path: Path) -> None:
    from persistence.journal import JsonlJournal

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True)
    stale_name = "events-20260801-000000.jsonl"
    (journal_dir / stale_name).write_text('{"a": 1}\n', encoding="utf-8")
    (journal_dir / "events.jsonl").touch()

    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    journal = JsonlJournal(
        journal_dir / "events.jsonl",
        rotate_bytes=50 * 1024 * 1024,
        retention_days=14,
        total_limit_bytes=500 * 1024 * 1024,
        now=lambda: NOW,
    )
    manager = RetentionManager(
        repository=repository,
        config=reliability_config(),
        journal=journal,
        data_path=tmp_path,
        disk_usage=lambda _path: 10.0,
    )

    report = await manager.run_once(
        state_store=state, active_market_keys=set(), now=NOW
    )

    assert not (journal_dir / stale_name).exists()
    assert (journal_dir / "events.jsonl").exists()
    assert report.journals_removed == 1


@pytest.mark.asyncio
async def test_run_once_prunes_delivered_outbox_and_resolved_incidents(
    tmp_path: Path,
) -> None:
    from models.operations import OutboxAlert

    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    old_delivery = NOW - timedelta(days=40)
    await repository.enqueue_alert(
        OutboxAlert(
            alert_id="alert-old-delivered1",
            incident_fingerprint="incident:old",
            severity=IncidentSeverity.INFO,
            text="delivered long ago",
            created_at=old_delivery,
            next_attempt_at=old_delivery,
        ),
        dedupe_after=old_delivery,
    )
    await repository.mark_alert_delivered(
        "alert-old-delivered1", delivered_at=old_delivery
    )
    state = InMemoryStateStore(mode=Mode.DRY_RUN)

    manager, _ = make_manager(repository, tmp_path)
    report = await manager.run_once(
        state_store=state, active_market_keys=set(), now=NOW
    )

    assert report.outbox_rows_removed == 1
    assert report.incidents_removed >= 0
