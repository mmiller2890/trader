from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

    await current.start("START LIVE")
    assert current.runtime.calls == [("start", True)]


@pytest.mark.asyncio
async def test_controller_live_mode_change_requires_fresh_preflight_and_confirmation(
    tmp_path: Path,
) -> None:
    current = controller(tmp_path)
    calls: list[Mode] = []
    current._editor.set_mode = lambda mode: calls.append(mode) or mode

    with pytest.raises(RuntimeConflictError, match="fresh_preflight_required"):
        current.set_mode(Mode.LIVE, "ENABLE LIVE")

    mark_preflight_passed(current, datetime.now(tz=UTC))
    with pytest.raises(ConfirmationError, match="ENABLE LIVE"):
        current.set_mode(Mode.LIVE, "enable live")

    assert current.set_mode(Mode.LIVE, "ENABLE LIVE") == Mode.LIVE
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

    current.save_config({"subscribed_token_ids": ["123"], "target_token_ids": []})
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
    current = controller(tmp_path)
    current.runtime.is_running = True

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        current.save_config({"subscribed_token_ids": ["123"], "target_token_ids": []})


def test_controller_rejects_mode_change_during_runtime_transition(
    tmp_path: Path,
) -> None:
    current = controller(tmp_path)
    current.runtime.status = lambda: RuntimeStatus(
        phase=RuntimePhase.STARTING,
        mode=Mode.DRY_RUN,
    )

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        current.set_mode(Mode.DRY_RUN)


def test_controller_rejects_manual_scope_when_automatic_market_enabled(
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
        current.save_config(
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
