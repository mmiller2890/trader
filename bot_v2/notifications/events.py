"""Simple in-process event publisher/subscriber."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable

from models.events import BotEvent, EventType

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
        """Fan out event to subscribers."""

        handlers = [*self._all_subscribers, *self._subscribers.get(event.event_type, [])]
        if not handlers:
            return
        await asyncio.gather(*(handler(event) for handler in handlers))
