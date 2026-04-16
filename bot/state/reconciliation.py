"""Startup reconciliation service boundary."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from config.schema import Mode
from models.order import OrderResult
from pydantic import BaseModel, ConfigDict, Field
from state.store import InMemoryStateStore

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class OpenOrdersReader(Protocol):
    """Read-only subset of CLOB client used by reconciliation."""

    def get_open_orders(self, market_id: str | None = None) -> list[OrderResult]:
        """Return currently open orders from exchange."""


class ReconciliationReport(BaseModel):
    """Startup reconciliation result."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    mode: Mode
    checked_at: datetime = Field(default_factory=utc_now)
    local_open_orders: int = Field(ge=0)
    remote_open_orders: int = Field(ge=0)
    missing_on_remote: list[str] = Field(default_factory=list)
    missing_locally: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReconciliationService:
    """Compares local state to exchange truth on startup."""

    def __init__(
        self,
        *,
        state_store: InMemoryStateStore,
        mode: Mode,
        open_orders_reader: OpenOrdersReader | None = None,
    ) -> None:
        self._state_store = state_store
        self._mode = mode
        self._open_orders_reader = open_orders_reader

    async def reconcile_startup(self) -> ReconciliationReport:
        """Run conservative startup reconciliation."""

        local = await self._state_store.get_open_orders()
        local_ids = {item.client_order_id for item in local}

        remote: list[OrderResult] = []
        errors: list[str] = []
        if self._open_orders_reader is None:
            errors.append("open_orders_reader_not_configured")
        else:
            try:
                remote = self._open_orders_reader.get_open_orders()
            except Exception as exc:
                errors.append(f"remote_open_orders_fetch_failed:{exc}")

        remote_ids = {item.client_order_id for item in remote}
        missing_on_remote = sorted(local_ids - remote_ids)
        missing_locally = sorted(remote_ids - local_ids)

        for remote_order in remote:
            await self._state_store.set_order_status(remote_order)

        ok = not errors and not missing_on_remote
        if self._mode == Mode.LIVE and not ok:
            logger.error(
                "startup reconciliation failed in live mode",
                extra={
                    "component": "reconciliation",
                    "event_type": "reconciliation_failed",
                    "reason": ";".join(errors + ["missing_on_remote"]) if missing_on_remote else ";".join(errors),
                    "mode": self._mode.value,
                },
            )
        else:
            logger.info(
                "startup reconciliation completed",
                extra={
                    "component": "reconciliation",
                    "event_type": "reconciliation_completed",
                    "mode": self._mode.value,
                    "reason": "ok" if ok else "warnings",
                },
            )

        return ReconciliationReport(
            ok=ok if self._mode == Mode.LIVE else True,
            mode=self._mode,
            local_open_orders=len(local),
            remote_open_orders=len(remote),
            missing_on_remote=missing_on_remote,
            missing_locally=missing_locally,
            errors=errors,
        )
