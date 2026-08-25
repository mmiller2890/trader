from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.healthcheck import main


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def write_snapshot(path: Path, *, saved_at: str, heartbeats: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"saved_at": saved_at, "heartbeats": heartbeats}),
        encoding="utf-8",
    )


def write_health_file(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def health_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "process_live": True,
        "service_ready": True,
        "trading_ready": True,
        "state": "running",
        "reason": None,
        "tasks": [],
        "websocket": {
            "connected": True,
            "task_running": True,
            "last_heartbeat": None,
            "disconnected_since": None,
            "connection_attempts": 0,
            "last_error": None,
        },
        "market_data_source": "websocket",
        "last_reconciliation_at": None,
        "outbox_pending": 0,
        "oldest_outbox_age_seconds": None,
        "disk_percent": 40.0,
        "lease_expires_at": None,
        "updated_at": utc_now().isoformat(),
    }
    values.update(overrides)
    return values


# --- liveness ---------------------------------------------------------------


def test_liveness_defaults_to_kind_liveness_and_passes_when_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    health_path = tmp_path / "runtime.json"
    write_health_file(health_path, health_payload())
    monkeypatch.setenv("BOT_HEALTH_PATH", str(health_path))
    assert main([]) == 0
    assert main(["--kind", "liveness"]) == 0


def test_liveness_fails_when_health_snapshot_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = (utc_now() - timedelta(seconds=600)).isoformat()
    health_path = tmp_path / "runtime.json"
    write_health_file(health_path, health_payload(updated_at=stale))
    monkeypatch.setenv("BOT_HEALTH_PATH", str(health_path))
    assert main(["--kind", "liveness"]) == 2


def test_liveness_fails_when_health_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_HEALTH_PATH", str(tmp_path / "missing.json"))
    assert main(["--kind", "liveness"]) == 2


# --- readiness --------------------------------------------------------------


@pytest.mark.parametrize(
    ("service_ready", "expected"),
    [(True, 0), (False, 2)],
)
def test_readiness_reports_service_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_ready: bool,
    expected: int,
) -> None:
    state_value = "running" if service_ready else "starting"
    health_path = tmp_path / "runtime.json"
    write_health_file(
        health_path,
        health_payload(service_ready=service_ready, state=state_value),
    )
    monkeypatch.setenv("BOT_HEALTH_PATH", str(health_path))
    assert main(["--kind", "readiness"]) == expected


# --- trading readiness ------------------------------------------------------


def test_trading_readiness_preserves_market_data_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_path = tmp_path / "state.json"
    write_snapshot(
        snapshot_path,
        saved_at=utc_now().isoformat(),
        heartbeats={"market_data": utc_now().isoformat()},
    )
    monkeypatch.setenv("BOT_SNAPSHOT_PATH", str(snapshot_path))
    assert main(["--kind", "trading"]) == 0


def test_trading_readiness_fails_when_market_data_heartbeat_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_path = tmp_path / "state.json"
    write_snapshot(
        snapshot_path,
        saved_at=utc_now().isoformat(),
        heartbeats={"app": utc_now().isoformat()},
    )
    monkeypatch.setenv("BOT_SNAPSHOT_PATH", str(snapshot_path))
    assert main(["--kind", "trading"]) == 2


def test_trading_readiness_fails_when_heartbeat_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_path = tmp_path / "state.json"
    stale = (utc_now() - timedelta(seconds=600)).isoformat()
    write_snapshot(snapshot_path, saved_at=utc_now().isoformat(), heartbeats={"market_data": stale})
    monkeypatch.setenv("BOT_SNAPSHOT_PATH", str(snapshot_path))
    assert main(["--kind", "trading"]) == 2


def test_unknown_kind_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_HEALTH_PATH", str(tmp_path / "missing.json"))
    with pytest.raises(SystemExit):
        main(["--kind", "bogus"])
