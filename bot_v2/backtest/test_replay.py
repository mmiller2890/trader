from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from backtest.replay import BacktestEngine, ReplayEngine, ReplayResult
from backtest.cli import main as backtest_main
from backtest.conftest import (
    delta_event,
    snapshot_event,
)
from backtest.models import ExecutionStatus
from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.order import OrderResult, OrderStatus
from models.signal import SignalSide, TradeSignal
from strategies.base import StrategyBase
from strategies.spike import SpikeStrategy


def snapshot(*, price: str, at: datetime) -> MarketSnapshot:
    mid_price = Decimal(price)
    return MarketSnapshot(
        market_id="market-1",
        token_id="token-1",
        best_bid=mid_price - Decimal("0.01"),
        best_ask=mid_price + Decimal("0.01"),
        mid_price=mid_price,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=at,
        received_ts=at,
    )


def deep_snapshot_event() -> object:
    return snapshot_event(sequence=1, bids=[("0.49", "100")], asks=[("0.50", "100")])


class BuyOnceStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "buy-once"

    async def on_market_update(self, item: MarketSnapshot) -> list[TradeSignal]:
        if hasattr(self, "_emitted"):
            return []
        self._emitted = True
        return [
            TradeSignal(
                strategy_name=self.name,
                market_id=item.market_id,
                token_id=item.token_id,
                side=SignalSide.BUY,
                reference_price=item.mid_price,
                target_price=item.mid_price,
                observed_move_bps=100,
                created_at=item.received_ts,
                reason="test signal",
            )
        ]

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        return []

    async def on_timer(self) -> list[TradeSignal]:
        return []


class BuyThenSellStrategy(StrategyBase):
    def __init__(self) -> None:
        self._updates = 0

    @property
    def name(self) -> str:
        return "buy-then-sell"

    async def on_market_update(self, item: MarketSnapshot) -> list[TradeSignal]:
        side = SignalSide.BUY if self._updates == 0 else SignalSide.SELL
        self._updates += 1
        return [
            TradeSignal(
                strategy_name=self.name,
                market_id=item.market_id,
                token_id=item.token_id,
                side=side,
                reference_price=item.mid_price,
                target_price=item.mid_price,
                observed_move_bps=100,
                created_at=item.received_ts,
                reason="test signal",
            )
        ]

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        return []

    async def on_timer(self) -> list[TradeSignal]:
        return []


@pytest.mark.asyncio
async def test_backtest_fills_signals_and_marks_open_position() -> None:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5", "min_order_size": "1", "max_order_size": "25"},
    )
    engine = BacktestEngine(config=config)

    result = await engine.run(
        strategy=BuyOnceStrategy(),
        snapshots=[
            snapshot(price="0.50", at=started_at),
            snapshot(price="0.60", at=started_at + timedelta(seconds=1)),
        ],
    )

    assert len(result.signals) == 1
    assert len(result.order_results) == 1
    assert result.order_results[0].accepted is True
    assert result.order_results[0].filled_size == Decimal("5")
    assert result.metrics.filled_order_count == 1
    assert result.metrics.rejected_order_count == 0
    assert result.metrics.unrealized_pnl == Decimal("0.45")
    assert result.positions[0].quantity == Decimal("5")
    assert [point.total_pnl for point in result.equity_curve] == [Decimal("-0.05"), Decimal("0.45")]


@pytest.mark.asyncio
async def test_backtest_records_a_risk_rejection_without_a_fill() -> None:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    config = AppConfig(
        bot={"mode": Mode.BACKTEST, "kill_switch_on_startup": True},
        execution={"default_order_size": "5", "min_order_size": "1", "max_order_size": "25"},
    )
    engine = BacktestEngine(config=config)

    result = await engine.run(strategy=BuyOnceStrategy(), snapshots=[snapshot(price="0.50", at=started_at)])

    assert len(result.order_results) == 1
    assert result.order_results[0].accepted is False
    assert result.metrics.filled_order_count == 0
    assert result.metrics.rejected_order_count == 1
    assert result.positions == []


