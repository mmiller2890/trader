from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.cli import main as backtest_main
from backtest.conftest import NOW, delta_event, snapshot_event
from models.market import MarketSnapshot


def full_book_payload(*, sequence: int) -> dict[str, object]:
    return snapshot_event(sequence=sequence).model_dump(mode="json")


def delta_payload(
    *,
    sequence: int,
    ask_updates: list[tuple[str, str]] | None = None,
) -> dict[str, object]:
    return delta_event(sequence=sequence, ask_updates=ask_updates).model_dump(mode="json")


def cli_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bot.yaml").write_text("bot:\n  mode: dry_run\n", encoding="utf-8")
    return config_dir, tmp_path / "events.json", tmp_path / "result.json"


def run_cli(*, config_dir: Path, input_path: Path, output_path: Path) -> int:
    return backtest_main([
        "--snapshots", str(input_path),
        "--config-dir", str(config_dir),
        "--output", str(output_path),
    ])


def test_cli_accepts_legacy_snapshots_and_emits_capital_metrics(tmp_path: Path) -> None:
    config_dir, input_path, output_path = cli_paths(tmp_path)
    legacy = []
    for index, price_text in enumerate(("0.50", "0.50", "0.50", "0.56")):
        price = Decimal(price_text)
        legacy.append(MarketSnapshot(
            market_id="m1",
            token_id="t1",
            best_bid=price - Decimal("0.01"),
            best_ask=price + Decimal("0.01"),
            mid_price=price,
            top_bid_size=Decimal("100"),
            top_ask_size=Decimal("100"),
            source_ts=NOW + timedelta(seconds=index),
            received_ts=NOW + timedelta(seconds=index),
        ).model_dump(mode="json"))
    input_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert run_cli(config_dir=config_dir, input_path=input_path, output_path=output_path) == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["metrics"]["starting_cash"] == "1000"
    assert "execution_reports" in payload
    assert "portfolio_snapshots" in payload


def test_cli_accepts_snapshot_and_delta_events(tmp_path: Path) -> None:
    config_dir, input_path, output_path = cli_paths(tmp_path)
    input_path.write_text(json.dumps([
        full_book_payload(sequence=10),
        delta_payload(sequence=11, ask_updates=[("0.51", "0"), ("0.52", "4")]),
    ]), encoding="utf-8")
    assert run_cli(config_dir=config_dir, input_path=input_path, output_path=output_path) == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(payload["portfolio_snapshots"]) == 2


def test_cli_returns_two_for_sequence_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir, input_path, output_path = cli_paths(tmp_path)
    input_path.write_text(json.dumps([
        full_book_payload(sequence=10),
        delta_payload(sequence=12),
    ]), encoding="utf-8")
    assert run_cli(config_dir=config_dir, input_path=input_path, output_path=output_path) == 2
    assert "sequence gap" in capsys.readouterr().err
    assert not output_path.exists()
