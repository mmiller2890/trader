"""Rotating append-only JSONL event journal.

The active file is never deleted. Rotation happens by size or UTC day,
and maintenance removes rotated files older than the retention window
and enforces a total storage cap oldest-first.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from models.events import BotEvent

_ROTATED_PATTERN = re.compile(r"^events-(\d{8})(?:-\d{6})?\.jsonl$")


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class JsonlJournal:
    """File-backed append-only journal with size/date rotation."""

    def __init__(
        self,
        path: str | Path,
        *,
        rotate_bytes: int = 50 * 1024 * 1024,
        retention_days: int = 14,
        total_limit_bytes: int = 500 * 1024 * 1024,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_bytes = max(1, int(rotate_bytes))
        self._retention_days = max(0, int(retention_days))
        self._total_limit_bytes = max(1, int(total_limit_bytes))
        self._clock = now or _utc_now
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Journal path."""

        return self._path

    def set_clock(self, clock: Callable[[], datetime]) -> None:
        """Replace the injection clock (used by tests and soak runs)."""

        self._clock = clock

    async def append(self, event: BotEvent) -> None:
        """Append one event as JSONL, rotating first when needed."""

        payload = event.model_dump(mode="json")
        line = json.dumps(payload, sort_keys=True)

        def _write() -> None:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")

        async with self._lock:
            self._rotate_if_needed_locked()
            await asyncio.to_thread(_write)

    async def maintain(self, *, now: datetime | None = None) -> int:
        """Delete stale rotated files and enforce the total-size cap."""

        moment = now or self._clock()
        return await asyncio.to_thread(self._maintain_locked, moment)

    def _maintain_locked(self, moment: datetime) -> int:
        directory = self._path.parent
        active_name = self._path.name
        cutoff_date = moment.astimezone(UTC).date() - timedelta(
            days=self._retention_days
        )
        rotated: list[tuple[date, Path]] = []
        removed = 0
        for candidate in sorted(directory.glob("*.jsonl")):
            if candidate.name == active_name:
                continue
            stamp = self._stamp_date(candidate.name)
            if stamp is None:
                stamp = datetime.fromtimestamp(
                    candidate.stat().st_mtime, tz=UTC
                ).date()
            if stamp < cutoff_date:
                try:
                    candidate.unlink()
                    removed += 1
                except OSError:
                    continue
            else:
                rotated.append((stamp, candidate))

        def _total_size() -> int:
            total = self._path.stat().st_size if self._path.exists() else 0
            for _, candidate in rotated:
                total += candidate.stat().st_size
            return total

        while rotated and _total_size() > self._total_limit_bytes:
            rotated.sort(key=lambda item: item[0])
            oldest = rotated.pop(0)[1]
            try:
                oldest.unlink()
                removed += 1
            except OSError:
                break
        return removed

    def _rotate_if_needed_locked(self) -> None:
        if not self._path.exists():
            return
        stat = self._path.stat()
        today = self._clock().astimezone(UTC).date()
        modified_day = datetime.fromtimestamp(stat.st_mtime, tz=UTC).date()
        if stat.st_size < self._rotate_bytes and modified_day == today:
            return
        stamp = self._clock().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
        target = self._path.with_name(f"events-{stamp}.jsonl")
        counter = 0
        while target.exists():
            counter += 1
            target = self._path.with_name(f"events-{stamp}-{counter}.jsonl")
        self._path.replace(target)

    @staticmethod
    def _stamp_date(name: str) -> date | None:
        match = _ROTATED_PATTERN.match(name)
        if match is None:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").replace(
                tzinfo=UTC
            ).date()
        except ValueError:
            return None
