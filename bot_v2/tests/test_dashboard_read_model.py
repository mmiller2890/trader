from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime import BotRuntime, RuntimePhase
from clients.gamma_markets import DiscoveredMarket, MarketOutcome
from clients.market_rotation import MarketRotationState, MarketRotationStatus
from config.schema import AppConfig, Mode
from dashboard.read_model import DashboardReadModel, tail_events
from models.events import BotEvent, EventType
from models.order import OrderResult, OrderStatus
from models.position import ExitReason, Position, PositionLifecycle
from persistence.journal import JsonlJournal
from persistence.snapshots import SnapshotStore
from state.store import InMemoryStateStore


NOW = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)


def automatic_market() -> DiscoveredMarket:
    start = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
    return DiscoveredMarket(
        event_id="event-auto",
        market_id="market-auto",
        condition_id="condition-auto",
        slug="btc-updown-15m-1787540400",
        title="Bitcoin Up or Down - August 24, 3:00AM-3:15AM ET",
        start_at=start,
        end_at=start + timedelta(minutes=15),
        up=MarketOutcome(name="Up", token_id="111"),
        down=MarketOutcome(name="Down", token_id="222"),
    )


class FakeRotator:
    def status(self) -> MarketRotationStatus:
        return MarketRotationStatus(
            state=MarketRotationState.HEALTHY,
            current_market=automatic_market(),
            last_success_at=datetime(2026, 8, 24, 3, 0, tzinfo=UTC),
            reason="market_discovered",
        )


