"""Graceful shutdown helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from app.bootstrap import AppServices
from config.schema import Mode
from models.events import BotEvent, EventType

logger = logging.getLogger(__name__)


async def _persist_cancel_failure(services: AppServices, reason: str) -> None:
    await services.journal.append(
        BotEvent(
            event_type=EventType.KILL_SWITCH_TRIPPED,
            component="shutdown",
            mode=services.config.bot.mode.value,
            message="cancel-all failure during shutdown",
            reason=reason,
        )
    )


async def shutdown_app(services: AppServices, tasks: Iterable[asyncio.Task[object]]) -> None:
    """Cancel tasks, cancel live orders, persist snapshot, and stop clients."""

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if services.config.bot.mode == Mode.LIVE:
        try:
            await asyncio.wait_for(
                services.submitter.cancel_all_open_orders(),
                timeout=services.config.bot.shutdown_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.critical(
                "cancel-all timed out during shutdown",
                extra={
                    "component": "shutdown",
                    "event_type": "cancel_all_timeout",
                    "reason": f"timeout={services.config.bot.shutdown_timeout_seconds}s",
                },
            )
            await _persist_cancel_failure(services, "cancel_all_timeout")
        except Exception as exc:
            logger.critical(
                "cancel-all failed during shutdown",
                extra={
                    "component": "shutdown",
                    "event_type": "cancel_all_failed",
                    "reason": str(exc),
                },
            )
            await _persist_cancel_failure(services, f"cancel_all_failed:{exc}")

    await services.snapshots.save_from_state(services.state_store)
    await services.ws_manager.stop()
