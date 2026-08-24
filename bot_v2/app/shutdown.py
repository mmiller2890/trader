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
            reason = f"cancel_all_failed:{type(exc).__name__}"
            logger.critical(
                "cancel-all failed during shutdown",
                extra={
                    "component": "shutdown",
                    "event_type": "cancel_all_failed",
                    "reason": reason,
                },
            )
            await _persist_cancel_failure(services, reason)

    cleanup_failures: list[str] = []
    try:
        await services.snapshots.save_from_state(services.state_store)
    except Exception as exc:
        cleanup_failures.append(f"snapshot:{type(exc).__name__}")
    market_rotator = getattr(services, "market_rotator", None)
    if market_rotator is not None:
        try:
            await market_rotator.stop()
        except Exception as exc:
            cleanup_failures.append(f"market_rotator:{type(exc).__name__}")
    try:
        await services.ws_manager.stop()
    except Exception as exc:
        cleanup_failures.append(f"websocket:{type(exc).__name__}")
    if cleanup_failures:
        raise RuntimeError(
            "shutdown_cleanup_failed:" + ",".join(cleanup_failures)
        )
