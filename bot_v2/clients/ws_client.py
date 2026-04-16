"""Resilient async websocket manager boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Awaitable, Callable

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict | str], Awaitable[None]]


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class WebSocketManager:
    """Reconnect-capable websocket client with async callbacks."""

    def __init__(
        self,
        *,
        url: str,
        on_message: MessageHandler,
        on_connect: Callable[[WebSocketClientProtocol], Awaitable[None]] | None = None,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self._url = url
        self._on_message = on_message
        self._on_connect = on_connect
        self._reconnect_initial = reconnect_initial_seconds
        self._reconnect_max = reconnect_max_seconds

        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._ws: WebSocketClientProtocol | None = None
        self._last_heartbeat: datetime | None = None
        self._connection_attempts = 0

    @property
    def last_heartbeat(self) -> datetime | None:
        """Timestamp of the last received frame."""

        return self._last_heartbeat

    async def start(self) -> None:
        """Start the websocket run loop."""

        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="ws-manager")

    async def stop(self) -> None:
        """Stop websocket loop and close current connection."""

        self._stop_event.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        backoff = self._reconnect_initial
        while not self._stop_event.is_set():
            try:
                await self._consume_connection()
                backoff = self._reconnect_initial
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "websocket consume failed",
                    extra={
                        "component": "ws_client",
                        "event_type": "ws_error",
                        "reason": str(exc),
                        "latency_ms": None,
                    },
                )
                await asyncio.sleep(backoff)
                backoff = min(self._reconnect_max, backoff * 2)

    async def _consume_connection(self) -> None:
        self._connection_attempts += 1
        logger.info(
            "connecting websocket",
            extra={
                "component": "ws_client",
                "event_type": "ws_connecting",
                "reason": f"attempt={self._connection_attempts}",
            },
        )

        async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as websocket:
            self._ws = websocket
            self._last_heartbeat = utc_now()
            if self._on_connect is not None:
                await self._on_connect(websocket)

            async for frame in websocket:
                if self._stop_event.is_set():
                    break
                self._last_heartbeat = utc_now()
                message = self._decode_frame(frame)
                await self._on_message(message)
            self._ws = None

    def _decode_frame(self, frame: str | bytes) -> dict | str:
        if isinstance(frame, bytes):
            try:
                frame = frame.decode("utf-8")
            except UnicodeDecodeError:
                return ""
        try:
            parsed = json.loads(frame)
            if isinstance(parsed, dict):
                return parsed
            return frame
        except json.JSONDecodeError:
            return frame