async def read_model_for_position(
    *,
    quantity: str,
    average: str,
    mark: str,
    opened_at: datetime,
    end_at: datetime | None = None,
    pending_exit: str | None = None,
    reason: ExitReason | None = None,
    minimum: str = "1",
) -> DashboardReadModel:
    config = AppConfig(execution={"min_order_size": minimum})
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.set_position(
        Position(
            market_id="m1",
            token_id="t1",
            quantity=Decimal(quantity),
            average_entry_price=Decimal(average),
            mark_price=Decimal(mark),
        )
    )
    state._lifecycles[("m1", "t1")] = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=opened_at,
        last_fill_at=opened_at,
        market_end_at=end_at,
        pending_exit_client_order_id=pending_exit,
        last_exit_reason=reason,
    )
    runtime = BotRuntime(config_loader=lambda _: config)
    runtime._phase = RuntimePhase.RUNNING
    runtime._mode = Mode.DRY_RUN
    runtime._services = SimpleNamespace(state_store=state, config=config)
    return DashboardReadModel(
        config=config,
        runtime=runtime,
        data_dir=Path("/tmp"),
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_dashboard_exposes_managed_exit_state() -> None:
    model = await read_model_for_position(
        quantity="2", average="0.40", mark="0.44",
        opened_at=NOW - timedelta(seconds=30), end_at=NOW + timedelta(minutes=5),
        pending_exit="exit-order-0001", reason=ExitReason.TAKE_PROFIT,
    )
    state = await model.build()
    managed = state.managed_positions[0]
    assert managed.return_bps == Decimal("1000")
    assert managed.held_seconds == 30
    assert managed.exit_pending is True


@pytest.mark.asyncio
async def test_dashboard_warns_about_dust() -> None:
    model = await read_model_for_position(
        quantity="0.5", average="0.40", mark="0.44",
        opened_at=NOW - timedelta(seconds=30), minimum="1",
    )
    state = await model.build()
    assert "position_dust:m1:t1:0.5" in state.warnings


@pytest.mark.asyncio
async def test_live_read_model_exposes_state_without_secrets(tmp_path: Path) -> None:
    config = AppConfig(
        market_data={"subscribed_token_ids": ["123"]},
        secrets={
            "private_key": "never-return-me",
            "clob_api_key": "key-private",
            "clob_secret": "also-private",
            "clob_passphrase": "pass-private",
            "polymarket_proxy_address": "0x" + "1" * 40,
        },
    )
    state = InMemoryStateStore(mode=Mode.DRY_RUN)
    await state.update_heartbeat("market_data")
    await state.set_order_status(
        OrderResult(
            client_order_id="order-0001",
            status=OrderStatus.SUBMITTED,
            accepted=True,
            requested_size=1,
        )
    )
    await state.set_position(
        Position(
            market_id="m1",
            token_id="123",
            quantity=2,
            mark_price=Decimal("0.25"),
        )
    )
    runtime = BotRuntime(config_loader=lambda _: config)
    runtime._phase = RuntimePhase.RUNNING
    runtime._mode = Mode.DRY_RUN
    runtime._services = SimpleNamespace(state_store=state, config=config)

    model = await DashboardReadModel(config=config, runtime=runtime, data_dir=tmp_path).build()
    payload = model.model_dump_json()

    assert model.source == "live"
    assert model.runtime.phase == RuntimePhase.RUNNING
    assert len(model.open_orders) == 1
    assert len(model.positions) == 1
    assert model.total_exposure == Decimal("0.50")
    assert model.credentials.private_key_configured is True
    assert "never-return-me" not in payload
    assert "key-private" not in payload
    assert "also-private" not in payload
    assert "pass-private" not in payload


@pytest.mark.asyncio
async def test_stopped_read_model_uses_historical_snapshot(tmp_path: Path) -> None:
    config = AppConfig()
    historical = InMemoryStateStore(mode=Mode.DRY_RUN)
    await historical.update_heartbeat(
        "market_data", datetime.now(tz=UTC) - timedelta(minutes=10)
    )
    await SnapshotStore(tmp_path / "snapshots" / "state.json").save_from_state(historical)

    model = await DashboardReadModel(
        config=config,
        runtime=BotRuntime(config_loader=lambda _: config),
        data_dir=tmp_path,
    ).build()

    assert model.source == "historical"
    assert model.heartbeats[0].state == "stale"
    assert model.kill_switch is False


@pytest.mark.asyncio
async def test_stopped_read_model_exposes_last_latched_halt_reason(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    historical = InMemoryStateStore(mode=Mode.DRY_RUN)
    await historical.activate_kill_switch("transport_heartbeat_stale")
    await SnapshotStore(
        tmp_path / "snapshots" / "state.json"
    ).save_from_state(historical)

    model = await DashboardReadModel(
        config=config,
        runtime=BotRuntime(config_loader=lambda _: config),
        data_dir=tmp_path,
    ).build()

    assert model.kill_switch is True
    assert model.kill_switch_reason == "transport_heartbeat_stale"


@pytest.mark.asyncio
async def test_live_read_model_exposes_healthy_automatic_market_without_secrets(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        market_data={"automatic_market": {"enabled": True}},
        secrets={"private_key": "automatic-secret-sentinel"},
    )
    runtime = BotRuntime(config_loader=lambda _: config)
    runtime._phase = RuntimePhase.RUNNING
    runtime._mode = Mode.DRY_RUN
    runtime._services = SimpleNamespace(
        state_store=InMemoryStateStore(mode=Mode.DRY_RUN),
        config=config,
        market_rotator=FakeRotator(),
    )

    model = await DashboardReadModel(
        config=config, runtime=runtime, data_dir=tmp_path
    ).build()

    assert model.market_rotation.enabled is True
    assert model.market_rotation.state == "healthy"
    assert model.market_rotation.slug == "btc-updown-15m-1787540400"
    assert model.market_rotation.up_token_id == "111"
    assert model.market_rotation.down_token_id == "222"
    assert "automatic-secret-sentinel" not in model.model_dump_json()
    readiness = {item.name: item for item in model.readiness}
    assert readiness["subscription"].passed is True
    assert readiness["single_market_scope"].passed is True


@pytest.mark.asyncio
async def test_stopped_automatic_market_reports_starting_without_ids(
    tmp_path: Path,
) -> None:
    config = AppConfig(market_data={"automatic_market": {"enabled": True}})

    model = await DashboardReadModel(
        config=config,
        runtime=BotRuntime(config_loader=lambda _: config),
        data_dir=tmp_path,
    ).build()

    assert model.market_rotation.enabled is True
    assert model.market_rotation.state == "starting"
    assert model.market_rotation.slug is None
    assert model.market_rotation.up_token_id is None
    assert model.market_rotation.down_token_id is None


@pytest.mark.asyncio
async def test_readiness_lists_live_start_and_subscription_blockers(tmp_path: Path) -> None:
    config = AppConfig()
    model = await DashboardReadModel(
        config=config,
        runtime=BotRuntime(config_loader=lambda _: config),
        data_dir=tmp_path,
    ).build()

    failed_names = {item.name for item in model.readiness if not item.passed}
    assert "live_start" in failed_names
    assert "subscription" in failed_names


@pytest.mark.asyncio
async def test_tail_events_ignores_malformed_lines_without_leaking_them(tmp_path: Path) -> None:
    journal = JsonlJournal(tmp_path / "events.jsonl")
    await journal.append(
        BotEvent(
            event_type=EventType.BOT_STARTED,
            component="app",
            mode="dry_run",
            message="started",
        )
    )
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write('not-json-with-secret-value\n')

    result = tail_events(journal.path, limit=10)

    assert len(result.events) == 1
    assert result.malformed_count == 1
    assert "secret-value" not in result.model_dump_json()


def test_tail_events_redacts_known_secrets_in_valid_events(tmp_path: Path) -> None:
    journal_path = tmp_path / "events.jsonl"
    event = BotEvent(
        event_type=EventType.KILL_SWITCH_TRIPPED,
        component="runtime",
        mode="dry_run",
        message="failure contained api-secret-value",
        reason="sdk_error:api-secret-value",
    )
    journal_path.write_text(event.model_dump_json() + "\n", encoding="utf-8")

    result = tail_events(
        journal_path, limit=10, redactions=["api-secret-value"]
    )

    assert "api-secret-value" not in result.model_dump_json()
    assert "[REDACTED]" in result.model_dump_json()
