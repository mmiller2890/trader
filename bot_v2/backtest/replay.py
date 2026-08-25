"""Deterministic historical replay and simulated paper-exchange backtesting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from backtest.metrics import ReplayMetrics, summarize
from backtest.models import (
    BookSnapshotEvent,
    ExecutionReport,
    ExecutionStatus,
    HistoricalBookEvent,
    PortfolioSnapshot,
)
from backtest.orderbook import OrderBookState
from backtest.portfolio import PortfolioLedger
from config.schema import AppConfig, Mode
from execution.order_builder import OrderBuilder
from models.market import MarketSnapshot, OrderBookLevel
from models.order import OrderResult, OrderStatus
from models.position import Position
from models.signal import TradeSignal
from risk.pretrade import PreTradeRiskEngine
from state.store import InMemoryStateStore
from strategies.base import StrategyBase


@dataclass(slots=True)
class ReplayResult:
    """Collected replay outputs."""

    signals: list[TradeSignal] = field(default_factory=list)
    order_results: list[OrderResult] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    equity_curve: list["EquityPoint"] = field(default_factory=list)
    execution_reports: list[ExecutionReport] = field(default_factory=list)
    portfolio_snapshots: list[PortfolioSnapshot] = field(default_factory=list)
    metrics: ReplayMetrics | None = None


class _BacktestClock:
    """Mutable deterministic clock shared with historical risk checks."""

    def __init__(self) -> None:
        self.current = datetime.now(tz=UTC)

    def now(self) -> datetime:
        return self.current


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """Portfolio P&L after processing one historical snapshot."""

    timestamp: datetime
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal


class BacktestEngine:
    """Replay historical events through strategy, risk, matching, and a portfolio ledger."""

    def __init__(self, *, config: AppConfig) -> None:
        if config.bot.mode not in {Mode.BACKTEST, Mode.REPLAY}:
            raise ValueError("BacktestEngine requires bot.mode to be backtest or replay")
        self._config = config
        self._reset_run_state()

    def _reset_run_state(self) -> None:
        self._clock = _BacktestClock()
        self._state_store = InMemoryStateStore(
            mode=self._config.bot.mode,
            kill_switch_active=self._config.bot.kill_switch_on_startup,
        )
        self._risk = PreTradeRiskEngine(
            config=self._config,
            state_store=self._state_store,
            now=self._clock.now,
        )
        self._order_builder = OrderBuilder(self._config)
        self._ledger = PortfolioLedger(self._config.backtest)
        self._books: dict[tuple[str, str], OrderBookState] = {}

    async def run(self, *, strategy: StrategyBase, snapshots: list[MarketSnapshot]) -> ReplayResult:
        """Run legacy top-of-book snapshots through the paper exchange."""
        ordered_snapshots = sorted(
            list(snapshots),
            key=lambda item: (item.received_ts, item.source_ts),
        )
        events = [
            BookSnapshotEvent(
                market_id=item.market_id,
                token_id=item.token_id,
                bids=[OrderBookLevel(price=item.best_bid, size=item.top_bid_size)],
                asks=[OrderBookLevel(price=item.best_ask, size=item.top_ask_size)],
                sequence_id=index,
                source_ts=item.source_ts,
                received_ts=item.received_ts,
            )
            for index, item in enumerate(ordered_snapshots)
        ]
        return await self.run_events(strategy=strategy, events=events)

    async def run_events(
        self,
        *,
        strategy: StrategyBase,
        events: list[HistoricalBookEvent],
    ) -> ReplayResult:
        """Process normalized historical events deterministically."""

        self._reset_run_state()
        result = ReplayResult()
        set_clock = getattr(strategy, "set_clock", None)
        if callable(set_clock):
            set_clock(self._clock.now)
        ordered_events = sorted(
            list(events), key=lambda item: (item.received_ts, item.source_ts, item.sequence_id)
        )
        for event in ordered_events:
            self._clock.current = event.received_ts
            book = self._books.setdefault(
                (event.market_id, event.token_id),
                OrderBookState(
                    event.market_id,
                    event.token_id,
                    reject_sequence_gaps=self._config.backtest.reject_sequence_gaps,
                ),
            )
            if isinstance(event, BookSnapshotEvent):
                book.apply_snapshot(event)
            else:
                book.apply_delta(event)

            snapshot = book.to_market_snapshot()
            if snapshot is not None:
                await self._state_store.update_market_snapshot(snapshot)
                await self._state_store.update_heartbeat("market_data", snapshot.received_ts)
                self._ledger.mark(snapshot)
                await self._mirror_positions()

                for signal in await strategy.on_market_update(snapshot):
                    result.signals.append(signal)
                    await self._state_store.add_signal(signal)
                    await self._process_signal(signal, snapshot, book, result)

                self._ledger.mark(snapshot)
                await self._mirror_positions()

            result.portfolio_snapshots.append(
                self._ledger.snapshot(event.received_ts)
            )
            result.equity_curve.append(await self._equity_point(event.received_ts))

        result.positions = await self._state_store.get_positions()
        result.metrics = summarize(
            result.signals,
            result.order_results,
            result.positions,
            result.execution_reports,
            result.portfolio_snapshots,
            self._ledger,
        )
        return result

    async def _process_signal(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        book: OrderBookState,
        result: ReplayResult,
    ) -> None:
        proposed_size = self._config.execution.default_order_size
        order = self._order_builder.build(signal=signal, snapshot=snapshot, size=proposed_size)
        order = order.model_copy(update={"created_at": self._clock.now()})
        candidate = book.quote(
            order,
            max_slippage_bps=Decimal(str(self._config.execution.max_slippage_bps)),
            fee_rate=self._config.backtest.fee_rate,
        )

        if candidate.filled_size <= 0:
            result.execution_reports.append(candidate)
            result.order_results.append(self._to_order_result(candidate))
            return

        allowed, reason = self._ledger.can_apply(candidate)
        if not allowed:
            rejected = self._reject_report(candidate, reason)
            result.execution_reports.append(rejected)
            result.order_results.append(self._to_order_result(rejected))
            return

        assert candidate.average_fill_price is not None
        decision = await self._risk.evaluate(
            signal=signal,
            snapshot=snapshot,
            proposed_size=candidate.filled_size,
            proposed_price=candidate.average_fill_price,
            executable_liquidity=candidate.executable_liquidity,
        )
        if not decision.approved:
            rejected = self._reject_report(candidate, decision.reason)
            result.execution_reports.append(rejected)
            result.order_results.append(self._to_order_result(rejected))
            return

        book.commit(candidate)
        self._ledger.apply(candidate, self._clock.now())
        await self._mirror_positions()
        result.execution_reports.append(candidate)
        result.order_results.append(self._to_order_result(candidate))

    async def _mirror_positions(self) -> None:
        for position in self._ledger.positions.values():
            await self._state_store.set_position(position)

    def _reject_report(self, candidate: ExecutionReport, reason: str) -> ExecutionReport:
        return candidate.model_copy(
            update={
                "status": ExecutionStatus.REJECTED,
                "fills": [],
                "filled_size": Decimal("0"),
                "remaining_size": candidate.requested_size,
                "average_fill_price": None,
                "total_notional": Decimal("0"),
                "total_fees": Decimal("0"),
                "reason": reason,
            }
        )

    def _to_order_result(self, report: ExecutionReport) -> OrderResult:
        if report.status in {ExecutionStatus.REJECTED, ExecutionStatus.UNFILLED}:
            status = OrderStatus.REJECTED
            accepted = False
        elif report.status == ExecutionStatus.FILLED:
            status = OrderStatus.FILLED
            accepted = True
        elif report.status == ExecutionStatus.PARTIAL:
            status = OrderStatus.PARTIALLY_FILLED
            accepted = True
        else:
            status = OrderStatus.REJECTED
            accepted = False
        return OrderResult(
            client_order_id=report.order.client_order_id,
            market_id=report.order.market_id,
            token_id=report.order.token_id,
            side=report.order.side,
            status=status,
            accepted=accepted,
            message=report.reason,
            signal_id=report.order.signal_id,
            strategy_name=report.order.strategy_name,
            requested_size=report.requested_size,
            filled_size=report.filled_size,
            avg_fill_price=report.average_fill_price,
            created_at=report.order.created_at,
        )

    async def _equity_point(self, timestamp: datetime) -> EquityPoint:
        state = self._ledger.snapshot(timestamp)
        return EquityPoint(
            timestamp=timestamp,
            realized_pnl=state.realized_pnl,
            unrealized_pnl=state.unrealized_pnl,
            total_pnl=state.gross_pnl,
        )


class ReplayEngine(BacktestEngine):
    """Backward-compatible name for the deterministic backtest engine."""

    def __init__(self, *, config: AppConfig | None = None) -> None:
        super().__init__(config=config or AppConfig(bot={"mode": Mode.REPLAY}))
