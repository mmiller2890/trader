"""Order lifecycle tracker."""

from __future__ import annotations

from state.store import InMemoryStateStore
from models.order import OrderResult


class OrderTracker:
    """Updates runtime state from order lifecycle results."""

    def __init__(self, state_store: InMemoryStateStore) -> None:
        self._state_store = state_store

    async def handle_order_result(self, result: OrderResult) -> None:
        """Apply latest order result to runtime state."""

        await self._state_store.set_order_status(result)
        await self._state_store.update_heartbeat("execution")
