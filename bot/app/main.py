"""Application runtime entrypoint."""

from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime

from app.bootstrap import AppServices, bootstrap_app
from app.shutdown import shutdown_app
from config.schema import Mode
from models.events import BotEvent, EventType
from models.risk import RiskCheckResult


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


async def emit_event(services: AppServices, event: BotEvent) -> None:
    """Append and publish bot event."""

    await services.journal.append(event)
    await services.event_bus.publish(event)


async def housekeeping_loop(services: AppServices, stop_event: asyncio.Event) -> None:
    """Periodic runtime checks, timer hooks, and snapshots."""

    last_snapshot_at = utc_now()
    while not stop_event.is_set():
        await services.state_store.update_heartbeat("housekeeping")

        runtime_decision = await services.runtime_risk.evaluate_runtime()
        if not runtime_decision.approved:
            await services.state_store.set_kill_switch(True)
            event_type = (
                EventType.REPEATED_FAILURES
                if _has_check(runtime_decision.checks, "repeated_failures")
                else EventType.KILL_SWITCH_TRIPPED
            )
            await emit_event(
                services,
                BotEvent(
                    event_type=event_type,
                    component="runtime_risk",
                    mode=services.config.bot.mode.value,
                    message="runtime risk failure",
                    reason=runtime_decision.reason,
                ),
            )

        timer_signals = await services.strategy.on_timer()
        for signal_item in timer_signals:
            await services.router.route_signal(signal_item)

        if (utc_now() - last_snapshot_at).total_seconds() >= services.config.bot.snapshot_interval_seconds:
            await services.snapshots.save_from_state(services.state_store)
            last_snapshot_at = utc_now()

        await asyncio.sleep(services.config.bot.housekeeping_interval_seconds)


def _has_check(checks: list[RiskCheckResult], check_name: str) -> bool:
    return any(check.check_name == check_name and not check.passed for check in checks)


async def run() -> None:
    """Boot and run the main bot runtime."""

    services = bootstrap_app()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    report = await services.reconciliation.reconcile_startup()
    if services.config.bot.mode == Mode.LIVE and not report.ok:
        raise RuntimeError(f"live startup blocked by reconciliation: {report.model_dump_json()}")

    await services.state_store.update_heartbeat("app")
    await emit_event(
        services,
        BotEvent(
            event_type=EventType.BOT_STARTED,
            component="app",
            mode=services.config.bot.mode.value,
            message="bot started",
        ),
    )

    tasks: list[asyncio.Task[object]] = []
    if services.config.bot.mode in {Mode.DRY_RUN, Mode.LIVE}:
        await services.ws_manager.start()
    tasks.append(asyncio.create_task(housekeeping_loop(services, stop_event), name="housekeeping"))

    try:
        await stop_event.wait()
    finally:
        await shutdown_app(services, tasks)


def main() -> None:
    """Synchronous CLI entrypoint."""

    asyncio.run(run())


if __name__ == "__main__":
    main()
