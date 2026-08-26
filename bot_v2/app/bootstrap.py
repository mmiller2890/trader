"""App bootstrap and dependency wiring."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.modes import is_live_mode
from app.process_services import (
    ProcessReliabilityServices,
    build_process_reliability_services,
)
from clients.auth import build_clob_credentials
from clients.clob_client import ClobClientAdapter
from clients.data_api import DataApiClient
from clients.geoblock import GeoblockClient
from clients.market_data_client import MarketDataClient
from clients.gamma_markets import GammaMarketDiscoveryClient
from clients.market_rotation import Btc15mMarketRotator, DiscoveryClient
from clients.ws_client import WebSocketManager
from config.loader import load_config
from config.schema import AppConfig, AutomaticMarketConfig
from execution.order_builder import OrderBuilder
from execution.router import ExecutionRouter
from execution.submitter import OrderSubmitter
from execution.tracker import OrderTracker
from models.events import BotEvent, EventType
from notifications.events import EventBus
from notifications.outbox import (
    AlertService,
    NotificationWorker,
)
from persistence.db import KeyValueSqliteStore
from persistence.health import HealthSnapshotStore
from persistence.journal import JsonlJournal
from persistence.operations import OperationsRepository
from persistence.retention import RetentionManager
from persistence.snapshots import SnapshotStore
from reliability.metrics import DailySummaryEmitter, OperationalMetrics
from portfolio.exit_manager import PositionExitManager
from portfolio.exit_policy import PositionExitPolicy
from risk.circuit_breaker import CircuitBreaker
from risk.pretrade import PreTradeRiskEngine
from risk.runtime import RuntimeRiskEngine
from scripts.live_preflight import run_preflight
from state.reconciliation import ReconciliationService
from state.store import InMemoryStateStore
from strategies.market_maker import MarketMakerStrategy
from strategies.spike import SpikeStrategy


class JsonFormatter(logging.Formatter):
    """Small JSON formatter for structured logs."""

    RESERVED = {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED:
                payload[key] = value
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(config: AppConfig) -> None:
    """Configure application-wide structured logging."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.bot.log_level.value))


@dataclass(slots=True)
class AppServices:
    """Container of bootstrapped runtime services."""

    config: AppConfig
    data_dir: Path
    state_store: InMemoryStateStore
    clob_client: ClobClientAdapter
    ws_manager: WebSocketManager
    market_data_client: MarketDataClient
    strategy: SpikeStrategy
    market_maker: MarketMakerStrategy | None
    pretrade_risk: PreTradeRiskEngine
    runtime_risk: RuntimeRiskEngine
    circuit_breaker: CircuitBreaker
    order_builder: OrderBuilder
    submitter: OrderSubmitter
    tracker: OrderTracker
    router: ExecutionRouter
    exit_manager: PositionExitManager
    journal: JsonlJournal
    snapshots: SnapshotStore
    db: KeyValueSqliteStore
    event_bus: EventBus
    operations_repository: OperationsRepository
    alert_service: AlertService
    notification_worker: NotificationWorker
    retention_manager: RetentionManager
    health_store: HealthSnapshotStore
    operational_metrics: OperationalMetrics
    daily_summary_emitter: DailySummaryEmitter
    reconciliation: ReconciliationService
    market_rotator: Btc15mMarketRotator | None = None


DiscoveryClientFactory = Callable[[AutomaticMarketConfig], DiscoveryClient]


def _position_market_end(
    initial_market: object | None,
    market_rotator: object | None,
    *,
    market_id: str,
    token_id: str,
) -> datetime | None:
    """Return the deadline only when the position matches the current market."""

    current = None
    if market_rotator is not None:
        status = market_rotator.status()
        current = getattr(status, "current_market", None)
    market = current or initial_market
    if market is None:
        return None
    asset_ids = getattr(market, "asset_ids", [])
    if (
        market_id == getattr(market, "condition_id", None)
        or token_id in asset_ids
    ):
        return getattr(market, "end_at", None)
    return None


