from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.fingerprint import config_fingerprint
from config.schema import AppConfig, Mode
from models.operations import LeaseStatus, LiveOperatingLease
from persistence.operations import OperationsRepository
from reliability.lease import LiveLeaseService, LiveResumeRejected


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
    )


def dry_config() -> AppConfig:
    return AppConfig(bot={"mode": Mode.DRY_RUN})


def reliability_config() -> float:
    return 72


def leases(tmp_path: Path, *, hours: float = 72) -> LiveLeaseService:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    return LiveLeaseService(repository, live_lease_hours=hours)


def test_fingerprint_is_stable_64_hex() -> None:
    fp = config_fingerprint(live_config())
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
    assert fp == config_fingerprint(live_config())


def test_fingerprint_changes_with_mode() -> None:
    assert config_fingerprint(live_config()) != config_fingerprint(dry_config())


def test_fingerprint_ignores_serialization_order() -> None:
    config_a = AppConfig(market_data={"subscribed_token_ids": ["1", "2"]})
    config_b = AppConfig(market_data={"subscribed_token_ids": ["2", "1"]})
    assert config_fingerprint(config_a) == config_fingerprint(config_b)


@pytest.mark.asyncio
async def test_issue_creates_active_lease(tmp_path: Path) -> None:
    service = leases(tmp_path)
    lease = await service.issue(live_config(), now=NOW)

    assert lease.status == LeaseStatus.ACTIVE
    assert lease.expires_at == NOW + timedelta(hours=72)
    restored = await OperationsRepository(tmp_path / "bot.sqlite3").get_active_lease()
    assert restored == lease


@pytest.mark.asyncio
async def test_lease_duration_bounds_enforced_at_config_level() -> None:
    from config.schema import ReliabilityConfig
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReliabilityConfig(live_lease_hours=0)
    with pytest.raises(ValidationError):
        ReliabilityConfig(live_lease_hours=169)
    assert ReliabilityConfig(live_lease_hours=1).live_lease_hours == 1
    assert ReliabilityConfig(live_lease_hours=168).live_lease_hours == 168


@pytest.mark.asyncio
async def test_safety_halt_revokes_lease_before_process_restart(tmp_path: Path) -> None:
    service = leases(tmp_path)
    await service.issue(live_config(), now=NOW)
    await service.revoke("accounting_invariant", now=NOW + timedelta(minutes=1))
    with pytest.raises(LiveResumeRejected, match="lease_missing_or_revoked"):
        await service.validate_for_resume(
            live_config(), now=NOW + timedelta(minutes=2)
        )


@pytest.mark.asyncio
async def test_expired_lease_rejects_resume(tmp_path: Path) -> None:
    service = leases(tmp_path, hours=1)
    await service.issue(live_config(), now=NOW)
    with pytest.raises(LiveResumeRejected, match="lease_expired"):
        await service.validate_for_resume(
            live_config(), now=NOW + timedelta(hours=2)
        )


@pytest.mark.asyncio
async def test_config_mismatch_rejects_resume(tmp_path: Path) -> None:
    service = leases(tmp_path)
    await service.issue(live_config(), now=NOW)
    with pytest.raises(LiveResumeRejected, match="config_fingerprint_mismatch"):
        await service.validate_for_resume(dry_config(), now=NOW + timedelta(minutes=5))


@pytest.mark.asyncio
async def test_valid_lease_passes_resume_validation(tmp_path: Path) -> None:
    service = leases(tmp_path)
    await service.issue(live_config(), now=NOW)
    lease = await service.validate_for_resume(
        live_config(), now=NOW + timedelta(minutes=5)
    )
    assert lease.status == LeaseStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset_hours", "expected"),
    [
        (0, "valid"),
        (48, "warn_24h"),
        (71.5, "warn_1h"),
        (73, "expired"),
    ],
)
async def test_expiration_state_thresholds(tmp_path: Path, offset_hours: float, expected: str) -> None:
    service = leases(tmp_path)
    await service.issue(live_config(), now=NOW)
    state = await service.expiration_state(now=NOW + timedelta(hours=offset_hours))
    assert state == expected