@pytest.mark.asyncio
async def test_backtest_realizes_pnl_when_a_position_is_closed() -> None:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    config = AppConfig(bot={"mode": Mode.BACKTEST})
    engine = BacktestEngine(config=config)

    result = await engine.run(
        strategy=BuyThenSellStrategy(),
        snapshots=[
            snapshot(price="0.50", at=started_at),
            snapshot(price="0.60", at=started_at + timedelta(seconds=1)),
        ],
    )

    assert result.metrics.filled_order_count == 2
    assert result.metrics.realized_pnl == Decimal("0.40")
    assert result.metrics.unrealized_pnl == Decimal("0")
    assert result.positions[0].quantity == Decimal("0")


@pytest.mark.asyncio
async def test_backtest_uses_snapshot_time_for_strategy_cooldown_and_duplicate_risk() -> None:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        risk={"duplicate_signal_window_seconds": 15},
        spike_strategy={
            "lookback_ticks": 2,
            "spike_threshold_bps": 100,
            "cooldown_seconds": 1,
        },
    )
    engine = BacktestEngine(config=config)

    result = await engine.run(
        strategy=SpikeStrategy(config.spike_strategy),
        snapshots=[
            snapshot(price="0.50", at=started_at),
            snapshot(price="0.50", at=started_at + timedelta(seconds=1)),
            snapshot(price="0.60", at=started_at + timedelta(seconds=2)),
            snapshot(price="0.60", at=started_at + timedelta(seconds=20)),
        ],
    )

    assert result.metrics.filled_order_count == 2
    assert result.metrics.rejected_order_count == 0


@pytest.mark.asyncio
async def test_backtest_processes_unsorted_snapshots_in_historical_order() -> None:
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    engine = BacktestEngine(config=AppConfig(bot={"mode": Mode.BACKTEST}))

    result = await engine.run(
        strategy=BuyOnceStrategy(),
        snapshots=[
            snapshot(price="0.60", at=started_at + timedelta(seconds=1)),
            snapshot(price="0.50", at=started_at),
        ],
    )

    assert result.order_results[0].avg_fill_price == Decimal("0.51")
    assert result.metrics.unrealized_pnl == Decimal("0.45")


