from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.loader import ConfigError, load_config
from config.schema import AppConfig, TimeInForce

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_production_position_management_defaults() -> None:
    config = load_config(PROJECT_ROOT / "config")
    policy = config.position_management
    assert policy.take_profit_bps == Decimal("300")
    assert policy.stop_loss_bps == Decimal("200")
    assert policy.max_hold_seconds == 180
    assert policy.exit_before_market_end_seconds == 60
    assert policy.exit_retry_interval_seconds == 2
    assert policy.max_exit_attempts == 3
    assert policy.position_confirmation_grace_seconds == 30
    assert policy.exit_time_in_force == TimeInForce.IOC


@pytest.mark.parametrize(
    "field,value",
    [
        ("take_profit_bps", "0"),
        ("stop_loss_bps", "0"),
        ("max_hold_seconds", 0),
        ("exit_before_market_end_seconds", 0),
        ("exit_retry_interval_seconds", 0),
        ("max_exit_attempts", 0),
        ("position_confirmation_grace_seconds", 0),
    ],
)
def test_position_management_rejects_non_positive_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AppConfig(position_management={field: value})


def test_exchange_defaults_are_production_v2_and_live_stays_off() -> None:
    config = AppConfig()
    assert config.exchange.clob_host == "https://clob.polymarket.com"
    assert config.exchange.data_api_host == "https://data-api.polymarket.com"
    assert config.exchange.chain_id == 137
    assert config.exchange.signature_type == 3
    assert config.exchange.geoblock_url == "https://polymarket.com/api/geoblock"
    assert config.exchange.ws_ping_interval_seconds == 10
    assert config.execution.max_live_order_notional == Decimal("1")
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True


def test_production_yaml_uses_minimal_fok_execution() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    assert config.execution.default_order_size == Decimal("1")
    assert config.execution.time_in_force == TimeInForce.FOK
    assert config.execution.min_live_buy_notional == Decimal("1")
    assert config.execution.max_live_order_notional == Decimal("1.01")
    assert config.risk.max_data_staleness_seconds == 15
    assert config.market_data.heartbeat_timeout_seconds == 30
    assert config.market_data.automatic_market.enabled is True
    assert config.market_data.automatic_market.asset == "btc"
    assert config.market_data.automatic_market.duration_minutes == 15
    assert config.exchange.signature_type == 1


def test_live_notional_cap_must_not_exceed_large_order_threshold() -> None:
    with pytest.raises(ValidationError):
        AppConfig(execution={"max_live_order_notional": "101"})


def test_share_limit_is_independent_from_marked_notional_limit() -> None:
    config = AppConfig(
        risk={
            "max_single_position_size": "100",
            "max_total_exposure": "10",
        }
    )

    assert config.risk.max_single_position_size == Decimal("100")
    assert config.risk.max_total_exposure == Decimal("10")


def test_load_config_merges_yaml_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_text(
        tmp_path / "bot.yaml",
        """
bot:
  mode: dry_run
execution:
  dry_run_force: true
  allow_live_trading: false
""".strip(),
    )
    write_text(
        tmp_path / "risk.yaml",
        """
risk:
  max_open_orders: 7
""".strip(),
    )
    write_text(
        tmp_path / "strategies" / "spike.yaml",
        """
spike_strategy:
  spike_threshold_bps: 125
""".strip(),
    )
    monkeypatch.setenv("CLOB_API_KEY", "abc123")

    config = load_config(tmp_path)

    assert config.bot.mode.value == "dry_run"
    assert config.risk.max_open_orders == 7
    assert config.spike_strategy.spike_threshold_bps == 125
    assert config.secrets.clob_api_key is not None
    assert config.secrets.clob_api_key.get_secret_value() == "abc123"


def test_live_mode_validation_fails_without_explicit_guard(tmp_path: Path) -> None:
    write_text(
        tmp_path / "bot.yaml",
        """
bot:
  mode: live
execution:
  dry_run_force: true
  allow_live_trading: false
""".strip(),
    )

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_reliability_defaults_match_unattended_operations_spec() -> None:
    config = AppConfig()
    reliability = config.reliability
    assert reliability.live_lease_hours == 72
    assert reliability.task_restart_limit == 3
    assert reliability.task_restart_window_seconds == 600
    assert reliability.degraded_alert_after_seconds == 120
    assert reliability.authoritative_state_halt_after_seconds == 300
    assert reliability.rest_fallback_after_seconds == 30
    assert reliability.retry_initial_seconds == 1
    assert reliability.retry_max_seconds == 30
    assert reliability.retry_jitter_ratio == 0.20
    assert reliability.disk_warning_percent == 80
    assert reliability.disk_degraded_percent == 90
    assert reliability.disk_halt_percent == 95
    assert config.notifications.durable_outbox_enabled is True
    assert config.notifications.telegram_deduplication_seconds == 900
    assert config.notifications.alert_retry_max_seconds == 300


@pytest.mark.parametrize(
    "payload",
    [
        {"live_lease_hours": 0},
        {"live_lease_hours": 169},
        {"retry_initial_seconds": 31, "retry_max_seconds": 30},
        {"retry_jitter_ratio": 1.01},
        {"disk_warning_percent": 91, "disk_degraded_percent": 90},
        {"disk_degraded_percent": 96, "disk_halt_percent": 95},
    ],
)
def test_reliability_rejects_unsafe_bounds(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AppConfig(reliability=payload)
