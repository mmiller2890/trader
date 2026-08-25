from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime import ControlResult, RuntimePhase, RuntimeStatus
from config.schema import AppConfig, Mode
from dashboard.controller import (
    ConfirmationError,
    DashboardController,
    RuntimeConflictError,
    secret_redactions,
)
from dashboard.models import PreflightView
from notifications.outbox import AlertService, NotificationWorker
from persistence.operations import OperationsRepository
from persistence.snapshots import SnapshotStore, StateSnapshot
from reliability.lease import LiveLeaseService


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.is_running = False
        self.services = None

    def status(self) -> RuntimeStatus:
        return RuntimeStatus(
            phase=RuntimePhase.RUNNING if self.is_running else RuntimePhase.STOPPED,
            mode=Mode.DRY_RUN,
        )

    async def start(self, config_dir: Path, *, allow_live: bool) -> RuntimeStatus:
        self.calls.append(("start", allow_live))
        self.is_running = True
        return self.status()

    async def stop(self) -> RuntimeStatus:
        self.calls.append("stop")
        self.is_running = False
        return self.status()

    async def shutdown_process(self) -> RuntimeStatus:
        self.calls.append("shutdown_process")
        self.is_running = False
        return self.status()

    async def emergency_halt(self, confirmation: str) -> RuntimeStatus:
        self.calls.extend(["kill_switch", "cancel_all"])
        return RuntimeStatus(phase=RuntimePhase.HALTED, mode=Mode.DRY_RUN)

    async def cancel_all(self, confirmation: str) -> ControlResult:
        self.calls.append("cancel_all")
        return ControlResult(ok=True, action="cancel_all", reason="orders_cancelled")


class FakePreflight:
    async def run(self) -> PreflightView:
        return PreflightView(ok=False, status="failed", reason="credentials_incomplete")


def controller(tmp_path: Path) -> DashboardController:
    return DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: AppConfig(),
        preflight=FakePreflight(),
    )


NOW = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)


def live_controller(tmp_path: Path) -> DashboardController:
    config = AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )
    return DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: config,
        preflight=FakePreflight(),
        now=lambda: NOW,
    )


def mark_preflight_passed(current: DashboardController, checked_at: datetime) -> None:
    current.last_preflight = PreflightView(
        ok=True,
        status="passed",
        reason="preflight_passed",
        checked_at=checked_at,
    )
    current._preflight_fingerprint = current._config_fingerprint()


async def mark_telegram_test_delivered(
    current: DashboardController,
    delivered_at: datetime,
) -> None:
    alert = await current._alert_service.enqueue_test(now=delivered_at)
    await current._operations_repository.mark_alert_delivered(
        alert.alert_id,
        delivered_at=delivered_at,
    )


def test_secret_redactions_extract_values_without_stringifying_secret_wrappers() -> None:
    config = AppConfig(
        secrets={
            "private_key": "test-private",
            "clob_api_key": "test-key",
            "clob_secret": "test-secret",
            "clob_passphrase": "test-passphrase",
            "telegram_bot_token": "test-telegram",
        }
    )

    assert secret_redactions(config) == [
        "test-private",
        "test-key",
        "test-secret",
        "test-passphrase",
        "test-telegram",
    ]


def test_secret_redactions_include_derived_eoa_funder() -> None:
    config = AppConfig(
        exchange={"signature_type": 0},
        secrets={
            "private_key": "0x" + "11" * 32,
        },
    )

    redactions = secret_redactions(config)

    assert config.secrets.private_key.get_secret_value() in redactions
    assert any(value.startswith("0x") and len(value) == 42 for value in redactions)


@pytest.mark.asyncio
async def test_controller_starts_dashboard_runtime_with_live_disabled(tmp_path: Path) -> None:
    current = controller(tmp_path)

    status = await current.start()

    assert status.phase == RuntimePhase.RUNNING
    assert current.runtime.calls == [("start", False)]


@pytest.mark.asyncio
async def test_live_start_requires_recent_delivered_telegram_test(
    tmp_path: Path,
) -> None:
    current = live_controller(tmp_path)
    mark_preflight_passed(current, NOW)

    with pytest.raises(RuntimeConflictError, match="recent_telegram_test_required"):
        await current.start("START LIVE")

    assert current.runtime.calls == []