def _complement_token(
    initial_market: object | None,
    market_rotator: object | None,
    *,
    market_id: str,
    token_id: str,
) -> str | None:
    """
    Return the other outcome token of the same market, when it is known.

    A binary market's two tokens price at p and 1 - p, so buying one is the
    same trade as selling the other. This lookup is what lets a sell-side
    signal execute without holding inventory.
    """

    current = None
    if market_rotator is not None:
        status = market_rotator.status()
        current = getattr(status, "current_market", None)
    for market in (current, initial_market):
        if market is None:
            continue
        asset_ids = list(getattr(market, "asset_ids", []) or [])
        if token_id not in asset_ids:
            continue
        if market_id not in {
            getattr(market, "condition_id", None),
            getattr(market, "market_id", None),
        }:
            continue
        others = [candidate for candidate in asset_ids if candidate != token_id]
        if len(others) == 1:
            return others[0]
    return None


async def _rotation_safe(
    state_store: InMemoryStateStore,
    market: object,
    config: AppConfig,
) -> bool:
    """Rotation is safe only when no sellable position remains in the ending market.

    Positions carry the CLOB condition identifier from WebSocket data, so match
    against ``condition_id`` (or outcome-token membership) rather than the
    discovery-layer ``market_id``.
    """

    outcome_token_ids = {
        token_id
        for token_id in (
            getattr(getattr(market, "up", None), "token_id", None),
            getattr(getattr(market, "down", None), "token_id", None),
        )
        if token_id
    }
    condition_id = getattr(market, "condition_id", None)
    for position in await state_store.get_positions():
        in_ending_market = (
            (condition_id is not None and position.market_id == condition_id)
            or (bool(outcome_token_ids) and position.token_id in outcome_token_ids)
        )
        if in_ending_market and position.quantity >= config.execution.min_order_size:
            return False
    return True


class LivePreflightError(RuntimeError):
    """Safe live-start failure carrying only failed check names."""

    def __init__(self, failed_checks: tuple[str, ...]) -> None:
        self.failed_checks = failed_checks
        super().__init__("live_preflight_failed")


async def bootstrap_app(
    config_dir: str | Path | None = None,
    *,
    discovery_client_factory: DiscoveryClientFactory = GammaMarketDiscoveryClient,
    process_services: ProcessReliabilityServices | None = None,
) -> AppServices:
    """Load config, configure logging, and wire all runtime services."""

    config = load_config(config_dir)
    configure_logging(config)

    data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
    journal = JsonlJournal(
        data_dir / "journal" / "events.jsonl",
        rotate_bytes=int(config.reliability.journal_rotation_mib * 1024 * 1024),
        retention_days=config.reliability.journal_retention_days,
        total_limit_bytes=int(
            config.reliability.journal_total_limit_mib * 1024 * 1024
        ),
    )
    snapshots = SnapshotStore(data_dir / "snapshots" / "state.json")
    db = KeyValueSqliteStore(data_dir / "bot.sqlite3")

    state_store = InMemoryStateStore(
        mode=config.bot.mode,
        kill_switch_active=config.bot.kill_switch_on_startup,
        fee_rate=config.execution.fee_rate,
    )
    await snapshots.restore_into_state(
        state_store,
        restore_heartbeats=False,
        restore_positions=not is_live_mode(config.bot.mode),
    )
    tracker = OrderTracker(
        state_store,
        snapshots=snapshots,
        confirmation_grace_seconds=(
            config.position_management.position_confirmation_grace_seconds
        ),
    )
    circuit_breaker = CircuitBreaker(
        failure_threshold=config.risk.circuit_breaker_failures,
        window_seconds=config.risk.circuit_breaker_window_seconds,
        cooldown_seconds=config.risk.circuit_breaker_cooldown_seconds,
    )

    discovery_client: DiscoveryClient | None = None
    market_rotator: Btc15mMarketRotator | None = None
    initial_market = None
    asset_ids = config.market_data.subscribed_token_ids or None
    if config.market_data.automatic_market.enabled:
        try:
            discovery_client = discovery_client_factory(
                config.market_data.automatic_market
            )
            initial_market = await discovery_client.discover_active(
                now=datetime.now(tz=UTC)
            )
        except Exception as exc:
            if discovery_client is not None:
                with suppress(Exception):
                    await discovery_client.close()
            if is_live_mode(config.bot.mode):
                raise LivePreflightError(("market_discovery",)) from exc
            raise
        asset_ids = initial_market.asset_ids

    def position_market_end_lookup(
        market_id: str,
        token_id: str,
    ) -> datetime | None:
        return _position_market_end(
            initial_market,
            market_rotator,
            market_id=market_id,
            token_id=token_id,
        )

    def complement_token_lookup(market_id: str, token_id: str) -> str | None:
        return _complement_token(
            initial_market,
            market_rotator,
            market_id=market_id,
            token_id=token_id,
        )

    try:
        try:
            credentials = build_clob_credentials(config)
        except Exception as exc:
            if is_live_mode(config.bot.mode):
                raise LivePreflightError(("credentials_complete",)) from exc
            raise
        if is_live_mode(config.bot.mode):
            try:
                clob_client = ClobClientAdapter.from_v2(
                    config=config,
                    credentials=credentials,
                )
                positions_client = DataApiClient(config)
                geoblock = GeoblockClient(config)
                reconciliation_probe = ReconciliationService(
                    state_store=state_store,
                    mode=config.bot.mode,
                    open_orders_reader=clob_client,
                    positions_reader=positions_client,
                    funder_address=credentials.proxy_address,
                    apply_fill=tracker.handle_order_result,
                    market_end_lookup=position_market_end_lookup,
                    require_position_market_end=(
                        config.market_data.automatic_market.enabled
                    ),
                    min_order_size=config.execution.min_order_size,
                )
            except Exception as exc:
                raise LivePreflightError(("client_initialization",)) from exc

            async def reconcile_probe() -> bool:
                report = await reconciliation_probe.reconcile_startup()
                return report.ok

            preflight = await run_preflight(
                config=config,
                credentials=credentials,
                adapter=clob_client,
                positions_client=positions_client,
                geoblock=geoblock,
                reconcile=reconcile_probe,
                subscribed_token_ids=asset_ids,
            )
            if not preflight.ok:
                failed = tuple(
                    check.name for check in preflight.checks if not check.passed
                )
                raise LivePreflightError(failed)
        else:
            clob_client = ClobClientAdapter.disabled()
    except Exception:
        if discovery_client is not None:
            await discovery_client.close()
        raise

    event_bus = EventBus()
    process_services = process_services or build_process_reliability_services(
        config=config,
        data_dir=data_dir,
    )
    operations_repository = process_services.repository
    alert_service = process_services.alerts
    notification_worker = process_services.notification_worker
    retention_manager = RetentionManager(
        repository=operations_repository,
        config=config.reliability,
        journal=journal,
        data_path=data_dir,
        delivered_outbox_retention_days=(
            config.notifications.delivered_outbox_retention_days
        ),
    )
    health_store = HealthSnapshotStore(data_dir / "health" / "runtime.json")

    import shutil

    def _disk_percent() -> float:
        total, _used, free = shutil.disk_usage(data_dir)
        if total <= 0:
            return 0.0
        return (total - free) / total * 100.0

    async def _authoritative_pnl() -> tuple[Decimal, Decimal]:
        realized = sum(
            (await state_store.get_realized_pnl_by_day()).values(),
            start=Decimal("0"),
        )
        unrealized = sum(
            (position.unrealized_pnl for position in await state_store.get_positions()),
            start=Decimal("0"),
        )
        return realized, unrealized

    async def _operational_state() -> str:
        state, _reason = await state_store.get_operational_state()
        return state.value

    async def _pending_alerts() -> int:
        depth, _age = await operations_repository.outbox_stats(
            now=datetime.now(tz=UTC)
        )
        return int(depth)

    async def _lease_remaining() -> float | None:
        lease = await operations_repository.get_active_lease()
        if lease is None:
            return None
        return max(0.0, (lease.expires_at - datetime.now(tz=UTC)).total_seconds())

    operational_metrics = OperationalMetrics(
        repository=operations_repository,
        pnl_provider=_authoritative_pnl,
        state_provider=_operational_state,
        outbox_pending_provider=_pending_alerts,
        disk_percent_provider=_disk_percent,
        lease_remaining_seconds_provider=_lease_remaining,
    )
    daily_summary_emitter = DailySummaryEmitter(
        metrics=operational_metrics,
        alert_service=alert_service,
        repository=operations_repository,
        hour_utc=config.notifications.daily_summary_hour_utc,
    )

    async def emit_event(event: BotEvent) -> None:
        await journal.append(event)
        await event_bus.publish(event)

    async def durable_alert(event: BotEvent) -> None:
        await alert_service.enqueue_event(event)

    event_bus.subscribe(durable_alert)
    event_bus.subscribe(operational_metrics.record_event)

    strategy_config = config.spike_strategy
    if config.market_data.automatic_market.enabled:
        strategy_config = strategy_config.model_copy(
            update={"target_token_ids": []}
        )
    strategy = SpikeStrategy(
        strategy_config,
        complement_provider=complement_token_lookup,
        tick_size_provider=(
            clob_client.get_tick_size if is_live_mode(config.bot.mode) else None
        ),
    )
    market_maker: MarketMakerStrategy | None = None
    if config.market_maker.enabled:
        market_maker_config = config.market_maker
        if config.market_data.automatic_market.enabled:
            market_maker_config = market_maker_config.model_copy(
                update={"target_token_ids": []}
            )
        market_maker = MarketMakerStrategy(
            market_maker_config,
            position_reader=state_store.get_position,
            tick_size_provider=(
                clob_client.get_tick_size
                if is_live_mode(config.bot.mode)
                else None
            ),
        )
    pretrade_risk = PreTradeRiskEngine(config=config, state_store=state_store)
    runtime_risk = RuntimeRiskEngine(
        config=config,
        state_store=state_store,
        circuit_breaker=circuit_breaker,
    )
    order_builder = OrderBuilder(
        config,
        tick_size_provider=(
            clob_client.get_tick_size if is_live_mode(config.bot.mode) else None
        ),
        min_size_provider=(
            clob_client.get_minimum_order_size
            if is_live_mode(config.bot.mode)
            else None
        ),
    )
    submitter = OrderSubmitter(
        config=config,
        clob_client=clob_client,
        circuit_breaker=circuit_breaker,
    )
    reconciliation = ReconciliationService(
        state_store=state_store,
        mode=config.bot.mode,
        open_orders_reader=clob_client,
        positions_reader=positions_client if is_live_mode(config.bot.mode) else None,
        funder_address=credentials.proxy_address,
        apply_fill=tracker.handle_order_result,
        market_end_lookup=position_market_end_lookup,
        require_position_market_end=(
            config.market_data.automatic_market.enabled
        ),
        min_order_size=config.execution.min_order_size,
    )
    router = ExecutionRouter(
        config=config,
        state_store=state_store,
        risk_engine=pretrade_risk,
        order_builder=order_builder,
        submitter=submitter,
        tracker=tracker,
        journal=journal,
        event_bus=event_bus,
        post_fill_reconcile=reconciliation.reconcile_runtime,
        snapshots=snapshots,
    )
    exit_policy = PositionExitPolicy(
        config.position_management,
        min_order_size=config.execution.min_order_size,
        max_data_age_seconds=config.risk.max_data_staleness_seconds,
        tick_size_provider=(
            clob_client.get_tick_size if is_live_mode(config.bot.mode) else None
        ),
        default_tick_size=config.execution.default_tick_size,
    )
    exit_manager = PositionExitManager(
        config=config,
        state_store=state_store,
        snapshots=snapshots,
        policy=exit_policy,
        on_event=emit_event,
        cancel_order=submitter.cancel_order,
    )
    async def on_snapshot(snapshot) -> None:  # type: ignore[no-untyped-def]
        market_end_at = position_market_end_lookup(
            snapshot.market_id,
            snapshot.token_id,
        )
        for exit_signal in await exit_manager.on_market_update(
            snapshot, market_end_at=market_end_at
        ):
            await router.route_signal(
                exit_signal, snapshot=snapshot, market_end_at=market_end_at
            )
        if market_maker is not None:
            quote_plan = await market_maker.plan_quotes(
                snapshot, market_end_at=market_end_at
            )
            if not quote_plan.empty:
                await router.route_quote_plan(
                    quote_plan,
                    strategy=market_maker,
                    snapshot=snapshot,
                    market_end_at=market_end_at,
                )
        for signal in await strategy.on_market_update(snapshot):
            if signal.side.value == "sell":
                converted = await exit_manager.from_strategy_signal(
                    signal, snapshot=snapshot, market_end_at=market_end_at
                )
                if converted is not None:
                    await router.route_signal(
                        converted, snapshot=snapshot, market_end_at=market_end_at
                    )
                continue
            await router.route_signal(
                signal, snapshot=snapshot, market_end_at=market_end_at
            )

    try:
        async def on_transport_heartbeat(timestamp: datetime) -> None:
            await state_store.update_heartbeat(
                "market_transport",
                timestamp,
            )

        market_data_client = MarketDataClient(
            state_store=state_store,
            on_snapshot=on_snapshot,
        )
        ws_manager = WebSocketManager(
            url=config.market_data.ws_url,
            on_message=market_data_client.handle_ws_message,
            on_heartbeat=on_transport_heartbeat,
            asset_ids=asset_ids,
            ping_interval_seconds=config.exchange.ws_ping_interval_seconds,
            reconnect_initial_seconds=config.market_data.reconnect_initial_seconds,
            reconnect_max_seconds=config.market_data.reconnect_max_seconds,
        )
        market_rotator = (
            Btc15mMarketRotator(
                config=config.market_data.automatic_market,
                discovery=discovery_client,
                websocket=ws_manager,
                initial_market=initial_market,
                can_rotate=lambda market: _rotation_safe(
                    state_store, market, config
                ),
            )
            if discovery_client is not None and initial_market is not None
            else None
        )

        return AppServices(
            config=config,
            data_dir=data_dir,
            state_store=state_store,
            clob_client=clob_client,
            ws_manager=ws_manager,
            market_data_client=market_data_client,
            strategy=strategy,
            market_maker=market_maker,
            pretrade_risk=pretrade_risk,
            runtime_risk=runtime_risk,
            circuit_breaker=circuit_breaker,
            order_builder=order_builder,
            submitter=submitter,
            tracker=tracker,
            router=router,
            exit_manager=exit_manager,
            journal=journal,
            snapshots=snapshots,
            db=db,
            event_bus=event_bus,
            operations_repository=operations_repository,
            alert_service=alert_service,
            notification_worker=notification_worker,
            retention_manager=retention_manager,
            health_store=health_store,
            operational_metrics=operational_metrics,
            daily_summary_emitter=daily_summary_emitter,
            reconciliation=reconciliation,
            market_rotator=market_rotator,
        )
    except Exception:
        if discovery_client is not None:
            await discovery_client.close()
        raise
