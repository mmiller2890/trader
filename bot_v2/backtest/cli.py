"""Command-line runner for deterministic historical backtests."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from pydantic import TypeAdapter

from backtest.models import BookSnapshotEvent, HistoricalBookEvent
from backtest.replay import BacktestEngine, ReplayResult
from config.loader import ConfigError, load_config
from config.schema import Mode
from models.market import MarketSnapshot, OrderBookLevel
from strategies.spike import SpikeStrategy


event_adapter = TypeAdapter(HistoricalBookEvent)


def _load_events(path: Path) -> list[HistoricalBookEvent]:
    """Read a JSON array of legacy snapshots or normalized book events."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read snapshots file: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in snapshots file: {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("snapshots file must contain a JSON array")
    events: list[HistoricalBookEvent] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"historical event at index {index} must be an object")
        if "event_type" in item:
            events.append(event_adapter.validate_python(item))
        else:
            snapshot = MarketSnapshot.model_validate(item)
            events.append(
                BookSnapshotEvent(
                    market_id=snapshot.market_id,
                    token_id=snapshot.token_id,
                    bids=[OrderBookLevel(price=snapshot.best_bid, size=snapshot.top_bid_size)],
                    asks=[OrderBookLevel(price=snapshot.best_ask, size=snapshot.top_ask_size)],
                    sequence_id=index,
                    source_ts=snapshot.source_ts,
                    received_ts=snapshot.received_ts,
                )
            )
    return events


def _serialize_result(result: ReplayResult) -> dict[str, object]:
    if result.metrics is None:
        raise RuntimeError("backtest completed without metrics")
    return {
        "signals": [signal.model_dump(mode="json") for signal in result.signals],
        "order_results": [order.model_dump(mode="json") for order in result.order_results],
        "execution_reports": [report.model_dump(mode="json") for report in result.execution_reports],
        "positions": [position.model_dump(mode="json") for position in result.positions],
        "equity_curve": [asdict(point) for point in result.equity_curve],
        "portfolio_snapshots": [snapshot.model_dump(mode="json") for snapshot in result.portfolio_snapshots],
        "metrics": asdict(result.metrics),
    }


async def run_backtest(*, snapshots_path: Path, config_dir: Path | None = None) -> ReplayResult:
    """Load config and historical events, then execute the configured spike strategy offline."""

    config = load_config(config_dir)
    config = config.model_copy(
        update={"bot": config.bot.model_copy(update={"mode": Mode.BACKTEST})}
    )
    engine = BacktestEngine(config=config)
    return await engine.run_events(
        strategy=SpikeStrategy(config.spike_strategy),
        events=_load_events(snapshots_path),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an offline backtest over JSON market snapshots.")
    parser.add_argument("--snapshots", required=True, type=Path, help="JSON array of MarketSnapshot values")
    parser.add_argument("--config-dir", type=Path, help="Directory containing bot.yaml and optional fragments")
    parser.add_argument("--output", required=True, type=Path, help="Path for JSON backtest results")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and write results, returning a shell-compatible status code."""

    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(run_backtest(snapshots_path=args.snapshots, config_dir=args.config_dir))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(_serialize_result(result), default=str, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except (ConfigError, RuntimeError, ValueError) as exc:
        print(f"backtest failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
