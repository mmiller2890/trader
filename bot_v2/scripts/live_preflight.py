"""Read-only live preflight checks and operator command."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from clients.auth import ClobCredentials, build_clob_credentials, is_live_trading_enabled
from clients.clob_client import ClobClientAdapter
from clients.data_api import DataApiClient
from clients.geoblock import GeoblockClient
from config.loader import ConfigError, load_config
from config.schema import AppConfig
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


async def run_preflight(
    *,
    config: AppConfig,
    credentials: ClobCredentials,
    adapter: Any,
    positions_client: Any,
    geoblock: Any,
    reconcile: Callable[[], Awaitable[bool]],
) -> LivePreflightReport:
    """Run every read-only live gate and aggregate the results."""

    checks: list[PreflightCheck] = []

    if is_live_trading_enabled(config):
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
        checks.append(PreflightCheck(name="geoblock_allowed", passed=False, reason=f"geoblock_error:{exc}"))
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
        checks.append(PreflightCheck(name="clob_health", passed=False, reason=f"clob_health_failed:{exc}"))

    try:
        adapter.get_open_orders()
        checks.append(PreflightCheck(name="open_orders_read", passed=True, reason="open_orders_read_ok"))
    except Exception as exc:
        checks.append(PreflightCheck(name="open_orders_read", passed=False, reason=f"open_orders_read_failed:{exc}"))

    try:
        positions_client.get_positions(credentials.proxy_address or "")
        checks.append(PreflightCheck(name="positions_read", passed=True, reason="positions_read_ok"))
    except Exception as exc:
        checks.append(PreflightCheck(name="positions_read", passed=False, reason=f"positions_read_failed:{exc}"))

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
        checks.append(PreflightCheck(name="collateral_sufficient", passed=False, reason=f"collateral_read_failed:{exc}"))

    if config.market_data.subscribed_token_ids:
        checks.append(PreflightCheck(name="subscription_configured", passed=True, reason="subscription_configured"))
    else:
        checks.append(PreflightCheck(name="subscription_configured", passed=False, reason="no_subscribed_token_ids"))

    try:
        reconciled = await reconcile()
    except Exception as exc:
        checks.append(PreflightCheck(name="reconciliation", passed=False, reason=f"reconciliation_failed:{exc}"))
    else:
        if reconciled:
            checks.append(PreflightCheck(name="reconciliation", passed=True, reason="reconciliation_ok"))
        else:
            checks.append(PreflightCheck(name="reconciliation", passed=False, reason="reconciliation_failed"))

    return LivePreflightReport(ok=all(check.passed for check in checks), checks=checks)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only live preflight checks.")
    parser.add_argument("--config-dir", type=Path, help="Directory containing bot.yaml and optional fragments")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run preflight and print a redacted JSON report."""

    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config_dir)
        credentials = build_clob_credentials(config)
        adapter = ClobClientAdapter.from_v2(config=config, credentials=credentials)
        positions_client = DataApiClient(config)
        geoblock = GeoblockClient(config)

        async def reconcile() -> bool:
            state = InMemoryStateStore(mode=config.bot.mode)
            service = ReconciliationService(
                state_store=state,
                mode=config.bot.mode,
                open_orders_reader=adapter,
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
            )
        )
    except (ConfigError, RuntimeError, ValueError) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
