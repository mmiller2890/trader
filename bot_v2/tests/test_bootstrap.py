from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bootstrap import LivePreflightError, bootstrap_app
from clients.gamma_markets import DiscoveredMarket, MarketOutcome
from config.schema import AppConfig, AutomaticMarketConfig
from models.market import MarketSnapshot
from models.signal import SignalSide, TradeSignal
from notifications.outbox import AlertService
from persistence.operations import OperationsRepository
from reliability.lease import LiveLeaseService
from scripts.live_preflight import LivePreflightReport, PreflightCheck


def write_config(path: Path, *, automatic: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "bot.yaml").write_text(
        f"""
bot:
  mode: dry_run
market_data:
  subscribed_token_ids: ["900", "901"]
  automatic_market:
    enabled: {str(automatic).lower()}
execution:
  dry_run_force: true
  allow_live_trading: false
spike_strategy:
  target_token_ids: ["900", "901"]
risk:
  # These tests predate the edge gate and exercise wiring/discovery/routing
  # plumbing, not cost; their synthetic signals cannot clear cost.
  edge_gate_mode: "off"
""".strip(),
        encoding="utf-8",
    )


def discovered_market() -> DiscoveredMarket:
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    return DiscoveredMarket(
        event_id="event-current",
        market_id="market-current",
        condition_id="condition-current",
        slug="btc-updown-15m-current",
        title="Bitcoin Up or Down",
        start_at=start,
        end_at=start + timedelta(minutes=15),
        up=MarketOutcome(name="Up", token_id="111"),
        down=MarketOutcome(name="Down", token_id="222"),
    )


def write_live_config(path: Path, *, automatic: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "bot.yaml").write_text(
        f"""
bot:
  mode: live
market_data:
  subscribed_token_ids: ["900", "901"]
  automatic_market:
    enabled: {str(automatic).lower()}
execution:
  dry_run_force: false
  allow_live_trading: true
exchange:
  signature_type: 0
""".strip(),
        encoding="utf-8",
    )


def set_live_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("CLOB_API_KEY", "key")
    monkeypatch.setenv("CLOB_SECRET", "secret")
    monkeypatch.setenv("CLOB_PASSPHRASE", "passphrase")


@pytest.mark.asyncio
async def test_bootstrap_reuses_process_owned_reliability_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=False)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BOT_DATA_DIR", str(data_dir))
    config = AppConfig()
    repository = OperationsRepository(data_dir / "bot.sqlite3")
    alerts = AlertService(repository, config)
    worker = object()
    process_services = SimpleNamespace(
        repository=repository,
        leases=LiveLeaseService(repository),
        alerts=alerts,
        telegram=object(),
        notification_worker=worker,
    )

    services = await bootstrap_app(
        config_dir,
        process_services=process_services,
    )

    assert services.operations_repository is repository
    assert services.alert_service is alerts
    assert services.notification_worker is worker


