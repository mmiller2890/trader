"""Resilient async websocket manager boundary."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict | str], Awaitable[None]]
HeartbeatHandler = Callable[[datetime], Awaitable[None]]


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class WebSocketManager:
    """Reconnect-capable websocket client with subscription and application ping."""

    def __init__(
        self,
        *,
        url: str,
        on_message: MessageHandler,
        on_connect: Callable[[ClientConnection], Awaitable[None]] | None = None,
        on_heartbeat: HeartbeatHandler | None = None,
        asset_ids: list[str] | None = None,
        ping_interval_seconds: float = 10,
        reconnect_initial_seconds: float = 1.0,
        reconnect_max_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if asset_ids is not None and not asset_ids:
            raise ValueError("asset_ids must not be empty when provided")
        self._url = url
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_heartbeat = on_heartbeat
        self._asset_ids = asset_ids
        self._ping_interval = ping_interval_seconds
        self._reconnect_initial = reconnect_initial_seconds
        self._reconnect_max = reconnect_max_seconds
        self._sleep = sleep

        self._stop_event = asyncio.Event()
        self._subscription_lock = asyncio.Lock()
        self._replacement_lock = asyncio.Lock()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._ws: ClientConnection | None = None
        self._is_connected = False
        self._last_heartbeat: datetime | None = None
        self._connection_attempts = 0
        self._connect_factory = connect

    @property
    def last_heartbeat(self) -> datetime | None:
        """Timestamp of the last received frame."""

        return self._last_heartbeat

    @property
    def is_connected(self) -> bool:
        """Whether a market WebSocket connection is currently active."""

        return self._is_connected

    @property
    def asset_ids(self) -> list[str] | None:
        """Return a copy of the next market subscription."""

        return list(self._asset_ids) if self._asset_ids is not None else None

    async def start(self) -> None:
        """Start the websocket run loop."""

        if self._task and not self._task.done():
            return
        async with self._subscription_lock:
            self._stopping = False
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="ws-manager")

    async def stop(self) -> None:
        """Stop websocket loop and close current connection."""

        async with self._subscription_lock:
            self._stopping = True
        self._stop_event.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def replace_asset_ids(self, asset_ids: list[str]) -> bool:
        """Replace the subscription and reconnect the active socket."""

        if (
            not asset_ids
            or len(set(asset_ids)) != len(asset_ids)
            or any(not asset_id.isdecimal() for asset_id in asset_ids)
        ):
            raise ValueError("asset_ids must be unique non-empty decimal strings")
        async with self._replacement_lock:
            async with self._subscription_lock:
                if self._stopping:
                    raise RuntimeError("websocket manager is stopping")
                if self._asset_ids == asset_ids:
                    return False
                previous_asset_ids = (
                    list(self._asset_ids) if self._asset_ids is not None else None
                )
                self._asset_ids = list(asset_ids)
                websocket = self._ws
            if websocket is not None:
                await websocket.close()
            async with self._subscription_lock:
                if self._stopping:
                    self._asset_ids = previous_asset_ids
                    raise RuntimeError("websocket manager is stopping")
            return True

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

        async with self._connect_factory(self._url, ping_interval=20, ping_timeout=20) as websocket:
            self._ws = websocket
            self._is_connected = True
            await self._record_heartbeat()
            ping_task: asyncio.Task[None] | None = None
            try:
                if self._on_connect is not None:
                    await self._on_connect(websocket)
                async with self._subscription_lock:
                    asset_ids = (
                        list(self._asset_ids)
                        if self._asset_ids is not None
                        else None
                    )
                if asset_ids:
                    await websocket.send(
                        json.dumps({
                            "assets_ids": asset_ids,
                            "type": "market",
                            "custom_feature_enabled": True,
                        })
                    )

                ping_task = asyncio.create_task(
                    self._ping_loop(websocket), name="ws-application-ping"
                )
                async for frame in websocket:
                    if self._stop_event.is_set():
                        break
                    await self._record_heartbeat()
                    message = self._decode_frame(frame)
                    await self._on_message(message)
            finally:
                if ping_task is not None:
                    ping_task.cancel()
                    await asyncio.gather(ping_task, return_exceptions=True)
                self._is_connected = False
                self._ws = None

    async def _ping_loop(self, websocket: ClientConnection) -> None:
        while True:
            await self._sleep(self._ping_interval)
            await websocket.send("PING")

    async def _record_heartbeat(self) -> None:
        timestamp = utc_now()
        self._last_heartbeat = timestamp
        if self._on_heartbeat is not None:
            await self._on_heartbeat(timestamp)

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
