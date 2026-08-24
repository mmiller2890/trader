from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from clients.ws_client import WebSocketManager


def test_ws_client_imports_without_deprecated_websockets_api() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::DeprecationWarning",
            "-c",
            "import clients.ws_client",
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


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


class BlockingCloseSocket(FakeSocket):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True


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
    on_heartbeat: object | None = None,
    asset_ids: list[str] | None = None,
    ping_interval_seconds: float = 10,
) -> WebSocketManager:
    connect = FakeConnect(socket)
    manager = WebSocketManager(
        url="wss://example.invalid/ws",
        on_message=lambda message: asyncio.sleep(0),
        on_connect=on_connect,
        on_heartbeat=on_heartbeat,
        asset_ids=asset_ids or ["t1", "t2"],
        ping_interval_seconds=ping_interval_seconds,
        sleep=one_shot_sleep(),
    )
    manager._connect_factory = lambda url, **kwargs: connect
    return manager


@pytest.mark.asyncio
async def test_transport_heartbeat_records_connection_and_received_frames() -> None:
    observed: list[datetime] = []

    async def record(timestamp: datetime) -> None:
        observed.append(timestamp)

    manager = make_manager(
        socket=FakeSocket(frames=['{"event_type":"book"}']),
        on_heartbeat=record,
    )

    await manager._consume_connection()

    assert len(observed) == 2
    assert all(timestamp.tzinfo is not None for timestamp in observed)


@pytest.mark.asyncio
async def test_first_sent_frame_is_market_subscription() -> None:
    socket = FakeSocket()
    manager = make_manager(socket=socket)
    await manager._consume_connection()
    assert socket.sent[0] == json.dumps({
        "assets_ids": ["t1", "t2"],
        "type": "market",
        "custom_feature_enabled": True,
    })


@pytest.mark.asyncio
async def test_application_ping_is_sent_every_interval() -> None:
    socket = FakeSocket(frames=["{}"])
    manager = make_manager(socket=socket, ping_interval_seconds=10)
    await manager._consume_connection()
    assert socket.sent[0] == json.dumps({
        "assets_ids": ["t1", "t2"],
        "type": "market",
        "custom_feature_enabled": True,
    })
    assert socket.sent[1] == "PING"


@pytest.mark.asyncio
async def test_subscription_is_resent_on_reconnect() -> None:
    socket = FakeSocket()
    manager = make_manager(socket=socket)
    await manager._consume_connection()
    await manager._consume_connection()
    expected_subscription = json.dumps({
        "assets_ids": ["t1", "t2"],
        "type": "market",
        "custom_feature_enabled": True,
    })
    assert socket.sent[0] == expected_subscription
    assert socket.sent[1] == "PING"
    assert socket.sent[2] == expected_subscription


def test_empty_asset_list_is_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="asset"):
        WebSocketManager(
            url="wss://example.invalid/ws",
            on_message=lambda message: asyncio.sleep(0),
            asset_ids=[],
        )


@pytest.mark.asyncio
async def test_health_distinguishes_connected_retrying_and_stopped() -> None:
    socket = FakeSocket(frames=["{}"])
    manager = make_manager(socket=socket)
    assert manager.health().connected is False
    assert manager.health().task_running is False

    await manager._consume_connection()
    health = manager.health()
    assert health.connected is False
    assert health.task_running is False

    from clients.ws_client import WebSocketManager as WSM
    live = make_manager(socket=FakeSocket(frames=["{}"]))
    await live.start()
    await asyncio.sleep(0.05)
    health_live = live.health()
    assert health_live.task_running is True
    assert health_live.connected is True
    assert health_live.last_heartbeat is not None
    await live.stop()
    assert live.health().task_running is False


@pytest.mark.asyncio
async def test_wait_closed_resolves_after_stop() -> None:
    socket = FakeSocket()
    manager = make_manager(socket=socket)

    async def run_supervised() -> None:
        await manager._consume_connection()

    task = asyncio.create_task(manager._consume_connection())
    await asyncio.sleep(0.02)
    await manager.stop()
    await asyncio.wait_for(manager.wait_closed(), timeout=1)


