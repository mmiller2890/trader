"""Order lifecycle tracker."""

from __future__ import annotations

from datetime import UTC, datetime

from models.order import OrderResult
from models.position import FillApplication
from persistence.snapshots import SnapshotStore
from pydantic import BaseModel, ConfigDict, Field
from state.store import InMemoryStateStore, PositionAccountingError


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class TrackingOutcome(BaseModel):
    """Typed result of applying one order lifecycle update."""

    model_config = ConfigDict(extra="forbid")

    fill_applied: bool = False
    position_closed: bool = False
    unknown_outcome: bool = False
    accounting_error: str | None = None
    fill_application: FillApplication | None = None


class OrderTracker:
    """Updates runtime state from order lifecycle results."""

    def __init__(
        self,
        state_store: InMemoryStateStore,
        *,
        snapshots: SnapshotStore | None = None,
        confirmation_grace_seconds: float = 30,
    ) -> None:
        self._state_store = state_store
        self._snapshots = snapshots
        self._confirmation_grace_seconds = confirmation_grace_seconds

    async def handle_order_result(
        self,
        result: OrderResult,
        *,
        market_end_at: datetime | None = None,
    ) -> TrackingOutcome:
        """Apply latest order result to runtime state."""

        await self._state_store.set_order_status(result)
        await self._state_store.update_heartbeat("execution")

        outcome = TrackingOutcome()
        if result.status.value == "unknown":
            outcome.unknown_outcome = True
            return outcome
        if result.status.value not in {"filled", "partially_filled", "simulated"}:
            return outcome

        try:
            application = await self._state_store.apply_confirmed_fill(
                result,
                market_end_at=market_end_at,
                confirmed_at=utc_now(),
                confirmation_grace_seconds=self._confirmation_grace_seconds,
            )
        except PositionAccountingError as exc:
            outcome.accounting_error = str(exc)
            return outcome

        outcome.fill_application = application
        if application.duplicate:
            return outcome
        outcome.fill_applied = True
        if application.position is not None and application.position.quantity == 0:
            outcome.position_closed = True

        if self._snapshots is not None:
            await self._snapshots.save_from_state(self._state_store)
        return outcome
