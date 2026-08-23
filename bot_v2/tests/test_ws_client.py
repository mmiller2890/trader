from __future__ import annotations

import asyncio
import json

import pytest

from clients.ws_client import WebSocketManager


class FakeSocket:
    def __init__(self, frames: list[str] | None = None) -> None:
        self.sent: list[object] = []
        self.closed = False
        self._frames = list(frames or [])

    async def send(self, payload: object) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> "FakeSocket":
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class FakeConnect:
    def __init__(self, socket: FakeSocket) -> None:
        self._socket = socket
        self.attempts = 0

    async def __aenter__(self) -> FakeSocket:
        self.attempts += 1
        return self._socket

    async def __aexit__(self, *exc: object) -> None:
        return None


def one_shot_sleep() -> object:
    calls = 0

    async def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        await asyncio.Event().wait()

    return sleep


def make_manager(
    *,
    socket: FakeSocket,
    on_connect: object | None = None,
    asset_ids: list[str] | None = None,
    ping_interval_seconds: float = 10,
) -> WebSocketManager:
    connect = FakeConnect(socket)
    manager = WebSocketManager(
        url="wss://example.invalid/ws",
        on_message=lambda message: asyncio.sleep(0),
        on_connect=on_connect,
        asset_ids=asset_ids or ["t1", "t2"],
        ping_interval_seconds=ping_interval_seconds,
        sleep=one_shot_sleep(),
    )
    manager._connect_factory = lambda url, **kwargs: connect
    return manager


@pytest.mark.asyncio
async def test_first_sent_frame_is_market_subscription() -> None:
    socket = FakeSocket()
    manager = make_manager(socket=socket)
    await manager._consume_connection()
    assert socket.sent[0] == json.dumps({"assets_ids": ["t1", "t2"], "type": "market"})


@pytest.mark.asyncio
async def test_application_ping_is_sent_every_interval() -> None:
    socket = FakeSocket(frames=["{}"])
    manager = make_manager(socket=socket, ping_interval_seconds=10)
    await manager._consume_connection()
    assert socket.sent[0] == json.dumps({"assets_ids": ["t1", "t2"], "type": "market"})
    assert socket.sent[1] == "PING"


@pytest.mark.asyncio
async def test_subscription_is_resent_on_reconnect() -> None:
    socket = FakeSocket()
    manager = make_manager(socket=socket)
    await manager._consume_connection()
    await manager._consume_connection()
    assert socket.sent[0] == json.dumps({"assets_ids": ["t1", "t2"], "type": "market"})
    assert socket.sent[1] == "PING"
    assert socket.sent[2] == json.dumps({"assets_ids": ["t1", "t2"], "type": "market"})


def test_empty_asset_list_is_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="asset"):
        WebSocketManager(
            url="wss://example.invalid/ws",
            on_message=lambda message: asyncio.sleep(0),
            asset_ids=[],
        )
