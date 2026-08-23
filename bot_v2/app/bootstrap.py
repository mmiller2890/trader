"""App bootstrap and dependency wiring."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.modes import is_live_mode
from clients.auth import build_clob_credentials
from clients.clob_client import ClobClientAdapter
from clients.data_api import DataApiClient
from clients.geoblock import GeoblockClient
from clients.market_data_client import MarketDataClient
from clients.ws_client import WebSocketManager
from config.loader import load_config
from config.schema import AppConfig
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
    journal: JsonlJournal
    snapshots: SnapshotStore
    db: KeyValueSqliteStore
    event_bus: EventBus
    telegram: TelegramNotifier
    reconciliation: ReconciliationService


async def bootstrap_app(config_dir: str | Path | None = None) -> AppServices:
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
    circuit_breaker = CircuitBreaker(
        failure_threshold=config.risk.circuit_breaker_failures,
        window_seconds=config.risk.circuit_breaker_window_seconds,
        cooldown_seconds=config.risk.circuit_breaker_cooldown_seconds,
    )

    credentials = build_clob_credentials(config)
    if is_live_mode(config.bot.mode):
        clob_client = ClobClientAdapter.from_v2(config=config, credentials=credentials)
        positions_client = DataApiClient(config)
        geoblock = GeoblockClient(config)
        state_for_reconciliation = InMemoryStateStore(mode=config.bot.mode)
        reconciliation_probe = ReconciliationService(
            state_store=state_for_reconciliation,
            mode=config.bot.mode,
            open_orders_reader=clob_client,
        )

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
        )
        if not preflight.ok:
            failed = [check.name for check in preflight.checks if not check.passed]
            raise RuntimeError(f"live startup blocked by preflight: {failed}")
    else:
        clob_client = ClobClientAdapter.disabled()

    event_bus = EventBus()
    telegram = TelegramNotifier(config)
    event_bus.subscribe(telegram.notify_event)

    strategy = SpikeStrategy(config.spike_strategy)
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
    tracker = OrderTracker(state_store)
    router = ExecutionRouter(
        config=config,
        state_store=state_store,
        risk_engine=pretrade_risk,
        order_builder=order_builder,
        submitter=submitter,
        tracker=tracker,
        journal=journal,
        event_bus=event_bus,
    )

    async def on_snapshot(snapshot) -> None:  # type: ignore[no-untyped-def]
        event = BotEvent(
            event_type=EventType.MARKET_UPDATE_RECEIVED,
            component="market_data",
            mode=config.bot.mode.value,
            message="market snapshot received",
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
        )
        await journal.append(event)
        await event_bus.publish(event)
        signals = await strategy.on_market_update(snapshot)
        for signal in signals:
            await router.route_signal(signal, snapshot=snapshot)

    market_data_client = MarketDataClient(
        state_store=state_store,
        on_snapshot=on_snapshot,
    )
    ws_manager = WebSocketManager(
        url=config.market_data.ws_url,
        on_message=market_data_client.handle_ws_message,
        asset_ids=config.market_data.subscribed_token_ids or None,
        ping_interval_seconds=config.exchange.ws_ping_interval_seconds,
        reconnect_initial_seconds=config.market_data.reconnect_initial_seconds,
        reconnect_max_seconds=config.market_data.reconnect_max_seconds,
    )
    reconciliation = ReconciliationService(
        state_store=state_store,
        mode=config.bot.mode,
        open_orders_reader=clob_client,
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
        journal=journal,
        snapshots=snapshots,
        db=db,
        event_bus=event_bus,
        telegram=telegram,
        reconciliation=reconciliation,
    )
