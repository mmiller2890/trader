"""Read-only live preflight checks and operator command."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from clients.auth import ClobCredentials, build_clob_credentials, is_live_trading_enabled
from clients.clob_client import ClobClientAdapter
from clients.data_api import DataApiClient
from clients.geoblock import GeoblockClient
from clients.gamma_markets import GammaMarketDiscoveryClient
from config.loader import load_config
from config.schema import AppConfig, Mode
from state.reconciliation import ReconciliationService
from state.store import InMemoryStateStore


class PreflightCheck(BaseModel):
    """One named, redacted preflight result."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    passed: bool
    reason: str = Field(min_length=1)


class LivePreflightReport(BaseModel):
    """Aggregated read-only live readiness report."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    checks: list[PreflightCheck] = Field(default_factory=list)


def failure_report(check_name: str, reason: str) -> LivePreflightReport:
    """Build a machine-readable failure without copying exception messages."""

    return LivePreflightReport(
        ok=False,
        checks=[PreflightCheck(name=check_name, passed=False, reason=reason)],
    )


def _exception_reason(prefix: str, exc: Exception) -> str:
    """Return a stable error code safe for logs, JSON, and the dashboard."""

    return f"{prefix}:{type(exc).__name__}"


async def run_preflight(
    *,
    config: AppConfig,
    credentials: ClobCredentials,
    adapter: Any,
    positions_client: Any,
    geoblock: Any,
    reconcile: Callable[[], Awaitable[bool]],
    require_live_guards: bool = True,
    subscribed_token_ids: Sequence[str] | None = None,
) -> LivePreflightReport:
    """Run every read-only live gate and aggregate the results."""

    checks: list[PreflightCheck] = []

    if not require_live_guards:
        checks.append(
            PreflightCheck(
                name="config_live_guards",
                passed=True,
                reason="not_required_for_read_only_preflight",
            )
        )
    elif is_live_trading_enabled(config):
        checks.append(PreflightCheck(name="config_live_guards", passed=True, reason="live_guards_enabled"))
    else:
        checks.append(PreflightCheck(name="config_live_guards", passed=False, reason="live_guards_disabled"))

    if credentials.has_l1 and credentials.has_l2 and credentials.proxy_address:
        checks.append(PreflightCheck(name="credentials_complete", passed=True, reason="credentials_complete"))
    else:
        missing = []
        if not credentials.has_l1:
            missing.append("private_key")
        if not credentials.has_l2:
            missing.append("l2_credentials")
        if not credentials.proxy_address:
            missing.append("funder")
        checks.append(PreflightCheck(name="credentials_complete", passed=False, reason=f"missing:{','.join(missing)}"))

    try:
        geoblock_status = geoblock.check()
    except Exception as exc:
        geoblock_status = None
        checks.append(
            PreflightCheck(
                name="geoblock_allowed",
                passed=False,
                reason=_exception_reason("geoblock_error", exc),
            )
        )
    if geoblock_status is not None:
        checks.append(
            PreflightCheck(
                name="geoblock_allowed",
                passed=geoblock_status.allowed,
                reason=geoblock_status.reason,
            )
        )

    try:
        adapter.healthcheck()
        checks.append(PreflightCheck(name="clob_health", passed=True, reason="clob_health_ok"))
    except Exception as exc:
        checks.append(
            PreflightCheck(
                name="clob_health",
                passed=False,
                reason=_exception_reason("clob_health_failed", exc),
            )
        )

    try:
        adapter.get_open_orders()
        checks.append(PreflightCheck(name="open_orders_read", passed=True, reason="open_orders_read_ok"))
    except Exception as exc:
        checks.append(
            PreflightCheck(
                name="open_orders_read",
                passed=False,
                reason=_exception_reason("open_orders_read_failed", exc),
            )
        )

    try:
        positions_client.get_positions(credentials.proxy_address or "")
        checks.append(PreflightCheck(name="positions_read", passed=True, reason="positions_read_ok"))
    except Exception as exc:
        checks.append(
            PreflightCheck(
                name="positions_read",
                passed=False,
                reason=_exception_reason("positions_read_failed", exc),
            )
        )

    try:
        collateral = adapter.get_collateral_status()
        required = config.execution.max_live_order_notional
        if collateral.balance >= required and collateral.allowance >= required:
            checks.append(PreflightCheck(name="collateral_sufficient", passed=True, reason="collateral_sufficient"))
        else:
            checks.append(
                PreflightCheck(
                    name="collateral_sufficient",
                    passed=False,
                    reason=f"collateral_insufficient:balance={collateral.balance},allowance={collateral.allowance},required={required}",
                )
            )
    except Exception as exc:
        checks.append(
            PreflightCheck(
                name="collateral_sufficient",
                passed=False,
                reason=_exception_reason("collateral_read_failed", exc),
            )
        )

    effective_token_ids = (
        list(subscribed_token_ids)
        if subscribed_token_ids is not None
        else config.market_data.subscribed_token_ids
    )
    if effective_token_ids:
        reason = (
            "automatic_market_subscription_configured"
            if config.market_data.automatic_market.enabled
            else "subscription_configured"
        )
        checks.append(
            PreflightCheck(
                name="subscription_configured",
                passed=True,
                reason=reason,
            )
        )
    else:
        checks.append(PreflightCheck(name="subscription_configured", passed=False, reason="no_subscribed_token_ids"))

    try:
        reconciled = await reconcile()
    except Exception as exc:
        checks.append(
            PreflightCheck(
                name="reconciliation",
                passed=False,
                reason=_exception_reason("reconciliation_failed", exc),
            )
        )
    else:
        if reconciled:
            checks.append(PreflightCheck(name="reconciliation", passed=True, reason="reconciliation_ok"))
        else:
            checks.append(PreflightCheck(name="reconciliation", passed=False, reason="reconciliation_failed"))

    return LivePreflightReport(ok=all(check.passed for check in checks), checks=checks)


async def resolve_preflight_token_ids(
    config: AppConfig,
    *,
    discovery_client_factory: Callable[[object], object] = GammaMarketDiscoveryClient,
) -> list[str]:
    """Resolve the validated token scope used by read-only live checks."""

    if not config.market_data.automatic_market.enabled:
        return list(config.market_data.subscribed_token_ids)
    discovery = discovery_client_factory(config.market_data.automatic_market)
    try:
        market = await discovery.discover_active()
        return list(market.asset_ids)
    finally:
        await discovery.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only live preflight checks.")
    parser.add_argument("--config-dir", type=Path, help="Directory containing bot.yaml and optional fragments")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run preflight and print a redacted JSON report."""

    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config_dir)
    except Exception as exc:
        report = failure_report(
            "config_load",
            _exception_reason("config_load_failed", exc),
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return 2

    try:
        credentials = build_clob_credentials(config)
    except Exception as exc:
        report = failure_report(
            "credentials_complete",
            _exception_reason("credentials_invalid", exc),
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return 2

    if not (credentials.has_l1 and credentials.has_l2 and credentials.proxy_address):
        missing = []
        if not credentials.has_l1:
            missing.append("private_key")
        if not credentials.has_l2:
            missing.append("l2_credentials")
        if not credentials.proxy_address:
            missing.append("funder")
        report = failure_report("credentials_complete", f"missing:{','.join(missing)}")
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return 2

    try:
        subscribed_token_ids = asyncio.run(resolve_preflight_token_ids(config))
    except Exception as exc:
        report = failure_report(
            "market_discovery",
            _exception_reason("market_discovery_failed", exc),
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return 2

    try:
        adapter = ClobClientAdapter.from_v2(
            config=config,
            credentials=credentials,
            read_only=True,
        )
        positions_client = DataApiClient(config)
        geoblock = GeoblockClient(config)

        async def reconcile() -> bool:
            state = InMemoryStateStore(mode=Mode.LIVE)
            service = ReconciliationService(
                state_store=state,
                mode=Mode.LIVE,
                open_orders_reader=adapter,
                positions_reader=positions_client,
                funder_address=credentials.proxy_address,
            )
            report = await service.reconcile_startup()
            return report.ok

        report = asyncio.run(
            run_preflight(
                config=config,
                credentials=credentials,
                adapter=adapter,
                positions_client=positions_client,
                geoblock=geoblock,
                reconcile=reconcile,
                require_live_guards=False,
                subscribed_token_ids=subscribed_token_ids,
            )
        )
    except Exception as exc:
        report = failure_report(
            "client_initialization",
            _exception_reason("client_initialization_failed", exc),
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return 2

    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
