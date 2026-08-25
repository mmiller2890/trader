from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.main as app_main
from app.runtime import FatalRuntimeError, RuntimePhase, RuntimeStatus
from config.schema import AppConfig, Mode
from app.main import parser
from persistence.operations import OperationsRepository
from reliability.lease import LiveLeaseService


def test_cli_requires_explicit_live_flag() -> None:
    assert parser().parse_args([]).live is False
    assert parser().parse_args(["--live"]).live is True


def test_cli_threads_resume_live_intent_without_fresh_authorization(
    monkeypatch,
) -> None:
    calls: list[tuple[bool, bool]] = []

    async def fake_run(*, allow_live: bool, resume_live: bool) -> None:
        calls.append((allow_live, resume_live))

    monkeypatch.setattr(app_main, "run", fake_run)

    app_main.main(["--resume-live"])

    assert calls == [(False, True)]


@pytest.mark.asyncio
async def test_headless_run_owns_one_process_reliability_graph(
    tmp_path,
    monkeypatch,
) -> None:
    config = AppConfig()
    calls: list[object] = []

    class ProcessServices:
        repository = object()
        leases = object()
        alerts = object()
        telegram = object()
        notification_worker = object()

        async def start(self) -> None:
            calls.append("process_start")

        async def close(self) -> None:
            calls.append("process_close")

    process_services = ProcessServices()

    def build_process_services(*, config: AppConfig, data_dir) -> object:
        calls.append(("build", config, data_dir))
        return process_services

    class Runtime:
        def __init__(self, *, process_services: object) -> None:
            calls.append(("runtime", process_services))

        async def start(self, *, allow_live: bool) -> RuntimeStatus:
            return RuntimeStatus(phase=RuntimePhase.RUNNING, mode=Mode.DRY_RUN)

        async def wait(self) -> RuntimeStatus:
            return RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.DRY_RUN)

        async def stop(self) -> RuntimeStatus:
            calls.append("runtime_stop")
            return RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.DRY_RUN)

        def request_stop(self) -> None:
            return None

    monkeypatch.setattr(app_main, "BotRuntime", Runtime)
    monkeypatch.setattr(app_main, "load_config", lambda _: config)
    monkeypatch.setattr(
        app_main,
        "build_process_reliability_services",
        build_process_services,
        raising=False,
    )
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    loop = app_main.asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)

    await app_main.run()

    assert calls == [
        ("build", config, tmp_path),
        ("runtime", process_services),
        "process_start",
        "runtime_stop",
        "process_close",
    ]


@pytest.mark.asyncio
async def test_headless_resume_rejects_missing_lease_before_runtime_start(
    tmp_path,
    monkeypatch,
) -> None:
    config = AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )
    closed: list[str] = []

    async def process_start() -> None:
        return None

    async def process_close() -> None:
        closed.append("closed")

    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    process_services = SimpleNamespace(
        repository=repository,
        leases=LiveLeaseService(repository),
        alerts=object(),
        telegram=object(),
        notification_worker=object(),
        start=process_start,
        close=process_close,
    )

    class Runtime:
        def __init__(self, *, process_services: object) -> None:
            assert process_services is expected_process_services

        async def start(self, *, allow_live: bool) -> RuntimeStatus:
            raise AssertionError("runtime must not start without a lease")

        async def stop(self) -> RuntimeStatus:
            return RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.LIVE)

        def request_stop(self) -> None:
            return None

    expected_process_services = process_services
    monkeypatch.setattr(app_main, "BotRuntime", Runtime)
    monkeypatch.setattr(app_main, "load_config", lambda _: config, raising=False)
    monkeypatch.setattr(
        app_main,
        "build_process_reliability_services",
        lambda **_: process_services,
    )
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    loop = app_main.asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)

    with pytest.raises(FatalRuntimeError, match="lease_missing_or_revoked"):
        await app_main.run(resume_live=True)
    assert closed == ["closed"]


@pytest.mark.asyncio
async def test_headless_resume_uses_valid_lease_as_existing_authorization(
    tmp_path,
    monkeypatch,
) -> None:
    config = AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )
    await LiveLeaseService(
        OperationsRepository(tmp_path / "bot.sqlite3")
    ).issue(config, now=datetime.now(tz=UTC))
    starts: list[bool] = []
    closed: list[str] = []

    async def process_start() -> None:
        return None

    async def process_close() -> None:
        closed.append("closed")

    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    process_services = SimpleNamespace(
        repository=repository,
        leases=LiveLeaseService(repository),
        alerts=object(),
        telegram=object(),
        notification_worker=object(),
        start=process_start,
        close=process_close,
    )

    class Runtime:
        def __init__(self, *, process_services: object) -> None:
            assert process_services is expected_process_services

        async def start(self, *, allow_live: bool) -> RuntimeStatus:
            starts.append(allow_live)
            return RuntimeStatus(phase=RuntimePhase.RUNNING, mode=Mode.LIVE)

        async def wait(self) -> RuntimeStatus:
            return RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.LIVE)

        async def stop(self) -> RuntimeStatus:
            return RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.LIVE)

        def request_stop(self) -> None:
            return None

    expected_process_services = process_services
    monkeypatch.setattr(app_main, "BotRuntime", Runtime)
    monkeypatch.setattr(app_main, "load_config", lambda _: config, raising=False)
    monkeypatch.setattr(
        app_main,
        "build_process_reliability_services",
        lambda **_: process_services,
    )
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        app_main.signal,
        "SIGINT",
        app_main.signal.SIGINT,
    )
    loop = app_main.asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *args: None)

    await app_main.run(resume_live=True)

    assert starts == [True]
    assert closed == ["closed"]
