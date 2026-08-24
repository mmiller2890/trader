"""Simple in-process event publisher/subscriber."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from models.events import BotEvent, EventType

logger = logging.getLogger(__name__)

EventHandler = Callable[[BotEvent], Awaitable[None]]


class EventBus:
    """Async in-process event bus."""

    def __init__(self) -> None:
        self._subscribers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._all_subscribers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler, *, event_type: EventType | None = None) -> None:
        """Register async event handler."""

        if event_type is None:
            self._all_subscribers.append(handler)
            return
        self._subscribers[event_type].append(handler)

    async def publish(self, event: BotEvent) -> None:
        """Fan out event to subscribers; a broken handler never breaks the bus."""

        handlers = [*self._all_subscribers, *self._subscribers.get(event.event_type, [])]
        if not handlers:
            return
        results = await asyncio.gather(
            *(handler(event) for handler in handlers), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.warning(
                    "event handler failed",
                    extra={
                        "component": "event_bus",
                        "event_type": "handler_failed",
                        "reason": type(result).__name__,
                    },
                )