class FakeDiscovery:
    def __init__(self, config: AutomaticMarketConfig) -> None:
        self.config = config
        self.discover_calls = 0
        self.close_calls = 0

    async def discover_active(self, now: datetime | None = None) -> DiscoveredMarket:
        assert now is not None
        self.discover_calls += 1
        return discovered_market()

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_live_bootstrap_preflight_receives_discovered_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "bot.yaml").write_text(
        """
bot:
  mode: live
market_data:
  subscribed_token_ids: []
  automatic_market:
    enabled: true
execution:
  dry_run_force: false
  allow_live_trading: true
exchange:
  signature_type: 0
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setenv("CLOB_API_KEY", "key")
    monkeypatch.setenv("CLOB_SECRET", "secret")
    monkeypatch.setenv("CLOB_PASSPHRASE", "passphrase")
    created: list[FakeDiscovery] = []
    observed_tokens: list[str] | None = None

    def factory(config: AutomaticMarketConfig) -> FakeDiscovery:
        client = FakeDiscovery(config)
        created.append(client)
        return client

    class Adapter:
        @classmethod
        def from_v2(cls, **_: object) -> object:
            return object()

    async def capture_preflight(**kwargs: object) -> LivePreflightReport:
        nonlocal observed_tokens
        observed_tokens = list(kwargs.get("subscribed_token_ids") or [])
        return LivePreflightReport(
            ok=False,
            checks=[
                PreflightCheck(name="sentinel", passed=False, reason="sentinel")
            ],
        )

    monkeypatch.setattr("app.bootstrap.ClobClientAdapter", Adapter)
    monkeypatch.setattr("app.bootstrap.DataApiClient", lambda _: object())
    monkeypatch.setattr("app.bootstrap.GeoblockClient", lambda _: object())
    monkeypatch.setattr("app.bootstrap.run_preflight", capture_preflight)

    with pytest.raises(LivePreflightError) as exc_info:
        await bootstrap_app(config_dir, discovery_client_factory=factory)

    assert exc_info.value.failed_checks == ("sentinel",)

    assert observed_tokens == ["111", "222"]
    assert len(created) == 1
    assert created[0].close_calls == 1


@pytest.mark.asyncio
async def test_market_ticks_are_not_written_to_operator_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=False)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("BOT_DATA_DIR", str(data_dir))
    services = await bootstrap_app(config_dir)

    await services.market_data_client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "900",
        "bids": [{"price": "0.49", "size": "100"}],
        "asks": [{"price": "0.51", "size": "100"}],
        "timestamp": "1757908892351",
    })

    assert not services.journal.path.exists()


@pytest.mark.asyncio
async def test_live_bootstrap_names_market_discovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_live_config(config_dir, automatic=True)
    set_live_credentials(monkeypatch)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))

    class FailingDiscovery(FakeDiscovery):
        async def discover_active(
            self, now: datetime | None = None
        ) -> DiscoveredMarket:
            raise RuntimeError("remote response with account details")

    with pytest.raises(LivePreflightError) as exc_info:
        await bootstrap_app(
            config_dir,
            discovery_client_factory=FailingDiscovery,
        )

    assert exc_info.value.failed_checks == ("market_discovery",)
    assert "account details" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_live_bootstrap_names_discovery_constructor_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_live_config(config_dir, automatic=True)
    set_live_credentials(monkeypatch)

    def failing_factory(_: AutomaticMarketConfig) -> FakeDiscovery:
        raise RuntimeError("constructor account details")

    with pytest.raises(LivePreflightError) as exc_info:
        await bootstrap_app(
            config_dir,
            discovery_client_factory=failing_factory,
        )

    assert exc_info.value.failed_checks == ("market_discovery",)


@pytest.mark.asyncio
async def test_discovery_cleanup_failure_cannot_mask_canonical_live_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_live_config(config_dir, automatic=True)
    set_live_credentials(monkeypatch)

    class FailingDiscoveryAndClose(FakeDiscovery):
        async def discover_active(
            self, now: datetime | None = None
        ) -> DiscoveredMarket:
            raise RuntimeError("discovery account details")

        async def close(self) -> None:
            raise RuntimeError("cleanup account details")

    with pytest.raises(LivePreflightError) as exc_info:
        await bootstrap_app(
            config_dir,
            discovery_client_factory=FailingDiscoveryAndClose,
        )

    assert exc_info.value.failed_checks == ("market_discovery",)


@pytest.mark.asyncio
async def test_live_bootstrap_names_credential_derivation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_live_config(config_dir)
    set_live_credentials(monkeypatch)
    monkeypatch.setenv("PRIVATE_KEY", "invalid-private-key")
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(LivePreflightError) as exc_info:
        await bootstrap_app(config_dir)

    assert exc_info.value.failed_checks == ("credentials_complete",)


@pytest.mark.asyncio
async def test_live_bootstrap_names_client_initialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_live_config(config_dir)
    set_live_credentials(monkeypatch)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))

    class FailingAdapter:
        @classmethod
        def from_v2(cls, **_: object) -> object:
            raise RuntimeError("SDK response with account details")

    monkeypatch.setattr("app.bootstrap.ClobClientAdapter", FailingAdapter)

    with pytest.raises(LivePreflightError) as exc_info:
        await bootstrap_app(config_dir)

    assert exc_info.value.failed_checks == ("client_initialization",)
    assert "account details" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_bootstrap_discovers_automatic_market_before_runtime_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=True)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    created: list[FakeDiscovery] = []

    def factory(config: AutomaticMarketConfig) -> FakeDiscovery:
        client = FakeDiscovery(config)
        created.append(client)
        return client

    services = await bootstrap_app(config_dir, discovery_client_factory=factory)

    assert len(created) == 1
    assert created[0].discover_calls == 1
    assert services.market_rotator is not None
    assert services.market_rotator.status().current_market == discovered_market()
    assert services.ws_manager.asset_ids == ["111", "222"]
    assert services.strategy._config.target_token_ids == []
    await services.market_rotator.stop()


@pytest.mark.asyncio
async def test_bootstrap_static_mode_skips_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=False)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))

    def unexpected_factory(config: AutomaticMarketConfig) -> FakeDiscovery:
        raise AssertionError("static mode must not construct discovery")

    services = await bootstrap_app(
        config_dir, discovery_client_factory=unexpected_factory
    )

    assert services.market_rotator is None
    assert services.ws_manager.asset_ids == ["900", "901"]
    assert services.strategy._config.target_token_ids == ["900", "901"]


@pytest.mark.asyncio
async def test_snapshot_routes_exit_before_entry_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=False)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    services = await bootstrap_app(config_dir)
    calls: list[str] = []

    async def record_exit(snapshot: object, *, market_end_at: object) -> list[object]:
        calls.append("exit_manager")
        return []

    async def record_strategy(snapshot: object) -> list[object]:
        calls.append("strategy")
        return []

    services.exit_manager.on_market_update = record_exit
    services.strategy.on_market_update = record_strategy

    await services.market_data_client._on_snapshot(
        MarketSnapshot(
            market_id="m1",
            token_id="900",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.51"),
            mid_price=Decimal("0.50"),
            top_bid_size=Decimal("100"),
            top_ask_size=Decimal("100"),
            source_ts=datetime.now(tz=UTC),
            received_ts=datetime.now(tz=UTC),
        )
    )

    assert calls[:2] == ["exit_manager", "strategy"]


@pytest.mark.asyncio
async def test_confirmed_live_fill_reconciles_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=False)
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    services = await bootstrap_app(config_dir)
    reconciliation_calls: list[int] = []

    async def record_reconcile() -> object:
        reconciliation_calls.append(1)
        return SimpleNamespace(ok=True, deferred_positions=[])

    services.router._post_fill_reconcile = record_reconcile

    await services.router.route_signal(
        TradeSignal(
            strategy_name="spike",
            market_id="m1",
            token_id="900",
            side=SignalSide.BUY,
            reference_price=Decimal("0.40"),
            target_price=Decimal("0.50"),
            observed_move_bps=100,
            reason="test",
        ),
        snapshot=MarketSnapshot(
            market_id="m1",
            token_id="900",
            best_bid=Decimal("0.49"),
            best_ask=Decimal("0.50"),
            mid_price=Decimal("0.495"),
            top_bid_size=Decimal("100"),
            top_ask_size=Decimal("100"),
            source_ts=datetime.now(tz=UTC),
            received_ts=datetime.now(tz=UTC),
        ),
    )

    assert reconciliation_calls == [1]


def paired_market() -> DiscoveredMarket:
    return discovered_market()


def test_complement_lookup_returns_the_other_outcome_token() -> None:
    from app.bootstrap import _complement_token

    market = paired_market()
    up, down = market.asset_ids

    assert _complement_token(
        market, None, market_id=market.condition_id, token_id=up
    ) == down
    assert _complement_token(
        market, None, market_id=market.condition_id, token_id=down
    ) == up


def test_complement_lookup_rejects_a_token_from_another_market() -> None:
    from app.bootstrap import _complement_token

    market = paired_market()

    assert _complement_token(
        market, None, market_id=market.condition_id, token_id="999999"
    ) is None


def test_complement_lookup_rejects_a_mismatched_market_id() -> None:
    from app.bootstrap import _complement_token

    market = paired_market()

    assert _complement_token(
        market, None, market_id="some-other-condition", token_id=market.asset_ids[0]
    ) is None


def test_complement_lookup_prefers_the_rotator_current_market() -> None:
    from app.bootstrap import _complement_token

    market = paired_market()
    rotator = SimpleNamespace(
        status=lambda: SimpleNamespace(current_market=market)
    )

    assert _complement_token(
        None, rotator, market_id=market.condition_id, token_id=market.asset_ids[0]
    ) == market.asset_ids[1]


def test_complement_lookup_is_none_without_any_market() -> None:
    from app.bootstrap import _complement_token

    assert _complement_token(None, None, market_id="m", token_id="t") is None


@pytest.mark.asyncio
async def test_exit_manager_can_cancel_its_own_resting_maker_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The maker-exit deadline sweep is inert unless bootstrap hands the exit
    manager a canceller. Task 8 shipped a policy field that nothing consumed;
    this asserts the same gap cannot reopen for the escalation path.
    """

    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    write_config(config_dir, automatic=False)

    services = await bootstrap_app(config_dir)

    assert services.exit_manager._cancel_order is not None
    assert services.exit_manager._cancel_order == services.submitter.cancel_order