@pytest.mark.asyncio
async def test_telegram_test_delivers_and_records_live_start_gate(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )
    repository = OperationsRepository(tmp_path / "process.sqlite3")
    alerts = AlertService(repository, config, now=lambda: NOW)

    class Telegram:
        async def send(self, alert: object) -> None:
            return None

        async def close(self) -> None:
            return None

    telegram = Telegram()
    worker = NotificationWorker(repository, telegram, config, now=lambda: NOW)
    process_services = SimpleNamespace(
        repository=repository,
        leases=LiveLeaseService(repository),
        alerts=alerts,
        telegram=telegram,
        notification_worker=worker,
    )
    current = DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        config_loader=lambda _: config,
        preflight=FakePreflight(),
        now=lambda: NOW,
        process_services=process_services,
    )

    with pytest.raises(ConfirmationError, match="SEND TEST"):
        await current.send_telegram_test("send test")
    result = await current.send_telegram_test("SEND TEST")

    assert result.ok is True
    assert result.reason == "telegram_test_delivered"
    assert await repository.last_delivered_at("telegram:test") == NOW


@pytest.mark.asyncio
async def test_controller_live_start_requires_fresh_preflight_and_confirmation(
    tmp_path: Path,
) -> None:
    current = live_controller(tmp_path)

    with pytest.raises(RuntimeConflictError, match="fresh_preflight_required"):
        await current.start("START LIVE")

    current.last_preflight = PreflightView(
        ok=True,
        status="passed",
        reason="preflight_passed",
        checked_at=NOW - timedelta(minutes=6),
    )
    with pytest.raises(RuntimeConflictError, match="fresh_preflight_required"):
        await current.start("START LIVE")

    mark_preflight_passed(current, NOW)
    with pytest.raises(ConfirmationError, match="START LIVE"):
        await current.start("start live")

    await mark_telegram_test_delivered(current, NOW)
    await current.start("START LIVE")
    assert current.runtime.calls == [("start", True)]


@pytest.mark.asyncio
async def test_manual_live_start_issues_lease_and_operator_stop_revokes_it(
    tmp_path: Path,
) -> None:
    current = live_controller(tmp_path)
    mark_preflight_passed(current, NOW)
    await mark_telegram_test_delivered(current, NOW)

    await current.start("START LIVE")

    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")
    lease = await repository.get_active_lease()
    assert lease is not None
    assert lease.issued_at == NOW

    await current.stop()

    assert await repository.get_active_lease() is None
    assert current.runtime.calls == [("start", True), "stop"]


@pytest.mark.asyncio
async def test_live_lease_is_issued_only_after_runtime_reaches_running(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )
    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")

    class Runtime(FakeRuntime):
        async def start(
            self, config_dir: Path, *, allow_live: bool
        ) -> RuntimeStatus:
            assert await repository.get_active_lease() is None
            return await super().start(config_dir, allow_live=allow_live)

    current = DashboardController(
        runtime=Runtime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: config,
        preflight=FakePreflight(),
        now=lambda: NOW,
    )
    mark_preflight_passed(current, NOW)
    test_alert = await current._alert_service.enqueue_test(now=NOW)
    await repository.mark_alert_delivered(test_alert.alert_id, delivered_at=NOW)

    status = await current.start("START LIVE")

    assert status.phase == RuntimePhase.RUNNING
    assert await repository.get_active_lease() is not None


@pytest.mark.asyncio
async def test_valid_persisted_lease_auto_resumes_live_runtime(tmp_path: Path) -> None:
    current = live_controller(tmp_path)
    config = current.config()
    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")
    await LiveLeaseService(repository).issue(config, now=NOW)

    status = await current.resume_on_startup()

    assert status.phase == RuntimePhase.RUNNING
    assert current.runtime.calls == [("start", True)]
    restored = await repository.get_active_lease()
    assert restored is not None and restored.issued_at == NOW


@pytest.mark.asyncio
async def test_auto_resume_rejects_latched_kill_switch(tmp_path: Path) -> None:
    current = live_controller(tmp_path)
    config = current.config()
    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")
    await LiveLeaseService(repository).issue(config, now=NOW)
    await SnapshotStore(tmp_path / "data" / "snapshots" / "state.json").save(
        StateSnapshot(
            mode=Mode.LIVE,
            kill_switch_active=True,
            kill_switch_reason="accounting_invariant",
        )
    )

    status = await current.resume_on_startup()

    assert status.phase == RuntimePhase.STOPPED
    assert current.runtime.calls == []


@pytest.mark.asyncio
async def test_auto_resume_rejection_uses_process_owned_outbox(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )
    repository = OperationsRepository(tmp_path / "process.sqlite3")
    process_services = SimpleNamespace(
        repository=repository,
        leases=LiveLeaseService(repository),
        alerts=AlertService(repository, config, now=lambda: NOW),
        telegram=object(),
        notification_worker=object(),
    )
    current = DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        config_loader=lambda _: config,
        preflight=FakePreflight(),
        now=lambda: NOW,
        process_services=process_services,
    )

    status = await current.resume_on_startup()

    assert status.phase == RuntimePhase.STOPPED
    due = await repository.due_alerts(now=NOW, limit=10)
    assert len(due) == 1
    assert due[0].incident_fingerprint.startswith("event:auto_resume_rejected")
    assert due[0].severity.value == "urgent"


