"""Process-owned reliability services shared with the trading runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from config.schema import AppConfig
from notifications.outbox import AlertService, NotificationWorker, TelegramTransport
from persistence.operations import OperationsRepository
from reliability.lease import LiveLeaseService


@dataclass(slots=True)
class ProcessReliabilityServices:
    """One process-wide owner for the durable alert and lease graph."""

    repository: OperationsRepository
    leases: LiveLeaseService
    alerts: AlertService
    telegram: TelegramTransport
    notification_worker: NotificationWorker
    _stop_event: asyncio.Event | None = field(default=None, init=False)
    _worker_task: asyncio.Task[None] | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    async def start(self) -> None:
        """Start the one process-owned outbox delivery worker."""

        if self._closed:
            raise RuntimeError("process_reliability_services_closed")
        if self._worker_task is not None:
            return
        self._stop_event = asyncio.Event()

        async def heartbeat() -> None:
            await asyncio.sleep(0)

        self._worker_task = asyncio.create_task(
            self.notification_worker.run(self._stop_event, heartbeat),
            name="process-notification-worker",
        )

    async def close(self) -> None:
        """Stop alert delivery and close its transport exactly once."""

        if self._closed:
            return
        self._closed = True
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            if self._worker_task is not None:
                await self._worker_task
        finally:
            await self.telegram.close()


def build_process_reliability_services(
    *, config: AppConfig, data_dir: Path
) -> ProcessReliabilityServices:
    """Build exactly one reliability service graph for a bot process."""

    repository = OperationsRepository(data_dir / "bot.sqlite3")
    telegram = TelegramTransport(config)
    return ProcessReliabilityServices(
        repository=repository,
        leases=LiveLeaseService(
            repository,
            live_lease_hours=config.reliability.live_lease_hours,
        ),
        alerts=AlertService(repository, config),
        telegram=telegram,
        notification_worker=NotificationWorker(repository, telegram, config),
    )
