"""Operator-facing application service for dashboard commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.runtime import BotRuntime, ControlResult, RuntimePhase, RuntimeStatus
from clients.auth import build_clob_credentials, is_live_trading_enabled
from config.loader import load_config
from config.schema import AppConfig, Mode
from dashboard.config_editor import EditableConfig, OperatorConfigEditor
from dashboard.models import (
    DashboardState,
    EventTail,
    PreflightCheckView,
    PreflightView,
    ReadinessItem,
)
from dashboard.preflight import PreflightRunner
from dashboard.read_model import DashboardReadModel, tail_events


class ConfirmationError(ValueError):
    pass


class PreflightBusyError(RuntimeError):
    pass


class RuntimeConflictError(RuntimeError):
    pass


def secret_redactions(config: AppConfig) -> list[str]:
    """Return raw secret values solely for in-memory output scrubbing."""

    candidates = (
        config.secrets.private_key,
        config.secrets.clob_api_key,
        config.secrets.clob_secret,
        config.secrets.clob_passphrase,
        config.secrets.telegram_bot_token,
    )
    redactions = [value.get_secret_value() for value in candidates if value is not None]
    try:
        funder = build_clob_credentials(config).proxy_address
    except ValueError:
        funder = None
    if funder:
        redactions.append(funder)
    return list(dict.fromkeys(redactions))


class DashboardController:
    def __init__(
        self,
        *,
        runtime: BotRuntime,
        config_dir: str | Path,
        data_dir: str | Path,
        config_loader: Any = load_config,
        preflight: Any | None = None,
        now: Callable[[], datetime] | None = None,
        preflight_ttl_seconds: float = 300,
    ) -> None:
        self.runtime = runtime
        self._config_dir = Path(config_dir)
        self._data_dir = Path(data_dir)
        self._config_loader = config_loader
        self._editor = OperatorConfigEditor(
            self._config_dir / "operator.yaml",
            is_running=lambda: self.runtime.status().phase
            not in {RuntimePhase.STOPPED, RuntimePhase.FAILED},
        )
        self._preflight = preflight or PreflightRunner(
            self._config_dir,
            redactions=secret_redactions(self._config_loader(self._config_dir)),
        )
        self._preflight_lock = asyncio.Lock()
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._preflight_ttl_seconds = preflight_ttl_seconds
        self._config_generation = 0
        self._preflight_fingerprint: str | None = None
        self.last_preflight = PreflightView(
            ok=False,
            status="not_run",
            reason="preflight_not_run",
        )

    def config(self) -> AppConfig:
        return self._config_loader(self._config_dir)

    async def state(self) -> DashboardState:
        config = self.config()
        state = await DashboardReadModel(
            config=config,
            runtime=self.runtime,
            data_dir=self._data_dir,
        ).build()
        preflight_fresh = self._preflight_is_fresh()
        checked_at = self.last_preflight.checked_at
        preflight_expires_at = (
            checked_at.astimezone(UTC)
            + timedelta(seconds=self._preflight_ttl_seconds)
            if checked_at is not None
            and checked_at.tzinfo is not None
            and checked_at.utcoffset() is not None
            else None
        )
        live_armed = is_live_trading_enabled(config)
        live_start_ready = preflight_fresh and live_armed
        readiness = [
            (
                ReadinessItem(
                    name="live_start",
                    passed=live_start_ready,
                    reason=(
                        "live_start_ready"
                        if live_start_ready
                        else "fresh_preflight_required"
                        if not preflight_fresh
                        else "enable_live_mode"
                    ),
                )
                if item.name == "live_start"
                else item
            )
            for item in state.readiness
        ]
        return state.model_copy(
            update={
                "preflight": self.last_preflight,
                "preflight_fresh": preflight_fresh,
                "preflight_expires_at": preflight_expires_at,
                "live_armed": live_armed,
                "live_start_ready": live_start_ready,
                "readiness": readiness,
            }
        )

    def events(self, *, limit: int = 100) -> EventTail:
        return tail_events(
            self._data_dir / "journal" / "events.jsonl",
            limit=limit,
            redactions=secret_redactions(self.config()),
        )

    async def start(self, confirmation: str | None = None) -> RuntimeStatus:
        config = self.config()
        allow_live = config.bot.mode == Mode.LIVE
        if allow_live:
            if not self._preflight_is_fresh():
                raise RuntimeConflictError("fresh_preflight_required")
            if confirmation != "START LIVE":
                raise ConfirmationError("confirmation must be exactly START LIVE")
        try:
            status = await self.runtime.start(self._config_dir, allow_live=allow_live)
        except Exception:
            if allow_live:
                self._invalidate_preflight("live_start_failed")
            raise
        if status.phase.value == "failed":
            if allow_live:
                self._invalidate_preflight_from_runtime(status.reason)
            raise RuntimeConflictError(status.reason or "bot_start_failed")
        return status

    async def stop(self) -> RuntimeStatus:
        return await self.runtime.stop()

    async def halt(self, confirmation: str) -> RuntimeStatus:
        if confirmation != "HALT":
            raise ConfirmationError("confirmation must be exactly HALT")
        return await self.runtime.emergency_halt(confirmation)

    async def cancel_all(self, confirmation: str) -> ControlResult:
        if confirmation != "CANCEL ALL":
            raise ConfirmationError("confirmation must be exactly CANCEL ALL")
        return await self.runtime.cancel_all(confirmation)

    async def run_preflight(self) -> PreflightView:
        if self._preflight_lock.locked():
            raise PreflightBusyError("preflight_already_running")
        async with self._preflight_lock:
            generation = self._config_generation
            fingerprint = self._config_fingerprint()
            set_redactions = getattr(self._preflight, "set_redactions", None)
            if callable(set_redactions):
                set_redactions(secret_redactions(self.config()))
            self.last_preflight = PreflightView(
                ok=False,
                status="running",
                reason="preflight_running",
            )
            try:
                result = await self._preflight.run()
            except Exception as exc:
                result = PreflightView(
                    ok=False,
                    status="failed",
                    reason=f"preflight_runner_failed:{type(exc).__name__}",
                    checked_at=self._now(),
                )
            if result.checked_at is None:
                result = result.model_copy(update={"checked_at": self._now()})
            if (
                generation != self._config_generation
                or fingerprint != self._config_fingerprint()
            ):
                self._invalidate_preflight(
                    "preflight_invalidated_by_config_change"
                )
                return self.last_preflight
            self.last_preflight = result
            self._preflight_fingerprint = fingerprint if result.ok else None
            return self.last_preflight

    def set_mode(self, mode: Mode, confirmation: str | None = None) -> Mode:
        selected = Mode(mode)
        if selected == Mode.LIVE:
            if not self._preflight_is_fresh():
                raise RuntimeConflictError("fresh_preflight_required")
            if confirmation != "ENABLE LIVE":
                raise ConfirmationError("confirmation must be exactly ENABLE LIVE")
        return self._editor.set_mode(selected)

    def _preflight_is_fresh(self) -> bool:
        checked_at = self.last_preflight.checked_at
        if (
            not self.last_preflight.ok
            or self.last_preflight.status != "passed"
            or checked_at is None
        ):
            return False
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            return False
        age = (self._now() - checked_at.astimezone(UTC)).total_seconds()
        try:
            fingerprint_matches = (
                self._preflight_fingerprint is not None
                and self._preflight_fingerprint == self._config_fingerprint()
            )
        except Exception:
            return False
        return fingerprint_matches and 0 <= age <= self._preflight_ttl_seconds

    def _config_fingerprint(self) -> str:
        """Hash preflight-relevant config, including secrets, without exposing it."""

        config = self.config()
        payload = config.model_dump(mode="json")
        payload["bot"]["mode"] = "operator_controlled"
        payload["execution"]["allow_live_trading"] = "operator_controlled"
        payload["execution"]["dry_run_force"] = "operator_controlled"
        payload["secrets"] = {
            "private_key": (
                config.secrets.private_key.get_secret_value()
                if config.secrets.private_key
                else None
            ),
            "clob_api_key": (
                config.secrets.clob_api_key.get_secret_value()
                if config.secrets.clob_api_key
                else None
            ),
            "clob_secret": (
                config.secrets.clob_secret.get_secret_value()
                if config.secrets.clob_secret
                else None
            ),
            "clob_passphrase": (
                config.secrets.clob_passphrase.get_secret_value()
                if config.secrets.clob_passphrase
                else None
            ),
            "polymarket_proxy_address": config.secrets.polymarket_proxy_address,
            "rpc_url": config.secrets.rpc_url,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _invalidate_preflight(self, reason: str) -> None:
        self.last_preflight = PreflightView(
            ok=False,
            status="failed" if reason.startswith("live_") else "not_run",
            reason=reason,
            checked_at=self._now() if reason.startswith("live_") else None,
        )
        self._preflight_fingerprint = None

    def _invalidate_preflight_from_runtime(self, reason: str | None) -> None:
        safe_reason = reason or "live_start_failed"
        checks: list[PreflightCheckView] = []
        prefix = "live_preflight_failed:"
        if safe_reason.startswith(prefix):
            checks = [
                PreflightCheckView(
                    name=name,
                    passed=False,
                    reason="bootstrap_preflight_failed",
                )
                for name in safe_reason.removeprefix(prefix).split(",")
                if name
            ]
        self._invalidate_preflight(safe_reason)
        if checks:
            self.last_preflight = self.last_preflight.model_copy(
                update={"checks": checks}
            )

    def get_config(self) -> EditableConfig:
        config = self.config()
        return EditableConfig(
            subscribed_token_ids=config.market_data.subscribed_token_ids,
            target_token_ids=config.spike_strategy.target_token_ids,
        )

    def save_config(self, payload: EditableConfig | dict[str, object]) -> EditableConfig:
        if self.config().market_data.automatic_market.enabled:
            raise RuntimeError("automatic_market_owns_token_scope")
        editable = (
            payload
            if isinstance(payload, EditableConfig)
            else EditableConfig.model_validate(payload)
        )
        self._config_generation += 1
        self._invalidate_preflight("preflight_invalidated_by_config_change")
        saved = self._editor.save(editable)
        return saved
