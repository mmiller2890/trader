"""Graceful shutdown helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.bootstrap import AppServices


async def shutdown_app(services: AppServices, tasks: Iterable[asyncio.Task[object]]) -> None:
    """Cancel tasks, persist snapshot, and stop clients."""

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await services.snapshots.save_from_state(services.state_store)
    await services.ws_manager.stop()
