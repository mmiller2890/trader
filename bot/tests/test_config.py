from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import ConfigError, load_config


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