def test_backtest_cli_writes_results_for_json_snapshots(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bot.yaml").write_text("bot:\n  mode: dry_run\n", encoding="utf-8")
    snapshot_path = tmp_path / "snapshots.json"
    started_at = datetime(2025, 1, 1, tzinfo=UTC)
    snapshots = [
        snapshot(price=price, at=started_at + timedelta(seconds=index)).model_dump(mode="json")
        for index, price in enumerate(("0.50", "0.50", "0.50", "0.56"))
    ]
    snapshot_path.write_text(json.dumps(snapshots), encoding="utf-8")
    output_path = tmp_path / "result.json"

    exit_code = backtest_main(
        [
            "--snapshots",
            str(snapshot_path),
            "--config-dir",
            str(config_dir),
            "--output",
            str(output_path),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["metrics"]["filled_order_count"] == 1
    assert payload["order_results"][0]["status"] == "filled"


def test_legacy_replay_engine_defaults_to_replay_mode() -> None:
    engine = ReplayEngine()

    assert engine._state_store.mode == Mode.REPLAY


@pytest.mark.asyncio
async def test_backtest_consumes_depth_and_records_partial_fill_and_fees() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5", "max_slippage_bps": 300, "time_in_force": "IOC"},
        backtest={"starting_cash": "100", "taker_fee_bps": "10"},
        risk={"min_top_of_book_liquidity": "1"},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(
        strategy=BuyOnceStrategy(),
        events=[snapshot_event(
            sequence=1,
            bids=[("0.49", "10")],
            asks=[("0.50", "2"), ("0.51", "1")],
        )],
    )
    order = result.order_results[0]
    report = result.execution_reports[0]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.requested_size == Decimal("5")
    assert order.filled_size == Decimal("3")
    assert report.average_fill_price == Decimal("0.5033333333333333333333333333")
    assert report.total_fees == Decimal("0.00151")
    assert result.positions[0].quantity == Decimal("3")
    assert result.metrics.fill_rate == Decimal("0.6")


@pytest.mark.asyncio
async def test_unfunded_quote_does_not_consume_book_or_create_position() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5"},
        backtest={"starting_cash": "1", "taker_fee_bps": "10"},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(strategy=BuyOnceStrategy(), events=[deep_snapshot_event()])
    assert result.order_results[0].status == OrderStatus.REJECTED
    assert result.order_results[0].message == "insufficient_cash"
    assert result.positions == []
    assert result.portfolio_snapshots[-1].cash == Decimal("1")
    assert engine._books[("m1", "t1")].asks[Decimal("0.50")] == Decimal("100")


class FixedIdBuyOnceStrategy(BuyOnceStrategy):
    def __init__(self, signal_id: str = "fixed-signal-0001") -> None:
        self._signal_id = signal_id

    async def on_market_update(self, item: MarketSnapshot) -> list[TradeSignal]:
        signals = await super().on_market_update(item)
        for signal in signals:
            signal.signal_id = self._signal_id
        return signals


class SellOnceStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "sell-once"

    async def on_market_update(self, item: MarketSnapshot) -> list[TradeSignal]:
        if hasattr(self, "_emitted"):
            return []
        self._emitted = True
        return [
            TradeSignal(
                strategy_name=self.name,
                market_id=item.market_id,
                token_id=item.token_id,
                side=SignalSide.SELL,
                reference_price=item.mid_price,
                target_price=item.mid_price,
                observed_move_bps=100,
                created_at=item.received_ts,
                reason="test signal",
            )
        ]

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        return []

    async def on_timer(self) -> list[TradeSignal]:
        return []


class BuyEveryUpdateStrategy(StrategyBase):
    @property
    def name(self) -> str:
        return "buy-every-update"

    async def on_market_update(self, item: MarketSnapshot) -> list[TradeSignal]:
        return [
            TradeSignal(
                strategy_name=self.name,
                market_id=item.market_id,
                token_id=item.token_id,
                side=SignalSide.BUY,
                reference_price=item.mid_price,
                target_price=item.mid_price,
                observed_move_bps=0,
                created_at=item.received_ts,
                reason="test signal",
            )
        ]

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        return []

    async def on_timer(self) -> list[TradeSignal]:
        return []


def serialize_result(result: ReplayResult) -> str:
    from dataclasses import asdict

    return json.dumps(
        {
            "signals": [item.model_dump(mode="json") for item in result.signals],
            "order_results": [item.model_dump(mode="json") for item in result.order_results],
            "execution_reports": [item.model_dump(mode="json") for item in result.execution_reports],
            "positions": [item.model_dump(mode="json") for item in result.positions],
            "equity_curve": [asdict(item) for item in result.equity_curve],
            "portfolio_snapshots": [item.model_dump(mode="json") for item in result.portfolio_snapshots],
            "metrics": asdict(result.metrics) if result.metrics is not None else {},
        },
        default=str,
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_repeated_runs_are_deterministic_and_isolated() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5"},
        backtest={"starting_cash": "100"},
    )
    engine = BacktestEngine(config=config)
    events = [
        snapshot_event(sequence=1, bids=[("0.49", "10")], asks=[("0.50", "10")]),
        snapshot_event(sequence=2, bids=[("0.49", "10")], asks=[("0.50", "10")], at=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC)),
    ]
    first = await engine.run_events(strategy=FixedIdBuyOnceStrategy(), events=events)
    second = await engine.run_events(strategy=FixedIdBuyOnceStrategy(), events=events)
    assert serialize_result(first) == serialize_result(second)
    assert first.positions == second.positions
    assert engine._ledger.positions == {}
    assert first.portfolio_snapshots[0].cash == Decimal("100")


@pytest.mark.asyncio
async def test_book_delta_changes_next_strategy_snapshot_and_fill_depth() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5", "max_slippage_bps": 500},
        backtest={"starting_cash": "100"},
        risk={"min_top_of_book_liquidity": "1", "duplicate_signal_window_seconds": 0},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(
        strategy=BuyEveryUpdateStrategy(),
        events=[
            snapshot_event(sequence=1, bids=[("0.49", "100")], asks=[("0.50", "100")]),
            delta_event(
                sequence=2,
                bid_updates=[("0.495", "100")],
                ask_updates=[("0.50", "0"), ("0.51", "100")],
            ),
        ],
    )
    assert [report.average_fill_price for report in result.execution_reports] == [
        Decimal("0.50"),
        Decimal("0.51"),
    ]
    assert result.order_results[1].filled_size == Decimal("5")


@pytest.mark.asyncio
async def test_fok_insufficient_depth_has_zero_fills_and_no_mutation() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5", "time_in_force": "FOK"},
        backtest={"starting_cash": "100"},
        risk={"min_top_of_book_liquidity": "1"},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(
        strategy=BuyOnceStrategy(),
        events=[snapshot_event(sequence=1, bids=[("0.49", "10")], asks=[("0.50", "2")])],
    )
    assert result.order_results[0].status == OrderStatus.REJECTED
    assert result.execution_reports[0].status == ExecutionStatus.UNFILLED
    assert result.execution_reports[0].filled_size == Decimal("0")
    assert result.positions == []
    assert result.portfolio_snapshots[-1].cash == Decimal("100")
    assert engine._books[("m1", "t1")].asks[Decimal("0.50")] == Decimal("2")


@pytest.mark.asyncio
async def test_synthetic_short_reserves_collateral() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5"},
        backtest={"starting_cash": "10", "taker_fee_bps": "0"},
        risk={"min_top_of_book_liquidity": "1"},
    )
    engine = BacktestEngine(config=config)
    events = [
        snapshot_event(sequence=1, bids=[("0.49", "100")], asks=[("0.50", "100")]),
        snapshot_event(sequence=2, bids=[("0.49", "100")], asks=[("0.50", "100")], at=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC)),
    ]
    result = await engine.run_events(strategy=SellOnceStrategy(), events=events)
    assert result.positions[0].quantity == Decimal("-5")
    assert result.portfolio_snapshots[-1].reserved_cash == Decimal("5")
    assert result.portfolio_snapshots[-1].available_cash == Decimal("7.45")