@pytest.mark.asyncio
async def test_health_exposes_type_only_error_after_failure() -> None:
    class ExplodingConnector:
        async def __aenter__(self) -> FakeSocket:
            raise ConnectionError("token=secret remote detail")

        async def __aexit__(self, *exc: object) -> None:
            return None

    manager = make_manager(socket=FakeSocket())
    manager._connect_factory = lambda url, **kwargs: ExplodingConnector()
    try:
        await manager._consume_connection()
    except ConnectionError:
        pass
    error_text = manager.health().last_error or ""
    assert "secret" not in error_text
    assert "ConnectionError" in error_text or "token" not in error_text


@pytest.mark.asyncio
async def test_replace_asset_ids_closes_socket_and_next_connection_uses_new_ids() -> None:
    first = FakeSocket()
    second = FakeSocket()
    manager = make_manager(socket=first, asset_ids=["1", "2"])
    manager._ws = first

    changed = await manager.replace_asset_ids(["3", "4"])
    manager._connect_factory = lambda url, **kwargs: FakeConnect(second)
    await manager._consume_connection()

    assert changed is True
    assert first.closed is True
    assert manager.asset_ids == ["3", "4"]
    assert json.loads(second.sent[0])["assets_ids"] == ["3", "4"]


@pytest.mark.asyncio
async def test_replace_asset_ids_is_noop_for_unchanged_ids() -> None:
    socket = FakeSocket()
    manager = make_manager(socket=socket, asset_ids=["1", "2"])
    manager._ws = socket

    changed = await manager.replace_asset_ids(["1", "2"])

    assert changed is False
    assert socket.closed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("asset_ids", [[], ["1", "1"], ["1", "token-x"]])
async def test_replace_asset_ids_rejects_invalid_values(asset_ids: list[str]) -> None:
    manager = make_manager(socket=FakeSocket(), asset_ids=["1", "2"])

    with pytest.raises(ValueError, match="asset"):
        await manager.replace_asset_ids(asset_ids)


@pytest.mark.asyncio
async def test_replace_asset_ids_rejects_after_stop() -> None:
    manager = make_manager(socket=FakeSocket(), asset_ids=["1", "2"])
    await manager.stop()

    with pytest.raises(RuntimeError, match="stopping"):
        await manager.replace_asset_ids(["3", "4"])


@pytest.mark.asyncio
async def test_stop_wins_when_replacement_is_closing_active_socket() -> None:
    socket = BlockingCloseSocket()
    manager = make_manager(socket=socket, asset_ids=["1", "2"])
    manager._ws = socket

    replace_task = asyncio.create_task(manager.replace_asset_ids(["3", "4"]))
    await socket.close_started.wait()
    stop_task = asyncio.create_task(manager.stop())
    await asyncio.sleep(0)
    socket.release_close.set()

    with pytest.raises(RuntimeError, match="stopping"):
        await replace_task
    await stop_task
    assert manager.asset_ids == ["1", "2"]


@pytest.mark.asyncio
async def test_stop_cancels_run_loop_blocked_in_message_handler() -> None:
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def blocked_handler(message: object) -> None:
        handler_started.set()
        await release_handler.wait()

    socket = FakeSocket(frames=["{}"])
    manager = WebSocketManager(
        url="wss://example.invalid/ws",
        on_message=blocked_handler,
        asset_ids=["1", "2"],
        sleep=one_shot_sleep(),
    )
    manager._connect_factory = lambda url, **kwargs: FakeConnect(socket)

    await manager.start()
    await handler_started.wait()

    assert manager.is_connected is True

    await asyncio.wait_for(manager.stop(), timeout=0.1)

    assert socket.closed is True
    assert manager._task is not None
    assert manager._task.done() is True
    assert manager.is_connected is False
