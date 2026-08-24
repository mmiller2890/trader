from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from persistence.journal import JsonlJournal
from models.events import BotEvent, EventType


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def event(index: int) -> BotEvent:
    return BotEvent(
        event_type=EventType.BOT_STARTED,
        component="test",
        mode="dry_run",
        message=f"event {index} " + "x" * 20,
    )


@pytest.mark.asyncio
async def test_journal_rotates_by_size_and_preserves_complete_json_lines(
    tmp_path: Path,
) -> None:
    journal = JsonlJournal(
        tmp_path / "journal" / "events.jsonl",
        rotate_bytes=180,
        retention_days=14,
        total_limit_bytes=1_000_000,
        now=lambda: NOW,
    )
    for index in range(20):
        await journal.append(event(index))
    await journal.maintain(now=NOW)
    rows = []
    for path in sorted((tmp_path / "journal").glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_journal_rotates_daily(tmp_path: Path) -> None:
    journal = JsonlJournal(
        tmp_path / "journal" / "events.jsonl",
        rotate_bytes=10_000_000,
        retention_days=14,
        total_limit_bytes=100_000_000,
        now=lambda: NOW,
    )
    await journal.append(event(0))
    day_two = NOW + timedelta(days=1)
    journal.set_clock(lambda: day_two)
    await journal.append(event(1))
    rotated = list((tmp_path / "journal").glob("*.jsonl"))
    assert len(rotated) >= 1


@pytest.mark.asyncio
async def test_journal_retention_removes_old_files(tmp_path: Path) -> None:
    old_date = NOW - timedelta(days=15)

    async def write_old(event: BotEvent) -> None:
        pass

    journal = JsonlJournal(
        tmp_path / "journal" / "events.jsonl",
        rotate_bytes=50,
        retention_days=14,
        total_limit_bytes=1_000_000,
        now=lambda: NOW,
    )
    for index in range(5):
        await journal.append(event(index))

    stale_dir = tmp_path / "journal"
    stale_name = f"events-{old_date.strftime('%Y%m%d')}.jsonl"
    (stale_dir / stale_name).write_text('{"stale": true}\n', encoding="utf-8")

    await journal.maintain(now=NOW)
    assert not (stale_dir / stale_name).exists()


@pytest.mark.asyncio
async def test_total_cap_removes_oldest_rotated_files_first(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True)
    for day in ("20260820", "20260821", "20260822"):
        (journal_dir / f"events-{day}-000000.jsonl").write_text("x" * 400)
    active = journal_dir / "events.jsonl"
    active.write_text("x" * 100, encoding="utf-8")

    journal = JsonlJournal(
        journal_dir / "events.jsonl",
        rotate_bytes=10_000,
        retention_days=14,
        total_limit_bytes=1_000,
        now=lambda: NOW,
    )
    removed = await journal.maintain(now=NOW)

    files = list(journal_dir.glob("*.jsonl"))
    assert active.exists()
    total = sum(item.stat().st_size for item in files)
    assert total <= 1_000
    assert removed == 1
    remaining = {item.name for item in files}
    assert "events-20260822-000000.jsonl" in remaining
    assert "events-20260820-000000.jsonl" not in remaining


@pytest.mark.asyncio
async def test_active_file_is_never_deleted_by_maintenance(
    tmp_path: Path,
) -> None:
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True)
    active = journal_dir / "events.jsonl"
    active.write_text("x" * 2_000, encoding="utf-8")

    journal = JsonlJournal(
        active,
        rotate_bytes=10_000,
        retention_days=14,
        total_limit_bytes=500,
        now=lambda: NOW,
    )
    removed = await journal.maintain(now=NOW)

    assert active.exists()
    assert active.read_text(encoding="utf-8") == "x" * 2_000
    assert removed == 0
