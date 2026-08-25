"""Deployment healthcheck over distinct liveness/readiness/trading kinds."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def _load_health_snapshot(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _liveness(health_path: Path, max_age_seconds: float) -> int:
    payload = _load_health_snapshot(health_path)
    if payload is None:
        return 2
    updated_at_raw = payload.get("updated_at")
    if not isinstance(updated_at_raw, str):
        return 2
    try:
        updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return 2
    if (utc_now() - updated_at).total_seconds() > max_age_seconds:
        return 2
    if payload.get("process_live") is False:
        return 2
    return 0


def _readiness(health_path: Path, max_age_seconds: float) -> int:
    payload = _load_health_snapshot(health_path)
    if payload is None:
        return 2
    updated_at_raw = payload.get("updated_at")
    if isinstance(updated_at_raw, str):
        try:
            updated_at = datetime.fromisoformat(
                updated_at_raw.replace("Z", "+00:00")
            )
            if (utc_now() - updated_at).total_seconds() > max_age_seconds * 4:
                return 2
        except ValueError:
            return 2
    return 0 if payload.get("service_ready") is True else 2


def _trading(snapshot_path: Path, max_age_seconds: float) -> int:
    """Preserve the original market-data freshness checks."""

    if not snapshot_path.exists():
        return 2

    try:
        with snapshot_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return 2

    saved_at_raw = payload.get("saved_at")
    if not isinstance(saved_at_raw, str):
        return 2

    saved_at = datetime.fromisoformat(saved_at_raw.replace("Z", "+00:00"))
    if (utc_now() - saved_at).total_seconds() > max_age_seconds:
        return 2

    heartbeats = payload.get("heartbeats", {})
    market_data_heartbeat = (
        heartbeats.get("market_data") if isinstance(heartbeats, dict) else None
    )
    if not isinstance(market_data_heartbeat, str):
        return 2
    heartbeat_at = datetime.fromisoformat(
        market_data_heartbeat.replace("Z", "+00:00")
    )
    if (utc_now() - heartbeat_at).total_seconds() > max_age_seconds:
        return 2

    return 0


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the selected health kind is satisfied, else 2."""

    parser = argparse.ArgumentParser(description="Bot deployment healthcheck")
    parser.add_argument(
        "--kind",
        choices=("liveness", "readiness", "trading"),
        default="liveness",
    )
    args = parser.parse_args(argv)

    max_age_seconds = float(os.getenv("BOT_HEALTHCHECK_MAX_AGE_SECONDS", "120"))
    snapshot_path = Path(os.getenv("BOT_SNAPSHOT_PATH", "data/snapshots/state.json"))
    health_path = Path(os.getenv("BOT_HEALTH_PATH", "data/health/runtime.json"))

    if args.kind == "liveness":
        return _liveness(health_path, max_age_seconds)
    if args.kind == "readiness":
        return _readiness(health_path, max_age_seconds)
    return _trading(snapshot_path, max_age_seconds)


if __name__ == "__main__":
    sys.exit(main())
