"""Minimal SQLite storage adapter."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class KeyValueSqliteStore:
    """Tiny SQLite key-value helper for future durable metadata."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def set(self, key: str, value: str) -> None:
        """Upsert string value by key."""

        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            connection.commit()

    def get(self, key: str) -> str | None:
        """Fetch string value by key."""

        with sqlite3.connect(self._path) as connection:
            row = connection.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _initialize(self) -> None:
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL"
                ")"
            )
            connection.commit()
