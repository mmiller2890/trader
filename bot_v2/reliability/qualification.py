"""Machine-readable qualification reports for unattended operation gates."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RunMode(str, Enum):
    ACCELERATED = "accelerated"
    WALL_CLOCK = "wall_clock"


class RequiredFault(str, Enum):
    """Fault families that must be injected at least once per run."""

    WEBSOCKET_DISCONNECT = "websocket_disconnect"
    REST_FALLBACK = "rest_fallback_period"
    CLOB_RATE_LIMIT = "clob_429_timeout_5xx"
    DISCOVERY_DELAY = "gamma_discovery_delay"
    TELEGRAM_OUTAGE = "telegram_outage_across_restart"
    PROCESS_RESTART = "ordinary_process_restart_with_valid_lease"
    TASK_CRASH_RESTARTED = "task_crash_restarted_successfully"
    SNAPSHOT_WRITE_FAILURE = "snapshot_archive_transient_write_failure"
    DISK_WARNING_DEGRADED = "disk_warning_and_degraded_below_halt"


class QualificationReport(BaseModel):
    """Deterministic, machine-readable result of one qualification run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: uuid4().hex)
    mode: Literal["accelerated", "wall_clock"]
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    markets_completed: int = Field(ge=0)
    orders_submitted: int = Field(default=0, ge=0)
    fills_accounted: int = Field(default=0, ge=0)
    duplicate_orders: int = Field(default=0, ge=0)
    orphan_open_orders: int = Field(default=0, ge=0)
    accounting_errors: int = Field(default=0, ge=0)
    injected_faults: dict[str, int] = Field(default_factory=dict)
    recovered_faults: dict[str, int] = Field(default_factory=dict)
    urgent_alerts_expected: int = Field(default=0, ge=0)
    urgent_alerts_delivered: int = Field(default=0, ge=0)
    max_memory_mib: float = Field(default=0.0, ge=0)
    final_memory_mib: float = Field(default=0.0, ge=0)
    max_disk_mib: float = Field(default=0.0, ge=0)
    passed: bool = False
    failures: list[str] = Field(default_factory=list)


class QualificationEvaluator:
    """Pure evaluator turning run counters into a passed/failing report."""

    def __init__(
        self,
        *,
        mode: RunMode,
        required_faults: list[RequiredFault],
        memory_ceiling_mib: float,
        accelerated_min_markets: int = 500,
        wall_clock_min_markets: int = 288,
        wall_clock_min_hours: float = 72.0,
    ) -> None:
        self._mode = mode
        self._required_faults = required_faults
        self._memory_ceiling_mib = memory_ceiling_mib
        self._min_markets = (
            accelerated_min_markets
            if mode is RunMode.ACCELERATED
            else wall_clock_min_markets
        )
        self._min_hours = wall_clock_min_hours

    def evaluate(
        self,
        *,
        markets_completed: int,
        duration_hours: float,
        orders_submitted: int,
        fills_accounted: int,
        duplicate_orders: int,
        orphan_open_orders: int,
        accounting_errors: int,
        injected_faults: dict[str, int],
        recovered_faults: dict[str, int],
        urgent_alerts_expected: int,
        urgent_alerts_delivered: int,
        max_memory_mib: float,
        final_memory_mib: float,
        max_disk_mib: float = 0.0,
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> QualificationReport:
        failures: list[str] = []
        if duplicate_orders > 0:
            failures.append(f"{duplicate_orders} duplicate order identities")
        if orphan_open_orders > 0:
            failures.append(f"{orphan_open_orders} orphan open orders")
        if accounting_errors > 0:
            failures.append(f"{accounting_errors} accounting errors")
        if urgent_alerts_delivered < urgent_alerts_expected:
            failures.append(
                f"urgent alerts delivered {urgent_alerts_delivered}"
                f" < expected {urgent_alerts_expected}"
            )
        for fault in self._required_faults:
            count = int(injected_faults.get(fault.value, 0))
            if count <= 0:
                failures.append(
                    f"required fault not injected: {fault.value}"
                )
        if max_memory_mib > self._memory_ceiling_mib:
            failures.append(
                f"peak memory {max_memory_mib:.1f} MiB exceeds ceiling"
                f" {self._memory_ceiling_mib:.1f} MiB (unbounded growth)"
            )
        if markets_completed < self._min_markets:
            failures.append(
                f"markets completed {markets_completed} < {self._min_markets}"
            )
        if (
            self._mode is RunMode.WALL_CLOCK
            and duration_hours < self._min_hours
        ):
            failures.append(
                f"duration {duration_hours:.2f}h < {self._min_hours:.2f}h"
            )

        now = datetime.now(tz=UTC)
        return QualificationReport(
            run_id=run_id or uuid4().hex,
            mode=self._mode.value,
            started_at=started_at or now,
            completed_at=now,
            markets_completed=markets_completed,
            orders_submitted=orders_submitted,
            fills_accounted=fills_accounted,
            duplicate_orders=duplicate_orders,
            orphan_open_orders=orphan_open_orders,
            accounting_errors=accounting_errors,
            injected_faults=dict(injected_faults),
            recovered_faults=dict(recovered_faults),
            urgent_alerts_expected=urgent_alerts_expected,
            urgent_alerts_delivered=urgent_alerts_delivered,
            max_memory_mib=max_memory_mib,
            final_memory_mib=final_memory_mib,
            max_disk_mib=max_disk_mib,
            passed=not failures,
            failures=failures,
        )
