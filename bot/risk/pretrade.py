"""Pre-trade risk checks for every order intent."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.risk import RiskAction, RiskCheckResult, RiskDecision
from models.signal import TradeSignal
from risk.policy import PreTradeRiskPolicy
from state.store import InMemoryStateStore


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


def _bps_distance(reference: Decimal, candidate: Decimal) -> float:
    if reference <= 0:
        return 0.0
    return float(abs((candidate - reference) / reference) * Decimal("10000"))


class PreTradeRiskEngine(PreTradeRiskPolicy):
    """Concrete risk engine that evaluates order intents before execution."""

    def __init__(self, *, config: AppConfig, state_store: InMemoryStateStore) -> None:
        self._config = config
        self._state_store = state_store

    async def evaluate(
        self,
        *,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_size: Decimal,
        proposed_price: Decimal,
    ) -> RiskDecision:
        checks: list[RiskCheckResult] = []

        checks.append(self._mode_check())
        checks.append(await self._kill_switch_check())
        checks.append(self._stale_data_check(snapshot))
        checks.append(await self._single_position_check(signal, proposed_size))
        checks.append(await self._total_exposure_check(proposed_size))
        checks.append(await self._open_orders_check())
        checks.append(await self._duplicate_guard_check(signal))
        checks.append(self._top_of_book_liquidity_check(signal, snapshot, proposed_size))
        checks.append(self._slippage_check(signal, snapshot, proposed_price))

        failed = [check for check in checks if not check.passed]
        if failed:
            primary = failed[0]
            halt = primary.check_name in {"kill_switch", "mode_supported"}
            return RiskDecision(
                action=RiskAction.HALT if halt else RiskAction.REJECT,
                approved=False,
                checks=checks,
                reason=primary.reason,
                signal_id=signal.signal_id,
            )

        return RiskDecision(
            action=RiskAction.APPROVE,
            approved=True,
            checks=checks,
            reason="approved",
            signal_id=signal.signal_id,
        )

    def _mode_check(self) -> RiskCheckResult:
        mode = self._config.bot.mode
        if mode in {Mode.DRY_RUN, Mode.LIVE, Mode.BACKTEST, Mode.REPLAY}:
            return RiskCheckResult(check_name="mode_supported", passed=True, reason=f"mode={mode.value}")
        return RiskCheckResult(check_name="mode_supported", passed=False, reason=f"unsupported_mode:{mode}")

    async def _kill_switch_check(self) -> RiskCheckResult:
        active = await self._state_store.is_kill_switch_active()
        return RiskCheckResult(
            check_name="kill_switch",
            passed=not active,
            reason="kill_switch_inactive" if not active else "kill_switch_active",
        )

    def _stale_data_check(self, snapshot: MarketSnapshot | None) -> RiskCheckResult:
        if snapshot is None:
            return RiskCheckResult(check_name="stale_data", passed=False, reason="market_snapshot_missing")
        max_age = timedelta(seconds=self._config.risk.max_data_staleness_seconds)
        age = utc_now() - snapshot.received_ts
        return RiskCheckResult(
            check_name="stale_data",
            passed=age <= max_age,
            reason="market_data_fresh" if age <= max_age else f"market_data_stale:{age.total_seconds():.2f}s",
        )

    async def _single_position_check(
        self,
        signal: TradeSignal,
        proposed_size: Decimal,
    ) -> RiskCheckResult:
        position = await self._state_store.get_position(signal.market_id, signal.token_id)
        current = abs(position.quantity) if position is not None else Decimal("0")
        limit = self._config.risk.max_single_position_size
        projected = current + proposed_size
        return RiskCheckResult(
            check_name="max_single_position_size",
            passed=projected <= limit,
            reason="single_position_within_limit" if projected <= limit else f"single_position_limit:{projected}>{limit}",
        )

    async def _total_exposure_check(self, proposed_size: Decimal) -> RiskCheckResult:
        current = await self._state_store.total_absolute_exposure()
        projected = current + proposed_size
        limit = self._config.risk.max_total_exposure
        return RiskCheckResult(
            check_name="max_total_exposure",
            passed=projected <= limit,
            reason="total_exposure_within_limit" if projected <= limit else f"total_exposure_limit:{projected}>{limit}",
        )

    async def _open_orders_check(self) -> RiskCheckResult:
        open_orders = await self._state_store.get_open_orders()
        current = len(open_orders)
        limit = self._config.risk.max_open_orders
        return RiskCheckResult(
            check_name="max_open_orders",
            passed=current < limit,
            reason="open_orders_within_limit" if current < limit else f"open_order_limit:{current}>={limit}",
        )

    async def _duplicate_guard_check(self, signal: TradeSignal) -> RiskCheckResult:
        window = timedelta(seconds=self._config.risk.duplicate_signal_window_seconds)
        cutoff = utc_now() - window
        signals = await self._state_store.get_signals()
        for existing in signals:
            if (
                existing.signal_id != signal.signal_id
                and
                existing.created_at >= cutoff
                and existing.market_id == signal.market_id
                and existing.token_id == signal.token_id
                and existing.side == signal.side
            ):
                return RiskCheckResult(
                    check_name="duplicate_guard",
                    passed=False,
                    reason=f"duplicate_signal:{existing.signal_id}",
                )

        open_orders = await self._state_store.get_open_orders()
        for order in open_orders:
            if (
                order.market_id == signal.market_id
                and order.token_id == signal.token_id
                and order.side is not None
                and order.side.value == signal.side.value
            ):
                return RiskCheckResult(
                    check_name="duplicate_guard",
                    passed=False,
                    reason=f"duplicate_open_order:{order.client_order_id}",
                )

        return RiskCheckResult(check_name="duplicate_guard", passed=True, reason="no_duplicates_detected")

    def _top_of_book_liquidity_check(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_size: Decimal,
    ) -> RiskCheckResult:
        if snapshot is None:
            return RiskCheckResult(
                check_name="top_of_book_liquidity",
                passed=False,
                reason="market_snapshot_missing",
            )
        available = snapshot.top_ask_size if signal.side.value == "buy" else snapshot.top_bid_size
        minimum = max(self._config.risk.min_top_of_book_liquidity, proposed_size)
        return RiskCheckResult(
            check_name="top_of_book_liquidity",
            passed=available >= minimum,
            reason="top_of_book_sufficient" if available >= minimum else f"top_of_book_too_thin:{available}<{minimum}",
        )

    def _slippage_check(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_price: Decimal,
    ) -> RiskCheckResult:
        if snapshot is None:
            return RiskCheckResult(check_name="slippage", passed=False, reason="market_snapshot_missing")
        reference = snapshot.best_ask if signal.side.value == "buy" else snapshot.best_bid
        slippage_bps = _bps_distance(reference, proposed_price)
        passed = slippage_bps <= self._config.risk.max_slippage_bps
        return RiskCheckResult(
            check_name="slippage",
            passed=passed,
            reason="slippage_within_limit" if passed else f"slippage_limit:{slippage_bps:.2f}>{self._config.risk.max_slippage_bps:.2f}",
        )
