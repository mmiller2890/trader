from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.healthcheck import main


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def write_snapshot(path: Path, *, saved_at: str, heartbeats: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"saved_at": saved_at, "heartbeats": heartbeats}),
        encoding="utf-8",
    )


def test_healthcheck_fails_when_market_data_heartbeat_is_missing(
    tmp_path: Path, monkeypatch: object
) -> None:
    snapshot_path = tmp_path / "state.json"
    write_snapshot(
        snapshot_path,
        saved_at=utc_now().isoformat(),
        heartbeats={"app": utc_now().isoformat()},
    )
    monkeypatch.setenv("BOT_SNAPSHOT_PATH", str(snapshot_path))
    assert main() == 1


def test_healthcheck_passes_with_fresh_snapshot_and_heartbeat(
    tmp_path: Path, monkeypatch: object
) -> None:
    snapshot_path = tmp_path / "state.json"
    write_snapshot(
        snapshot_path,
        saved_at=utc_now().isoformat(),
        heartbeats={"market_data": utc_now().isoformat()},
    )
    monkeypatch.setenv("BOT_SNAPSHOT_PATH", str(snapshot_path))
    assert main() == 0


def test_healthcheck_fails_when_heartbeat_is_stale(
    tmp_path: Path, monkeypatch: object
) -> None:
    snapshot_path = tmp_path / "state.json"
    stale = (utc_now() - timedelta(seconds=600)).isoformat()
    write_snapshot(
        snapshot_path,
        saved_at=utc_now().isoformat(),
        heartbeats={"market_data": stale},
    )
    monkeypatch.setenv("BOT_SNAPSHOT_PATH", str(snapshot_path))
    assert main() == 1
