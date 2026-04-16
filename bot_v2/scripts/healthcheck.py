"""Basic snapshot freshness healthcheck."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def main() -> int:
    """Exit 0 when snapshot is fresh enough, else 1."""

    snapshot_path = Path(os.getenv("BOT_SNAPSHOT_PATH", "data/snapshots/state.json"))
    max_age_seconds = float(os.getenv("BOT_HEALTHCHECK_MAX_AGE_SECONDS", "120"))

    if not snapshot_path.exists():
        return 1

    with snapshot_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    saved_at_raw = payload.get("saved_at")
    if not isinstance(saved_at_raw, str):
        return 1

    saved_at = datetime.fromisoformat(saved_at_raw.replace("Z", "+00:00"))
    if (utc_now() - saved_at).total_seconds() > max_age_seconds:
        return 1

    heartbeats = payload.get("heartbeats", {})
    market_data_heartbeat = heartbeats.get("market_data")
    if isinstance(market_data_heartbeat, str):
        heartbeat_at = datetime.fromisoformat(market_data_heartbeat.replace("Z", "+00:00"))
        if (utc_now() - heartbeat_at).total_seconds() > max_age_seconds:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
