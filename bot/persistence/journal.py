"""Append-only JSONL event journal."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from models.events import BotEvent


class JsonlJournal:
    """File-backed append-only journal."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """Journal path."""

        return self._path

    async def append(self, event: BotEvent) -> None:
        """Append one event as JSONL."""

        payload = event.model_dump(mode="json")
        line = json.dumps(payload, sort_keys=True)
        async with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.write("\n")
