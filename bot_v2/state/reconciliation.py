"""Startup reconciliation service boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from config.schema import Mode
from models.order import OrderResult, OrderStatus
from models.position import Position
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


class PositionsReader(Protocol):
    """Read-only subset of the Data API client used by reconciliation."""

    def get_positions(self, user_address: str) -> list[Position]:
        """Return current positions for a user address."""


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
    deferred_positions: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReconciliationService:
    """Compares local state to exchange truth on startup."""

    def __init__(
        self,
        *,
        state_store: InMemoryStateStore,
        mode: Mode,
        open_orders_reader: OpenOrdersReader | None = None,
        positions_reader: PositionsReader | None = None,
        funder_address: str | None = None,
        now: Callable[[], datetime] = utc_now,
        apply_fill: Callable[[OrderResult], Awaitable[object]] | None = None,
        market_end_lookup: Callable[[str, str], datetime | None] | None = None,
        require_position_market_end: bool = False,
        min_order_size: Decimal = Decimal("1"),
        min_size_provider: Callable[[str], Decimal] | None = None,
    ) -> None:
        self._state_store = state_store
        self._mode = mode
        self._open_orders_reader = open_orders_reader
        self._positions_reader = positions_reader
        self._funder_address = funder_address
        self._now = now
        self._apply_fill = apply_fill
        self._market_end_lookup = market_end_lookup
        self._require_position_market_end = require_position_market_end
        self._min_order_size = min_order_size
        self._min_size_provider = min_size_provider

    async def reconcile_startup(self) -> ReconciliationReport:
        """Run conservative startup reconciliation."""

        return await self._reconcile(authoritative_positions=False)

    async def reconcile_runtime(self) -> ReconciliationReport:
        """Refresh mutable account state while conservatively resolving orders."""

        return await self._reconcile(authoritative_positions=True)

    async def _reconcile(
        self,
        *,
        authoritative_positions: bool,
    ) -> ReconciliationReport:
        """Reconcile local state against current exchange reads."""

        local = await self._state_store.get_open_orders()
        local_by_identity = {_order_identity(item): item for item in local}
        local_ids = set(local_by_identity)

        remote: list[OrderResult] = []
        errors: list[str] = []
        if self._open_orders_reader is None:
            errors.append("open_orders_reader_not_configured")
        else:
            try:
                remote = await asyncio.to_thread(
                    self._open_orders_reader.get_open_orders
                )
            except Exception as exc:
                errors.append(
                    f"remote_open_orders_fetch_failed:{type(exc).__name__}"
                )

        remote_ids = {_order_identity(item) for item in remote}
        missing_on_remote = sorted(local_ids - remote_ids)
        missing_locally = sorted(remote_ids - local_ids)

        unresolved_missing: list[str] = []
        get_order = getattr(self._open_orders_reader, "get_order", None)
        for order_id in missing_on_remote:
            local_order = local_by_identity[order_id]
            if not callable(get_order):
                unresolved_missing.append(order_id)
                continue
            try:
                latest = await asyncio.to_thread(
                    get_order,
                    order_id,
                    client_order_id=local_order.client_order_id,
                )
            except Exception as exc:
                errors.append(
                    f"remote_order_fetch_failed:{order_id}:{type(exc).__name__}"
                )
                unresolved_missing.append(order_id)
                continue
            if latest.status in {
                OrderStatus.CANCELLED,
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.REJECTED,
                OrderStatus.FAILED,
            }:
                accounted = await self._apply_confirmed_fill(latest, errors)
                if latest.status not in {
                    OrderStatus.FILLED,
                    OrderStatus.PARTIALLY_FILLED,
                } or accounted:
                    await self._state_store.set_order_status(latest)
                else:
                    unresolved_missing.append(order_id)
            else:
                unresolved_missing.append(order_id)
        missing_on_remote = unresolved_missing

        for remote_order in remote:
            identity = _order_identity(remote_order)
            local_order = local_by_identity.get(identity)
            reconciled_order = (
                remote_order.model_copy(
                    update={"client_order_id": local_order.client_order_id}
                )
                if local_order is not None
                else remote_order
            )
            if reconciled_order.status in {
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
            } and reconciled_order.filled_size > 0:
                accounted = await self._apply_confirmed_fill(reconciled_order, errors)
                if not accounted:
                    continue
            await self._state_store.set_order_status(reconciled_order)

        deferred_positions: list[str] = []
        if self._positions_reader is not None:
            positions_fetch_succeeded = False
            try:
                remote_positions = await asyncio.to_thread(
                    self._positions_reader.get_positions,
                    self._funder_address or "",
                )
                positions_fetch_succeeded = True
            except Exception as exc:
                errors.append(
                    f"remote_positions_fetch_failed:{type(exc).__name__}"
                )
                remote_positions = []
            local_positions = await self._state_store.get_positions()
            if positions_fetch_succeeded and (
                authoritative_positions or not local_positions
            ):
                merge = await self._state_store.merge_authoritative_positions(
                    remote_positions,
                    now=self._now(),
                    market_end_lookup=self._market_end_lookup,
                    # Below the venue's minimum order size no order can close
                    # the gap, so a difference there is dust rather than a
                    # divergence and must not be raised on every pass. The floor
                    # is published per market, so resolve it per market and fall
                    # back to the configured value when the lookup fails.
                    dust_threshold=self._min_order_size,
                    dust_threshold_for=self._min_size_provider,
                )
                deferred_positions = merge.deferred_keys
                for key in merge.expired_keys:
                    errors.append(f"position_confirmation_timeout:{key}")
                if self._require_position_market_end:
                    sellable = {
                        f"{position.market_id}:{position.token_id}"
                        for position in remote_positions
                        if position.quantity >= self._min_order_size
                    }
                    for key in merge.unknown_market_keys:
                        if key in sellable:
                            errors.append(f"position_market_window_unknown:{key}")
            elif positions_fetch_succeeded and not await self._positions_match(
                local_positions,
                remote_positions,
            ):
                errors.append("position_mismatch")

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
            deferred_positions=deferred_positions,
            errors=errors,
        )

    async def _apply_confirmed_fill(
        self,
        result: OrderResult,
        errors: list[str],
    ) -> bool:
        """Route one confirmed exchange fill into position accounting."""

        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED}:
            return True
        if result.filled_size <= 0 or result.avg_fill_price is None:
            errors.append("confirmed_fill_missing_accounting_fields")
            return False
        if self._apply_fill is None:
            errors.append("fill_recorder_not_configured")
            return False
        try:
            outcome = await self._apply_fill(result)
        except Exception as exc:
            errors.append(f"fill_application_failed:{type(exc).__name__}")
            return False
        accounting_error = getattr(outcome, "accounting_error", None)
        if accounting_error is not None:
            errors.append(f"fill_accounting_error:{accounting_error}")
            return False
        return True

    async def _positions_match(
        self,
        local: list[Position],
        remote: list[Position],
    ) -> bool:
        """
        Compare local and remote holdings, ignoring differences no order could
        ever close.

        Two kinds of local-only position are not divergences. Inventory below
        the venue's minimum order size cannot be sold by anyone. And a position
        whose market has already ended has settled: the data API is queried
        with redeemable=false, so a resolved position leaves the remote read
        entirely while local still holds it. Treating either as a mismatch
        blocks live start over state that no action can reconcile.
        """

        local_map = {
            (position.market_id, position.token_id): position.quantity
            for position in local
        }
        remote_map = {
            (position.market_id, position.token_id): position.quantity
            for position in remote
        }
        if local_map == remote_map:
            return True

        now = self._now()
        for key, quantity in list(local_map.items()):
            if key in remote_map:
                continue
            threshold = self._min_order_size
            if self._min_size_provider is not None:
                try:
                    threshold = max(threshold, self._min_size_provider(key[0]))
                except Exception:
                    threshold = self._min_order_size
            if quantity < threshold:
                local_map.pop(key)
                continue
            lifecycle = await self._state_store.get_position_lifecycle(*key)
            market_end_at = getattr(lifecycle, "market_end_at", None)
            if market_end_at is not None and now >= market_end_at:
                local_map.pop(key)
        return local_map == remote_map


def _order_identity(order: OrderResult) -> str:
    """Use the exchange identifier when one has been assigned."""

    return order.exchange_order_id or order.client_order_id
