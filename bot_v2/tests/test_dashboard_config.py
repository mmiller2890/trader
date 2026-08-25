from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config.loader import ConfigError, load_config
from config.schema import Mode
from dashboard.config_editor import EditableConfig, OperatorConfigEditor


def write_base_config(root: Path) -> None:
    (root / "strategies").mkdir(parents=True)
    (root / "bot.yaml").write_text(
        """
bot:
  mode: dry_run
market_data:
  subscribed_token_ids: []
execution:
  dry_run_force: true
  allow_live_trading: false
""",
        encoding="utf-8",
    )
    (root / "strategies" / "spike.yaml").write_text(
        "spike_strategy:\n  target_token_ids: []\n",
        encoding="utf-8",
    )


def test_operator_overlay_changes_only_allowlisted_token_lists(tmp_path: Path) -> None:
    write_base_config(tmp_path)
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: False)

    saved = editor.save(
        EditableConfig(
            subscribed_token_ids=["123", "123", "456"],
            target_token_ids=["456"],
        )
    )
    config = load_config(tmp_path)

    assert saved.subscribed_token_ids == ["123", "456"]
    assert config.market_data.subscribed_token_ids == ["123", "456"]
    assert config.spike_strategy.target_token_ids == ["456"]
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True


def test_operator_overlay_rejects_non_decimal_and_too_many_tokens() -> None:
    with pytest.raises(ValidationError):
        EditableConfig(subscribed_token_ids=["token-x"], target_token_ids=[])
    with pytest.raises(ValidationError):
        EditableConfig(
            subscribed_token_ids=[str(index) for index in range(21)],
            target_token_ids=[],
        )


def test_operator_overlay_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        EditableConfig.model_validate(
            {"subscribed_token_ids": [], "target_token_ids": [], "mode": "live"}
        )


def test_operator_overlay_can_only_be_saved_while_stopped(tmp_path: Path) -> None:
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: True)

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        editor.save(EditableConfig())

    assert not (tmp_path / "operator.yaml").exists()


def test_operator_editor_loads_missing_file_as_empty(tmp_path: Path) -> None:
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: False)

    assert editor.load() == EditableConfig()


def test_operator_editor_atomically_enables_live_and_preserves_scope(
    tmp_path: Path,
) -> None:
    write_base_config(tmp_path)
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: False)
    editor.save(
        EditableConfig(
            subscribed_token_ids=["123"],
            target_token_ids=["123"],
        )
    )

    editor.set_mode(Mode.LIVE)

    config = load_config(tmp_path)
    assert config.bot.mode == Mode.LIVE
    assert config.execution.allow_live_trading is True
    assert config.execution.dry_run_force is False
    assert config.market_data.subscribed_token_ids == ["123"]
    assert config.spike_strategy.target_token_ids == ["123"]


def test_operator_editor_returns_to_dry_run_as_one_bundle(tmp_path: Path) -> None:
    write_base_config(tmp_path)
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: False)
    editor.set_mode(Mode.LIVE)

    editor.set_mode(Mode.DRY_RUN)

    config = load_config(tmp_path)
    assert config.bot.mode == Mode.DRY_RUN
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True


def test_operator_editor_rejects_mode_change_while_running(tmp_path: Path) -> None:
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: True)

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        editor.set_mode(Mode.LIVE)


def test_loader_rejects_partial_live_operator_bundle(tmp_path: Path) -> None:
    write_base_config(tmp_path)
    (tmp_path / "operator.yaml").write_text(
        "bot:\n  mode: live\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="complete mode bundle"):
        load_config(tmp_path)


def test_telegram_switch_round_trips_through_the_overlay(tmp_path: Path) -> None:
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: False)

    editor.save(EditableConfig(telegram_enabled=True))

    assert editor.load().telegram_enabled is True
    payload = yaml.safe_load((tmp_path / "operator.yaml").read_text())
    assert payload["notifications"] == {"telegram_enabled": True}


def test_saving_market_scope_does_not_clear_the_telegram_switch(tmp_path: Path) -> None:
    """The overlay is written whole, so every save must carry the switch."""

    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: False)
    editor.save(EditableConfig(telegram_enabled=True))

    current = editor.load()
    editor.save(
        EditableConfig(
            subscribed_token_ids=["123"],
            target_token_ids=["123"],
            telegram_enabled=current.telegram_enabled,
        )
    )

    assert editor.load().telegram_enabled is True


def test_telegram_switch_cannot_be_changed_while_running(tmp_path: Path) -> None:
    editor = OperatorConfigEditor(tmp_path / "operator.yaml", is_running=lambda: True)

    with pytest.raises(RuntimeError, match="bot_must_be_stopped"):
        editor.save(EditableConfig(telegram_enabled=True))


def test_overlay_with_the_telegram_switch_still_loads(tmp_path: Path) -> None:
    """The loader allowlist must admit the section the editor now writes."""

    (tmp_path / "bot.yaml").write_text(
        "bot:\n  mode: dry_run\nexecution:\n"
        "  dry_run_force: true\n  allow_live_trading: false\n",
        encoding="utf-8",
    )
    (tmp_path / "operator.yaml").write_text(
        "notifications:\n  telegram_enabled: true\n", encoding="utf-8"
    )

    config = load_config(tmp_path)

    assert config.notifications.telegram_enabled is True


def test_operator_overlay_still_refuses_telegram_credentials(tmp_path: Path) -> None:
    """Secrets must never be writable from, or readable by, the dashboard."""

    (tmp_path / "bot.yaml").write_text(
        "bot:\n  mode: dry_run\nexecution:\n"
        "  dry_run_force: true\n  allow_live_trading: false\n",
        encoding="utf-8",
    )
    (tmp_path / "operator.yaml").write_text(
        "notifications:\n  telegram_enabled: true\n"
        "  telegram_bot_token: leaked\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="forbidden keys"):
        load_config(tmp_path)
