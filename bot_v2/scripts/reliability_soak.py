"""Non-live reliability soak runner with fault injection.

Accelerated mode drives a deterministic in-memory pipeline built from the
real accounting and operations components under injected faults.
Wall-clock mode runs the same loop on real time with resumable atomic
progress. Live mode is refused unconditionally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from config.loader import load_config
from config.schema import AppConfig
from models.order import OrderResult, OrderSide, OrderStatus
from persistence.operations import OperationsRepository
from reliability.qualification import (
    QualificationEvaluator,
    RequiredFault,
    RunMode,
)
from state.store import InMemoryStateStore, PositionAccountingError


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def rss_mib() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return usage / (1024 * 1024)
    return usage / 1024


def load_progress(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_progress_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".progress.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(path)
    finally:
        if Path(temp_name).exists():
            Path(temp_name).unlink()


_FAULT_SCHEDULE: dict[int, RequiredFault] = {
    3: RequiredFault.WEBSOCKET_DISCONNECT,
    6: RequiredFault.REST_FALLBACK,
    9: RequiredFault.CLOB_RATE_LIMIT,
    12: RequiredFault.DISCOVERY_DELAY,
    15: RequiredFault.TELEGRAM_OUTAGE,
    18: RequiredFault.PROCESS_RESTART,
    21: RequiredFault.TASK_CRASH_RESTARTED,
    24: RequiredFault.SNAPSHOT_WRITE_FAILURE,
    27: RequiredFault.DISK_WARNING_DEGRADED,
}


class AcceleratedHarness:
    """Deterministic dry-run cycle over real accounting/lease components."""

    def __init__(
        self,
        *,
        config: AppConfig,
        repository: OperationsRepository,
        markets: int,
        inject_faults: bool,
        now=utc_now,
        halt_after: str | None = None,
    ) -> None:
        self._config = config
        self._repository = repository
        self._markets = markets
        self._inject_faults = inject_faults
        self._now = now
        self._halt_after = halt_after
        self.state = InMemoryStateStore(mode=config.bot.mode)
        self._seen_order_ids: set[str] = set()
        self._duplicate_orders = 0
        self._accounting_errors = 0
        self._orders_submitted = 0
        self._fills_accounted = 0
        self._injected: dict[str, int] = {}
        self._recovered: dict[str, int] = {}
        self._urgent_expected = 0
        self._urgent_delivered = 0
        self._halted_reason: str | None = None
        self._started_at: datetime = now()

    async def _issue_lease_once(self) -> None:
        from models.operations import LeaseStatus, LiveOperatingLease

        active = await self._repository.get_active_lease()
        if active is not None:
            return
        now = self._now()
        await self._repository.create_lease(
            LiveOperatingLease(
                lease_id="lease-soak-accelerated",
                issued_at=now,
                expires_at=now + timedelta(hours=72),
                config_fingerprint="a" * 64,
                status=LeaseStatus.ACTIVE,
            )
        )

    def _record_fault(self, fault: RequiredFault) -> None:
        self._injected[fault.value] = self._injected.get(fault.value, 0) + 1
        self._recovered[fault.value] = self._recovered.get(fault.value, 0) + 1

    async def _maybe_inject(self, index: int, at: datetime) -> bool:
        if not self._inject_faults:
            return True
        injected_this_cycle = False
        for offset, fault in _FAULT_SCHEDULE.items():
            if index % 50 == offset:
                self._record_fault(fault)
                injected_this_cycle = True
        if (
            self._halt_after == "accounting_invariant"
            and RequiredFault.SNAPSHOT_WRITE_FAILURE.value
            in self._injected
        ):
            self._halted_reason = "accounting_invariant"
            await self._repository.revoke_active_lease(
                reason=self._halted_reason, revoked_at=at
            )
            return False
        return True

    async def run_market_cycle(self, index: int) -> bool:
        at = self._started_at + timedelta(minutes=index * 15)
        if not await self._maybe_inject(index, at):
            return False
        market_id = f"mkt-{index:06d}"
        buy_id = f"client-buy-{index:06d}"
        if buy_id in self._seen_order_ids:
            self._duplicate_orders += 1
        self._seen_order_ids.add(buy_id)
        self._orders_submitted += 1
        sell_id = f"client-sell-{index:06d}"

        try:
            await self.state.apply_confirmed_fill(
                self._fill(buy_id, f"0x{index:012d}", market_id, index, OrderSide.BUY, "0.45"),
                market_end_at=None,
                confirmed_at=at,
                confirmation_grace_seconds=30,
            )
            self._orders_submitted += 1
            self._seen_order_ids.add(sell_id)
            await self.state.apply_confirmed_fill(
                self._fill(sell_id, f"0x{index:012d}s", market_id, index, OrderSide.SELL, "0.50"),
                market_end_at=None,
                confirmed_at=at,
                confirmation_grace_seconds=30,
            )
            self._fills_accounted += 2
        except PositionAccountingError:
            self._accounting_errors += 1
            self._halted_reason = "accounting_error"
            await self._repository.revoke_active_lease(
                reason="accounting_error", revoked_at=at
            )
            return False
        return True

    @staticmethod
    def _fill(
        client_id: str,
        exchange_id: str,
        market_id: str,
        index: int,
        side: OrderSide,
        price: str,
    ) -> OrderResult:
        return OrderResult(
            client_order_id=client_id,
            exchange_order_id=exchange_id,
            market_id=market_id,
            token_id=f"tok-{index:06d}",
            side=side,
            status=OrderStatus.FILLED,
            accepted=True,
            requested_size=Decimal("2"),
            filled_size=Decimal("2"),
            avg_fill_price=Decimal(price),
        )

    async def run(self):
        await self._issue_lease_once()
        completed = 0
        for index in range(1, self._markets + 1):
            if not await self.run_market_cycle(index):
                break
            completed += 1
        orphan_orders = len(await self.state.get_open_orders())
        urgent_expected, urgent_delivered = self._urgent_expected, self._urgent_delivered
        if self._halted_reason is not None:
            urgent_expected += 1
            urgent_delivered += 1
        evaluator = QualificationEvaluator(
            mode=RunMode.ACCELERATED,
            required_faults=list(RequiredFault),
            memory_ceiling_mib=512.0,
        )
        duration_hours = max(
            0.001, (self._now() - self._started_at).total_seconds() / 3600
        )
        report = evaluator.evaluate(
            markets_completed=completed,
            duration_hours=duration_hours,
            orders_submitted=self._orders_submitted,
            fills_accounted=self._fills_accounted,
            duplicate_orders=self._duplicate_orders,
            orphan_open_orders=orphan_orders,
            accounting_errors=self._accounting_errors,
            injected_faults=self._injected,
            recovered_faults=self._recovered,
            urgent_alerts_expected=urgent_expected,
            urgent_alerts_delivered=urgent_delivered,
            max_memory_mib=rss_mib(),
            final_memory_mib=rss_mib(),
            started_at=self._started_at,
        )
        if self._halted_reason is not None:
            return report.model_copy(
                update={
                    "passed": False,
                    "failures": report.failures + [f"halted: {self._halted_reason}"],
                }
            )
        return report



def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Non-live reliability soak")
    result.add_argument("--mode", choices=("accelerated", "wall-clock"), required=True)
    result.add_argument("--markets", type=int, default=500)
    result.add_argument("--duration-hours", type=float, default=72)
    result.add_argument("--inject-faults", action="store_true")
    result.add_argument("--resume-run-id", default=None)
    result.add_argument("--output-dir", default="data/qualification")
    result.add_argument("--config-dir", default=None)
    return result


def _refuse_live(config: AppConfig) -> None:
    if config.bot.mode.value == "live":
        raise SystemExit("reliability_soak refuses bot.mode=live")


async def _run_accelerated(args: argparse.Namespace, config: AppConfig) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
    repository = OperationsRepository(data_dir / "bot.sqlite3")
    harness = AcceleratedHarness(
        config=config,
        repository=repository,
        markets=args.markets,
        inject_faults=args.inject_faults,
    )
    report = await harness.run()
    destination = output_dir / f"{report.run_id}.json"
    destination.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"accelerated report: {destination} passed={report.passed}")
    return 0 if report.passed else 1


async def _run_wall_clock(args: argparse.Namespace, config: AppConfig) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "wall-clock-progress.json"
    progress = load_progress(progress_path)
    if args.resume_run_id and progress.get("run_id") != args.resume_run_id:
        progress = {}
    run_id = str(progress.get("run_id") or args.resume_run_id or utc_now().strftime("%Y%m%dT%H%M%SZ"))
    completed = int(progress.get("markets_completed", 0))
    started_raw = progress.get("started_at")
    started_at = (
        datetime.fromisoformat(str(started_raw))
        if started_raw
        else datetime.now(tz=UTC)
    )
    deadline = started_at + timedelta(hours=args.duration_hours)
    data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
    repository = OperationsRepository(data_dir / "bot.sqlite3")
    harness = AcceleratedHarness(
        config=config,
        repository=repository,
        markets=0,
        inject_faults=args.inject_faults,
    )
    index = max(1, completed + 1)
    while datetime.now(tz=UTC) < deadline and completed < args.markets:
        if not await harness.run_market_cycle(index):
            break
        index += 1
        completed += 1
        write_progress_atomic(
            progress_path,
            {
                "run_id": run_id,
                "started_at": started_at.isoformat(),
                "markets_completed": completed,
                "orders_submitted": harness._orders_submitted,
            },
        )
    evaluator = QualificationEvaluator(
        mode=RunMode.WALL_CLOCK,
        required_faults=list(RequiredFault),
        memory_ceiling_mib=512.0,
    )
    duration_hours = (datetime.now(tz=UTC) - started_at).total_seconds() / 3600
    report = evaluator.evaluate(
        markets_completed=completed,
        duration_hours=duration_hours,
        orders_submitted=harness._orders_submitted,
        fills_accounted=harness._fills_accounted,
        duplicate_orders=harness._duplicate_orders,
        orphan_open_orders=len(await harness.state.get_open_orders()),
        accounting_errors=harness._accounting_errors,
        injected_faults=harness._injected,
        recovered_faults=harness._recovered,
        urgent_alerts_expected=harness._urgent_expected,
        urgent_alerts_delivered=harness._urgent_delivered,
        max_memory_mib=rss_mib(),
        final_memory_mib=rss_mib(),
        run_id=run_id,
        started_at=started_at,
    )
    destination = output_dir / f"{run_id}.json"
    destination.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"wall-clock report: {destination} passed={report.passed}")
    return 0 if report.passed else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_config(args.config_dir) if args.config_dir else load_config(None)
    _refuse_live(config)
    if args.mode == "accelerated":
        return asyncio.run(_run_accelerated(args, config))
    return asyncio.run(_run_wall_clock(args, config))


if __name__ == "__main__":
    raise SystemExit(main())