@pytest.mark.asyncio
async def test_process_shutdown_preserves_live_lease(tmp_path: Path) -> None:
    current = live_controller(tmp_path)
    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")
    await LiveLeaseService(repository).issue(current.config(), now=NOW)

    await current.shutdown_process()

    assert await repository.get_active_lease() is not None
    assert current.runtime.calls == ["shutdown_process"]


@pytest.mark.asyncio
async def test_live_affecting_config_change_revokes_active_lease(
    tmp_path: Path,
) -> None:
    current = live_controller(tmp_path)
    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")
    await LiveLeaseService(repository).issue(current.config(), now=NOW)

    await current.save_config(
        {"subscribed_token_ids": ["123"], "target_token_ids": []}
    )

    assert await repository.get_active_lease() is None


@pytest.mark.asyncio
async def test_controller_live_mode_change_requires_fresh_preflight_and_confirmation(
    tmp_path: Path,
) -> None:
    current = controller(tmp_path)
    calls: list[Mode] = []
    current._editor.set_mode = lambda mode: calls.append(mode) or mode

    with pytest.raises(RuntimeConflictError, match="fresh_preflight_required"):
        await current.set_mode(Mode.LIVE, "ENABLE LIVE")

    mark_preflight_passed(current, datetime.now(tz=UTC))
    with pytest.raises(ConfirmationError, match="ENABLE LIVE"):
        await current.set_mode(Mode.LIVE, "enable live")

    assert await current.set_mode(Mode.LIVE, "ENABLE LIVE") == Mode.LIVE
    assert calls == [Mode.LIVE]


@pytest.mark.asyncio
async def test_controller_state_exposes_authoritative_live_gate_booleans(
    tmp_path: Path,
) -> None:
    current = live_controller(tmp_path)
    mark_preflight_passed(current, NOW)

    state = await current.state()

    assert state.preflight_fresh is True
    assert state.live_armed is True
    assert state.live_start_ready is True
    readiness = {item.name: item for item in state.readiness}
    assert readiness["live_start"].passed is True


@pytest.mark.asyncio
async def test_controller_state_exposes_when_passed_preflight_expired(
    tmp_path: Path,
) -> None:
    current = live_controller(tmp_path)
    checked_at = NOW - timedelta(minutes=6)
    mark_preflight_passed(current, checked_at)

    state = await current.state()

    assert state.preflight.status == "passed"
    assert state.preflight_fresh is False
    assert state.preflight_expires_at == checked_at + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_controller_rejects_failed_runtime_start(tmp_path: Path) -> None:
    current = controller(tmp_path)

    async def failed_start(config_dir: Path, *, allow_live: bool) -> RuntimeStatus:
        return RuntimeStatus(
            phase=RuntimePhase.FAILED,
            mode=Mode.LIVE,
            reason="live_start_disabled_pending_review",
        )

    current.runtime.start = failed_start

    with pytest.raises(RuntimeConflictError, match="live_start_disabled_pending_review"):
        await current.start()


@pytest.mark.asyncio
async def test_failed_live_bootstrap_invalidates_cached_preflight(tmp_path: Path) -> None:
    current = live_controller(tmp_path)
    mark_preflight_passed(current, NOW)

    async def failed_start(config_dir: Path, *, allow_live: bool) -> RuntimeStatus:
        return RuntimeStatus(
            phase=RuntimePhase.FAILED,
            mode=Mode.LIVE,
            reason="live_preflight_failed:collateral_sufficient",
        )

    current.runtime.start = failed_start
    await mark_telegram_test_delivered(current, NOW)

    with pytest.raises(RuntimeConflictError, match="collateral_sufficient"):
        await current.start("START LIVE")

    assert current.last_preflight.ok is False
    assert current.last_preflight.reason == "live_preflight_failed:collateral_sufficient"
    assert current._preflight_is_fresh() is False


@pytest.mark.asyncio
async def test_inflight_preflight_cannot_overwrite_config_invalidation(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingPreflight:
        async def run(self) -> PreflightView:
            started.set()
            await release.wait()
            return PreflightView(
                ok=True,
                status="passed",
                reason="preflight_passed",
                checked_at=NOW,
            )

    current = DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: AppConfig(),
        preflight=BlockingPreflight(),
        now=lambda: NOW,
    )
    task = asyncio.create_task(current.run_preflight())
    await started.wait()

    await current.save_config(
        {"subscribed_token_ids": ["123"], "target_token_ids": []}
    )
    release.set()
    result = await task

    assert result.ok is False
    assert result.reason == "preflight_invalidated_by_config_change"
    assert current._preflight_is_fresh() is False


