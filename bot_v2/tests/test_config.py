from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.loader import ConfigError, load_config
from config.schema import AppConfig


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def test_live_notional_cap_must_not_exceed_large_order_threshold() -> None:
    with pytest.raises(ValidationError):
        AppConfig(execution={"max_live_order_notional": "101"})


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
