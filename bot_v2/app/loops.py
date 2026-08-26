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
    OperationalState,
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
    await heartbeat()
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


async def sweep_stale_resting_orders(services: object) -> list[str]:
    """
    Cancel resting maker entries that have outlived their TTL or their market.

    Nothing at the venue expires a post-only order, and until this existed a
    spike entry rested until it filled -- one sat for 45 minutes on
    2026-08-26 and filled into a market that had already ended. Exits are
    excluded: PositionExitManager sweeps those on its own deadline and then
    escalates to a taker cross, and cancelling one here would race it.

    Returns the client order ids actually cancelled, for the caller to log.
    """

    from execution.stale_orders import stale_resting_orders
    from models.order import OrderStatus

    state_store = getattr(services, "state_store", None)
    submitter = getattr(services, "submitter", None)
    config = getattr(services, "config", None)
    if state_store is None or submitter is None or config is None:
        return []
    ttl = float(
        getattr(getattr(config, "spike_strategy", None), "quote_ttl_seconds", 0.0)
        or 0.0
    )
    if ttl <= 0:
        return []

    open_orders = await state_store.get_open_orders()
    if not open_orders:
        return []

    lifecycles = await state_store.get_position_lifecycles()
    protected = {
        lifecycle.pending_exit_client_order_id
        for lifecycle in lifecycles
        if lifecycle.pending_exit_client_order_id
    }
    market_ends = {
        (lifecycle.market_id, lifecycle.token_id): lifecycle.market_end_at
        for lifecycle in lifecycles
    }

    intents = stale_resting_orders(
        open_orders=open_orders,
        now=_utc_now(),
        ttl_seconds=ttl,
        protected_client_order_ids=protected,
        market_end_lookup=lambda market_id, token_id: market_ends.get(
            (market_id, token_id)
        ),
    )
    if not intents:
        return []

    by_id = {order.client_order_id: order for order in open_orders}
    cancelled: list[str] = []
    for intent in intents:
        result = await submitter.cancel_order(intent)
        if not getattr(result, "terminal", False):
            continue
        cancelled.append(intent.client_order_id)
        order = by_id.get(intent.client_order_id)
        if order is not None:
            # Drop it from the open-order map so the next pass does not try
            # to cancel an order that is already off the book.
            await state_store.set_order_status(
                order.model_copy(update={"status": OrderStatus.CANCELLED})
            )
    return cancelled


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
    market_maker = getattr(services, "market_maker", None)
    router = getattr(services, "router", None)
    state_store = getattr(services, "state_store", None)

    while not stop_event.is_set():

        async def work() -> None:
            if router is None:
                return
            if strategy is not None:
                for signal in await strategy.on_timer():
                    await router.route_signal(signal)
            cancelled = await sweep_stale_resting_orders(services)
            for client_order_id in cancelled:
                logger.info(
                    "cancelled stale resting order",
                    extra={
                        "component": "strategy_timer",
                        "event_type": "stale_resting_order_cancelled",
                        "client_order_id": client_order_id,
                    },
                )
            if market_maker is None:
                return
            # A latched kill switch means stop quoting entirely; otherwise
            # retire quotes that have outlived their TTL so they re-price.
            halted = state_store is not None and await state_store.is_kill_switch_active()
            plan = (
                await market_maker.plan_withdrawal("kill_switch_active")
                if halted
                else await market_maker.plan_maintenance()
            )
            if not plan.empty:
                await router.route_quote_plan(plan, strategy=market_maker)

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
    daily_summary = getattr(services, "daily_summary_emitter", None)

    while not stop_event.is_set():

        async def work() -> None:
            if worker is not None:
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
            if daily_summary is not None:
                try:
                    await daily_summary.maybe_emit()
                except Exception as exc:
                    logger.warning(
                        "daily summary emission failed",
                        extra={
                            "component": "notification_worker",
                            "event_type": "daily_summary_failed",
                            "reason": type(exc).__name__,
                        },
                    )

        await _cycle(
            interval_seconds=5.0,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )


async def health_report_loop(
    services: object,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Report,
    *,
    runtime: object = None,
) -> None:
    """Write the atomic runtime health snapshot every five seconds."""

    store = getattr(services, "health_store", None)
    if store is None:
        return

    while not stop_event.is_set():

        async def work() -> None:
            from persistence.health import build_runtime_health

            state_store = getattr(services, "state_store", None)
            repository = getattr(services, "operations_repository", None)
            ws_manager = getattr(services, "ws_manager", None)
            supervisor = getattr(runtime, "_supervisor", None)
            tasks = []
            alive = supervisor is not None
            if supervisor is not None and hasattr(supervisor, "health"):
                try:
                    tasks = list(await supervisor.health())
                except Exception:
                    tasks = []
                is_alive = getattr(supervisor, "is_alive", None)
                if callable(is_alive):
                    alive = bool(is_alive())
            websocket = None
            if ws_manager is not None and hasattr(ws_manager, "health"):
                try:
                    websocket = ws_manager.health()
                except Exception:
                    websocket = None
            operational_state = OperationalState.STARTING
            reason = None
            status_factory = getattr(runtime, "status", None)
            if callable(status_factory):
                try:
                    status = status_factory()
                    operational_state = OperationalState(status.phase.value)
                    reason = status.reason
                except Exception:
                    pass
            try:
                snapshot = await build_runtime_health(
                    operational_state=operational_state,
                    reason=reason,
                    tasks=tasks,
                    supervisor_alive=alive,
                    websocket=websocket,
                    last_reconciliation_at=(
                        await state_store.get_heartbeat("reconciliation")
                        if state_store is not None
                        else None
                    ),
                    repository=repository,
                    data_path=getattr(services, "data_dir", None),
                    disk_usage=getattr(services, "disk_usage", None),
                )
                await store.save(snapshot)
            except Exception as exc:
                incident = _make_incident(
                    component="health_report",
                    category=IncidentCategory.PERSISTENCE,
                    severity=IncidentSeverity.WARNING,
                    reason=f"health_write_failed:{type(exc).__name__}",
                )
                await report(incident)

        await _cycle(
            interval_seconds=5.0,
            stop_event=stop_event,
            heartbeat=heartbeat,
            work=work,
        )