@pytest.mark.asyncio
async def test_preflight_freshness_detects_external_config_change(tmp_path: Path) -> None:
    configs = [AppConfig()]

    class PassingPreflight:
        async def run(self) -> PreflightView:
            return PreflightView(
                ok=True,
                status="passed",
                reason="preflight_passed",
                checked_at=NOW,
            )

    current = DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: configs[0],
        preflight=PassingPreflight(),
        now=lambda: NOW,
    )
    await current.run_preflight()
    assert current._preflight_is_fresh() is True

    configs[0] = AppConfig(risk={"max_total_exposure": "51"})

    assert current._preflight_is_fresh() is False


@pytest.mark.asyncio
async def test_controller_requires_exact_destructive_confirmations(tmp_path: Path) -> None:
    current = controller(tmp_path)

    with pytest.raises(ConfirmationError):
        await current.halt("halt")
    with pytest.raises(ConfirmationError):
        await current.cancel_all("cancel all")

    await current.halt("HALT")
    assert current.runtime.calls == ["kill_switch", "cancel_all"]


@pytest.mark.asyncio
async def test_controller_retains_last_redacted_preflight(tmp_path: Path) -> None:
    current = controller(tmp_path)

    result = await current.run_preflight()

    assert result.ok is False
    assert result.reason == "credentials_incomplete"
    assert current.last_preflight == result
    assert (await current.state()).preflight == result


@pytest.mark.asyncio
async def test_controller_converts_preflight_runner_crash_to_safe_failure(
    tmp_path: Path,
) -> None:
    class CrashingPreflight:
        async def run(self) -> PreflightView:
            raise RuntimeError("sensitive remote URL")

    current = DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: AppConfig(),
        preflight=CrashingPreflight(),
    )

    result = await current.run_preflight()

    assert result.status == "failed"
    assert result.reason == "preflight_runner_failed:RuntimeError"
    assert "sensitive" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_controller_rejects_config_save_while_running(tmp_path: Path) -> None:
    current = live_controller(tmp_path)
    repository = OperationsRepository(tmp_path / "data" / "bot.sqlite3")
    await LiveLeaseService(repository).issue(current.config(), now=NOW)
    current.runtime.is_running = True

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        await current.save_config(
            {"subscribed_token_ids": ["123"], "target_token_ids": []}
        )
    assert await repository.get_active_lease() is not None


@pytest.mark.asyncio
async def test_controller_rejects_mode_change_during_runtime_transition(
    tmp_path: Path,
) -> None:
    current = controller(tmp_path)
    current.runtime.status = lambda: RuntimeStatus(
        phase=RuntimePhase.STARTING,
        mode=Mode.DRY_RUN,
    )

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        await current.set_mode(Mode.DRY_RUN)


@pytest.mark.asyncio
async def test_controller_rejects_manual_scope_when_automatic_market_enabled(
    tmp_path: Path,
) -> None:
    current = DashboardController(
        runtime=FakeRuntime(),
        config_dir=tmp_path,
        data_dir=tmp_path / "data",
        config_loader=lambda _: AppConfig(
            market_data={"automatic_market": {"enabled": True}}
        ),
        preflight=FakePreflight(),
    )

    with pytest.raises(RuntimeError, match="automatic_market_owns_token_scope"):
        await current.save_config(
            {"subscribed_token_ids": ["123"], "target_token_ids": ["123"]}
        )


# --- guarded intervention recovery -------------------------------------------


class FakeRecoveryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def clear_halt(self, *, incident_id: str, confirmation: str):
        from reliability.recovery import RecoveryResult

        self.calls.append(
            {"incident_id": incident_id, "confirmation": confirmation}
        )
        return RecoveryResult(
            cleared=True,
            incident_id=incident_id,
            checks=[],
            reason="halt_cleared",
        )


def test_clear_halt_delegates_and_invalidates_preflight(tmp_path: Path) -> None:
    current = controller(tmp_path)
    fresh_at = datetime.now(tz=UTC) - timedelta(seconds=30)
    mark_preflight_passed(current, fresh_at)
    assert current._preflight_is_fresh() is True

    incident_id = "inc-12345678abcd"
    confirmation = f"CLEAR HALT {incident_id[-8:]}"
    service = FakeRecoveryService()
    current._recovery = service
    result = asyncio.run(current.clear_halt(incident_id, confirmation))

    assert result.cleared is True
    assert service.calls == [
        {"incident_id": incident_id, "confirmation": confirmation}
    ]
    assert current._preflight_is_fresh() is False
    assert current.runtime.status().phase != RuntimePhase.RUNNING
