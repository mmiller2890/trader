from __future__ import annotations

import asyncio

import pytest

from app.process_services import ProcessReliabilityServices


@pytest.mark.asyncio
async def test_process_services_own_one_notification_worker_lifecycle() -> None:
    calls: list[str] = []
    started = asyncio.Event()

    class Worker:
        async def run(self, stop_event: asyncio.Event, heartbeat) -> None:
            calls.append("worker_start")
            await heartbeat()
            started.set()
            await stop_event.wait()
            calls.append("worker_stop")

    class Telegram:
        async def close(self) -> None:
            calls.append("telegram_close")

    services = ProcessReliabilityServices(
        repository=object(),
        leases=object(),
        alerts=object(),
        telegram=Telegram(),
        notification_worker=Worker(),
    )

    await services.start()
    await services.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await services.close()
    await services.close()

    assert calls == ["worker_start", "worker_stop", "telegram_close"]
