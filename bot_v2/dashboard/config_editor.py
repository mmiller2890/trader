"""Stopped-only editor for the local operator configuration overlay."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.schema import Mode


class EditableConfig(BaseModel):
    """The only settings the dashboard is allowed to mutate."""

    model_config = ConfigDict(extra="forbid")

    subscribed_token_ids: list[str] = Field(default_factory=list, max_length=20)
    target_token_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("subscribed_token_ids", "target_token_ids")
    @classmethod
    def validate_token_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            token_id = str(value).strip()
            if not token_id or not token_id.isdecimal():
                raise ValueError("token IDs must be non-empty decimal strings")
            if token_id not in seen:
                normalized.append(token_id)
                seen.add(token_id)
        return normalized


class OperatorConfigEditor:
    """Atomically persist a strict local config overlay while stopped."""

    def __init__(self, path: str | Path, *, is_running: Callable[[], bool]) -> None:
        self._path = Path(path)
        self._is_running = is_running

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> EditableConfig:
        raw = self._load_payload()
        market_data = raw.get("market_data", {}) if isinstance(raw, dict) else {}
        strategy = raw.get("spike_strategy", {}) if isinstance(raw, dict) else {}
        return EditableConfig(
            subscribed_token_ids=market_data.get("subscribed_token_ids", []),
            target_token_ids=strategy.get("target_token_ids", []),
        )

    def save(self, config: EditableConfig) -> EditableConfig:
        if self._is_running():
            raise RuntimeError("bot_must_be_stopped")
        normalized = EditableConfig.model_validate(config.model_dump())
        payload = self._load_payload()
        payload["market_data"] = {
            "subscribed_token_ids": normalized.subscribed_token_ids,
        }
        payload["spike_strategy"] = {
            "target_token_ids": normalized.target_token_ids,
        }
        self._write_payload(payload)
        return normalized

    def set_mode(self, mode: Mode) -> Mode:
        """Atomically persist one internally consistent runtime-mode bundle."""

        if self._is_running():
            raise RuntimeError("bot_must_be_stopped")
        selected = Mode(mode)
        if selected not in {Mode.DRY_RUN, Mode.LIVE}:
            raise ValueError("dashboard mode must be dry_run or live")
        live = selected == Mode.LIVE
        payload = self._load_payload()
        payload["bot"] = {"mode": selected.value}
        payload["execution"] = {
            "allow_live_trading": live,
            "dry_run_force": not live,
        }
        self._write_payload(payload)
        return selected

    def _load_payload(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError("operator config must be a mapping")
        return raw

    def _write_payload(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            delete=False,
        ) as handle:
            yaml.safe_dump(payload, handle, sort_keys=True)
            temporary_path = Path(handle.name)
        temporary_path.replace(self._path)
