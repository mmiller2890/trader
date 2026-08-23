from __future__ import annotations

from decimal import Decimal

import pytest

from clients.auth import ClobCredentials
from clients.clob_client import ClobAdapterError, CollateralStatus
from clients.data_api import DataApiError
from clients.geoblock import GeoblockStatus
from config.schema import AppConfig, Mode
from models.position import Position
from scripts.live_preflight import LivePreflightReport, run_preflight


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


def live_config() -> AppConfig:
    return AppConfig(
        bot={"mode": Mode.LIVE},
        execution={"allow_live_trading": True, "dry_run_force": False},
        market_data={"subscribed_token_ids": ["t1"]},
    )


def complete_credentials() -> ClobCredentials:
    return ClobCredentials(
        private_key="private-key",
        proxy_address="0x1111111111111111111111111111111111111111",
        api_key="api-key",
        secret="api-secret",
        passphrase="passphrase",
        rpc_url="https://rpc.example",
    )


class HealthyAdapter:
    def healthcheck(self) -> bool:
        return True

    def get_open_orders(self) -> list[object]:
        return []

    def get_collateral_status(self) -> CollateralStatus:
        return CollateralStatus(balance=Decimal("100"), allowance=Decimal("1000"))


class HealthyPositions:
    def get_positions(self, user_address: str) -> list[Position]:
        return []


class AllowedGeoblock:
    def check(self) -> GeoblockStatus:
        return GeoblockStatus(allowed=True, reason="geoblock_allowed")


class FailingAdapter(HealthyAdapter):
    def healthcheck(self) -> bool:
        raise ClobAdapterError("clob healthcheck failed: boom")


class FailingPositions(HealthyPositions):
    def get_positions(self, user_address: str) -> list[Position]:
        raise DataApiError("positions HTTP request failed: boom")


class BlockedGeoblock(AllowedGeoblock):
    def check(self) -> GeoblockStatus:
        return GeoblockStatus(allowed=False, reason="geoblock_blocked")


class LowAllowanceAdapter(HealthyAdapter):
    def get_collateral_status(self) -> CollateralStatus:
        return CollateralStatus(balance=Decimal("0.5"), allowance=Decimal("0"))


@pytest.mark.asyncio
async def test_preflight_passes_when_every_check_succeeds() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is True
    assert all(check.passed for check in report.checks)
    names = {check.name for check in report.checks}
    expected = {
        "config_live_guards",
        "credentials_complete",
        "geoblock_allowed",
        "clob_health",
        "open_orders_read",
        "positions_read",
        "collateral_sufficient",
        "subscription_configured",
        "reconciliation",
    }
    assert expected <= names


@pytest.mark.asyncio
async def test_preflight_fails_when_geoblock_blocks() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=BlockedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    geoblock_check = next(check for check in report.checks if check.name == "geoblock_allowed")
    assert geoblock_check.passed is False


@pytest.mark.asyncio
async def test_preflight_fails_when_clob_health_fails() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=FailingAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    health_check = next(check for check in report.checks if check.name == "clob_health")
    assert health_check.passed is False
    assert "boom" in health_check.reason


@pytest.mark.asyncio
async def test_preflight_fails_when_positions_read_fails() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=FailingPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    positions_check = next(check for check in report.checks if check.name == "positions_read")
    assert positions_check.passed is False


@pytest.mark.asyncio
async def test_preflight_fails_when_collateral_is_insufficient() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=LowAllowanceAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    collateral_check = next(check for check in report.checks if check.name == "collateral_sufficient")
    assert collateral_check.passed is False


@pytest.mark.asyncio
async def test_preflight_fails_when_reconciliation_fails() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _false(),
    )
    assert report.ok is False
    reconciliation_check = next(check for check in report.checks if check.name == "reconciliation")
    assert reconciliation_check.passed is False


@pytest.mark.asyncio
async def test_preflight_fails_when_credentials_are_incomplete() -> None:
    credentials = complete_credentials()
    credentials = ClobCredentials(
        private_key=None,
        proxy_address=credentials.proxy_address,
        api_key=credentials.api_key,
        secret=credentials.secret,
        passphrase=credentials.passphrase,
        rpc_url=credentials.rpc_url,
    )
    report = await run_preflight(
        config=live_config(),
        credentials=credentials,
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    credentials_check = next(check for check in report.checks if check.name == "credentials_complete")
    assert credentials_check.passed is False


@pytest.mark.asyncio
async def test_preflight_fails_when_no_token_is_subscribed() -> None:
    base = live_config()
    config = base.model_copy(
        update={
            "market_data": base.market_data.model_copy(
                update={"subscribed_token_ids": []}
            )
        }
    )
    report = await run_preflight(
        config=config,
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    subscription_check = next(check for check in report.checks if check.name == "subscription_configured")
    assert subscription_check.passed is False


@pytest.mark.asyncio
async def test_preflight_fails_when_live_guards_are_off() -> None:
    config = AppConfig(
        bot={"mode": Mode.DRY_RUN},
        market_data={"subscribed_token_ids": ["t1"]},
    )
    report = await run_preflight(
        config=config,
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    assert report.ok is False
    guards_check = next(check for check in report.checks if check.name == "config_live_guards")
    assert guards_check.passed is False


@pytest.mark.asyncio
async def test_preflight_report_serializes_without_secrets() -> None:
    report = await run_preflight(
        config=live_config(),
        credentials=complete_credentials(),
        adapter=HealthyAdapter(),
        positions_client=HealthyPositions(),
        geoblock=AllowedGeoblock(),
        reconcile=lambda: _true(),
    )
    payload = report.model_dump_json()
    assert "private-key" not in payload
    assert "api-key" not in payload
    assert "api-secret" not in payload
    assert "passphrase" not in payload
    assert isinstance(report, LivePreflightReport)
