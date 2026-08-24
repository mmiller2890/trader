"""Isolated supervised runtime loops.

Each loop owns exactly one responsibility, sends a heartbeat after every
successful cycle, and converts expected dependency failures into typed
incidents via the injected ``report`` callback. Unexpected programming
errors bubble up to the RuntimeSupervisor.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.supervisor import Heartbeat
from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    OperationalIncident,
)
from reliability.policy import RecoveryAction

logger = logging.getLogger(__name__)

Report = Callable[[OperationalIncident], Awaitable[str]]


def _utc_now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)


def _make_incident(
    *,
    component: str,
    category: IncidentCategory,
    severity: IncidentSeverity,
    reason: str,
) -> OperationalIncident:
    return OperationalIncident(
        incident_id=f"inc-{component}-{category.value}",
        fingerprint=f"{component}:{category.value}:{reason}",
        component=component,
        category=category,
        severity=severity,
        reason=reason[:512],
        first_seen_at=_utc_now(),
        last_seen_at=_utc_now(),
    )


async def _cycle(
    *,
    interval_seconds: float,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    work: Callable[[], Awaitable[None]],
) -> None:
    try:
        await work()
    except asyncio.CancelledError:
        raise
    heartbeat_done = heartbeat()
    await heartbeat()
    del heartbeat_done
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.01, interval_seconds))
    except TimeoutError:
        pass


async def reconciliation_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
) -> None:
    """Refresh authoritative account state on a fixed cadence."""

    config = getattr(services, "config", None)
    interval = getattr(getattr(config, "bot", None), "housekeeping_interval_seconds", 15)
    while not stop_event.is_set():
        async def work() -> None:
            try:
                reconciliation_report = await services.reconciliation.reconcile_runtime()
                deferred = getattr(reconciliation_report, "deferred_positions", [])
                if not getattr(reconciliation_report, "ok", True):
                    incident = _make_incident(
                        component="reconciliation",
                        category=IncidentCategory.AUTHORITATIVE_STATE,
                        severity=IncidentSeverity.WARNING,
                        reason="reconciliation_failed",
                    )
                    await report(incident)
                elif deferred:
                    incident = _make_incident(
                        component="reconciliation",
                        category=IncidentCategory.ACCOUNT_DIVERGENCE,
                        severity=IncidentSeverity.INFO,
                        reason="confirmation_deferred:" + ",".join(deferred),
                    )
                    await report(incident)
            except Exception as exc:
                incident = _make_incident(
                    component="reconciliation",
                    category=IncidentCategory.AUTHORITATIVE_STATE,
                    severity=IncidentSeverity.WARNING,
                    reason=f"transport:{type(exc).__name__}",
                )
                await report(incident)

        await _cycle(
            interval_seconds=interval,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )


async def runtime_risk_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
) -> None:
    """Run periodic safety checks and report typed halt incidents."""

    config = getattr(services, "config", None)
    interval = getattr(getattr(config, "bot", None), "housekeeping_interval_seconds", 15)
    while not stop_event.is_set():

        async def work() -> None:
            decision = await services.runtime_risk.evaluate_runtime()
            if decision.approved:
                return
            category = IncidentCategory.ACCOUNTING
            if any(
                check.check_name == "stale_heartbeat" and not check.passed
                for check in decision.checks
            ):
                category = IncidentCategory.TRANSIENT_TRANSPORT
            elif any(
                check.check_name == "repeated_failures" and not check.passed
                for check in decision.checks
            ):
                category = IncidentCategory.EXIT_SAFETY
            incident = _make_incident(
                component="runtime_risk",
                category=category,
                severity=IncidentSeverity.URGENT,
                reason=decision.reason,
            )
            await report(incident)

        await _cycle(
            interval_seconds=interval,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )


async def position_exit_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
) -> None:
    """Evaluate managed exits on the housekeeping cadence."""

    config = getattr(services, "config", None)
    interval = getattr(getattr(config, "bot", None), "housekeeping_interval_seconds", 15)
    exit_manager = getattr(services, "exit_manager", None)
    router = getattr(services, "router", None)

    def market_end_lookup(market_id: str) -> object:
        rotator = getattr(services, "market_rotator", None)
        if rotator is None:
            return None
        current = rotator.status().current_market
        if current is None:
            return None
        if market_id in {current.market_id, current.condition_id}:
            return current.end_at
        return None

    while not stop_event.is_set():

        async def work() -> None:
            if exit_manager is None or router is None:
                return
            signals = await exit_manager.on_timer(market_end_lookup=market_end_lookup)
            for signal in signals:
                await router.route_signal(signal)

        await _cycle(
            interval_seconds=interval,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )


async def strategy_timer_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
) -> None:
    """Route strategy timer signals on the housekeeping cadence."""

    config = getattr(services, "config", None)
    interval = getattr(getattr(config, "bot", None), "housekeeping_interval_seconds", 15)
    strategy = getattr(services, "strategy", None)
    router = getattr(services, "router", None)

    while not stop_event.is_set():

        async def work() -> None:
            if strategy is None or router is None:
                return
            signals = await strategy.on_timer()
            for signal in signals:
                await router.route_signal(signal)

        await _cycle(
            interval_seconds=interval,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )


async def snapshot_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
) -> None:
    """Persist bounded runtime snapshots and schedule retention passes."""

    config = getattr(services, "config", None)
    interval = getattr(getattr(config, "bot", None), "snapshot_interval_seconds", 30)
    snapshots = getattr(services, "snapshots", None)
    state_store = getattr(services, "state_store", None)
    retention_manager = getattr(services, "retention_manager", None)
    rotator = getattr(services, "market_rotator", None)
    reliability = getattr(config, "reliability", None)
    retention_interval = float(
        getattr(reliability, "retention_interval_seconds", 3600.0)
    )

    previous_keys: set[tuple[str, str]] = set()
    last_retention_at: datetime | None = None

    def current_market_keys() -> set[tuple[str, str]]:
        if rotator is None:
            return set()
        status = rotator.status()
        current = getattr(status, "current_market", None)
        if current is None:
            return set()
        keys: set[tuple[str, str]] = set()
        for token_id in getattr(current, "asset_ids", []) or []:
            condition_id = getattr(current, "condition_id", None)
            discovered_id = getattr(current, "market_id", None)
            if condition_id:
                keys.add((str(condition_id), str(token_id)))
            if discovered_id:
                keys.add((str(discovered_id), str(token_id)))
        return keys

    while not stop_event.is_set():

        async def work() -> None:
            nonlocal last_retention_at, previous_keys
            if snapshots is not None and state_store is not None:
                try:
                    await snapshots.save_from_state(state_store)
                except Exception as exc:
                    incident = _make_incident(
                        component="snapshots",
                        category=IncidentCategory.PERSISTENCE,
                        severity=IncidentSeverity.WARNING,
                        reason=f"snapshot_write_failed:{type(exc).__name__}",
                    )
                    await report(incident)

            if retention_manager is None or state_store is None:
                return
            now = _utc_now()
            if (
                last_retention_at is not None
                and (now - last_retention_at).total_seconds()
                < retention_interval
            ):
                return
            current_keys = current_market_keys()
            active_keys = previous_keys | current_keys
            try:
                set_reporter = getattr(retention_manager, "set_reporter", None)
                if callable(set_reporter):
                    set_reporter(report)
                await retention_manager.run_once(
                    state_store=state_store,
                    active_market_keys=active_keys,
                    now=now,
                )
                last_retention_at = now
                previous_keys = current_keys
            except Exception as exc:
                incident = _make_incident(
                    component="retention",
                    category=IncidentCategory.PERSISTENCE,
                    severity=IncidentSeverity.WARNING,
                    reason=f"retention_pass_failed:{type(exc).__name__}",
                )
                await report(incident)

        await _cycle(
            interval_seconds=interval,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )


async def notification_delivery_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
) -> None:
    """Deliver due outbox alerts; failures never terminate other loops."""

    worker = getattr(services, "notification_worker", None)

    while not stop_event.is_set():

        async def work() -> None:
            if worker is None:
                return
            try:
                await worker.deliver_due_once()
            except Exception as exc:
                logger.warning(
                    "notification delivery failed",
                    extra={
                        "component": "notification_worker",
                        "event_type": "delivery_failed",
                        "reason": type(exc).__name__,
                    },
                )

        await _cycle(
            interval_seconds=5.0,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )
