"""App bootstrap and dependency wiring."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.modes import is_live_mode
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
from notifications.telegram import TelegramNotifier
from persistence.db import KeyValueSqliteStore
from persistence.journal import JsonlJournal
from persistence.snapshots import SnapshotStore
from portfolio.exit_manager import PositionExitManager
from portfolio.exit_policy import PositionExitPolicy
from risk.circuit_breaker import CircuitBreaker
from risk.pretrade import PreTradeRiskEngine
from risk.runtime import RuntimeRiskEngine
from scripts.live_preflight import run_preflight
from state.reconciliation import ReconciliationService
from state.store import InMemoryStateStore
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
    state_store: InMemoryStateStore
    clob_client: ClobClientAdapter
    ws_manager: WebSocketManager
    market_data_client: MarketDataClient
    strategy: SpikeStrategy
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
    telegram: TelegramNotifier
    reconciliation: ReconciliationService
    market_rotator: Btc15mMarketRotator | None = None


DiscoveryClientFactory = Callable[[AutomaticMarketConfig], DiscoveryClient]


async def _rotation_safe(
    state_store: InMemoryStateStore,
    market: object,
    config: AppConfig,
) -> bool:
    """Rotation is safe only when no sellable position remains in the ending market."""

    for position in await state_store.get_positions():
        if position.market_id != market.market_id:
            continue
        if position.quantity >= config.execution.min_order_size:
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
) -> AppServices:
    """Load config, configure logging, and wire all runtime services."""

    config = load_config(config_dir)
    configure_logging(config)

    data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
    journal = JsonlJournal(data_dir / "journal" / "events.jsonl")
    snapshots = SnapshotStore(data_dir / "snapshots" / "state.json")
    db = KeyValueSqliteStore(data_dir / "bot.sqlite3")

    state_store = InMemoryStateStore(
        mode=config.bot.mode,
        kill_switch_active=config.bot.kill_switch_on_startup,
    )
    await snapshots.restore_into_state(
        state_store,
        restore_heartbeats=False,
        restore_positions=not is_live_mode(config.bot.mode),
    )
    circuit_breaker = CircuitBreaker(
        failure_threshold=config.risk.circuit_breaker_failures,
        window_seconds=config.risk.circuit_breaker_window_seconds,
        cooldown_seconds=config.risk.circuit_breaker_cooldown_seconds,
    )

    discovery_client: DiscoveryClient | None = None
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
    telegram = TelegramNotifier(config)
    event_bus.subscribe(telegram.notify_event)

    async def emit_event(event: BotEvent) -> None:
        await journal.append(event)
        await event_bus.publish(event)

    strategy_config = config.spike_strategy
    if config.market_data.automatic_market.enabled:
        strategy_config = strategy_config.model_copy(
            update={"target_token_ids": []}
        )
    strategy = SpikeStrategy(strategy_config)
    pretrade_risk = PreTradeRiskEngine(config=config, state_store=state_store)
    runtime_risk = RuntimeRiskEngine(
        config=config,
        state_store=state_store,
        circuit_breaker=circuit_breaker,
    )
    order_builder = OrderBuilder(config)
    submitter = OrderSubmitter(
        config=config,
        clob_client=clob_client,
        circuit_breaker=circuit_breaker,
    )
    tracker = OrderTracker(
        state_store,
        snapshots=snapshots,
        confirmation_grace_seconds=(
            config.position_management.position_confirmation_grace_seconds
        ),
    )
    reconciliation = ReconciliationService(
        state_store=state_store,
        mode=config.bot.mode,
        open_orders_reader=clob_client,
        positions_reader=positions_client if is_live_mode(config.bot.mode) else None,
        funder_address=credentials.proxy_address,
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
    )
    exit_policy = PositionExitPolicy(
        config.position_management,
        min_order_size=config.execution.min_order_size,
        max_data_age_seconds=config.risk.max_data_staleness_seconds,
    )
    exit_manager = PositionExitManager(
        config=config,
        state_store=state_store,
        snapshots=snapshots,
        policy=exit_policy,
        on_event=emit_event,
    )
    market_rotator: Btc15mMarketRotator | None = None

    async def on_snapshot(snapshot) -> None:  # type: ignore[no-untyped-def]
        market_end_at = None
        if market_rotator is not None:
            current = market_rotator.status().current_market
            if current is not None:
                market_end_at = current.end_at
        for exit_signal in await exit_manager.on_market_update(
            snapshot, market_end_at=market_end_at
        ):
            await router.route_signal(
                exit_signal, snapshot=snapshot, market_end_at=market_end_at
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
            state_store=state_store,
            clob_client=clob_client,
            ws_manager=ws_manager,
            market_data_client=market_data_client,
            strategy=strategy,
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
            telegram=telegram,
            reconciliation=reconciliation,
            market_rotator=market_rotator,
        )
    except Exception:
        if discovery_client is not None:
            await discovery_client.close()
        raise