@pytest.mark.asyncio
async def test_unfundable_short_is_rejected_atomically() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5"},
        backtest={"starting_cash": "2.5", "taker_fee_bps": "0"},
        risk={"min_top_of_book_liquidity": "1"},
    )
    engine = BacktestEngine(config=config)
    events = [
        snapshot_event(sequence=1, bids=[("0.49", "100")], asks=[("0.50", "100")]),
        snapshot_event(sequence=2, bids=[("0.49", "100")], asks=[("0.50", "100")], at=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC)),
    ]
    result = await engine.run_events(strategy=SellOnceStrategy(), events=events)
    assert result.order_results[-1].status == OrderStatus.REJECTED
    assert result.order_results[-1].message == "insufficient_short_collateral"
    assert result.positions == []
    assert result.portfolio_snapshots[-1].cash == Decimal("2.5")
    assert engine._books[("m1", "t1")].bids[Decimal("0.49")] == Decimal("100")


def test_backtest_path_never_imports_live_clients() -> None:
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    probe = (
        "import sys;"
        "from backtest.replay import BacktestEngine;"
        "bad = sorted(m for m in sys.modules if m == 'clients' or m.startswith('clients.')"
        " or m.startswith('execution.submitter') or m.startswith('websockets') or m.startswith('httpx'));"
        "print('live-imports=' + repr(bad))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=root,
    )
    assert proc.returncode == 0, proc.stderr
    assert "live-imports=[]" in proc.stdout
