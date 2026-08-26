"""Pre-trade risk checks for every order intent."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.operations import OperationalState
from models.risk import RiskAction, RiskCheckResult, RiskDecision
from models.signal import TradeSignal
from risk.edge import EdgeDecision, assess_edge
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

    def __init__(
        self,
        *,
        config: AppConfig,
        state_store: InMemoryStateStore,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._state_store = state_store
        self._now = now

    async def evaluate(
        self,
        *,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_size: Decimal,
        proposed_price: Decimal,
        executable_liquidity: Decimal | None = None,
    ) -> RiskDecision:
        checks: list[RiskCheckResult] = []

        checks.append(self._mode_check())
        checks.append(await self._kill_switch_check())
        checks.append(await self._operational_state_check(signal))
        checks.append(self._stale_data_check(snapshot))
        checks.append(self._reduce_only_check(signal))
        checks.append(await self._pending_exit_check(signal))
        checks.append(await self._single_position_check(signal, proposed_size))
        checks.append(
            await self._total_exposure_check(
                signal,
                proposed_size,
                proposed_price,
            )
        )
        checks.append(await self._open_orders_check())
        checks.append(await self._duplicate_guard_check(signal))
        checks.append(
            self._top_of_book_liquidity_check(
                signal, snapshot, proposed_size, executable_liquidity
            )
        )
        checks.append(self._slippage_check(signal, snapshot, proposed_price))
        checks.append(self._edge_gate_check(signal, snapshot, proposed_price))
        checks.append(self._entry_spread_check(signal, snapshot))

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

    async def _operational_state_check(self, signal: TradeSignal) -> RiskCheckResult:
        """Block new BUY entries outside RUNNING; reduce-only passes this gate."""

        state, reason = await self._state_store.get_operational_state()
        if state == OperationalState.RUNNING:
            return RiskCheckResult(
                check_name="operational_state",
                passed=True,
                reason=f"state={state.value}",
            )
        if signal.reduce_only or signal.side.value == "sell":
            return RiskCheckResult(
                check_name="operational_state",
                passed=True,
                reason=f"reduce_only_allowed:{state.value}",
            )
        suffix = f":{reason}" if reason else ""
        return RiskCheckResult(
            check_name="operational_state",
            passed=False,
            reason=f"entries_paused:{state.value}{suffix}",
        )

    def _stale_data_check(self, snapshot: MarketSnapshot | None) -> RiskCheckResult:
        if snapshot is None:
            return RiskCheckResult(check_name="stale_data", passed=False, reason="market_snapshot_missing")
        max_age = timedelta(seconds=self._config.risk.max_data_staleness_seconds)
        age = self._now() - snapshot.received_ts
        return RiskCheckResult(
            check_name="stale_data",
            passed=age <= max_age,
            reason="market_data_fresh" if age <= max_age else f"market_data_stale:{age.total_seconds():.2f}s",
        )

    def _reduce_only_check(self, signal: TradeSignal) -> RiskCheckResult:
        if signal.reduce_only and signal.side.value != "sell":
            return RiskCheckResult(
                check_name="reduce_only",
                passed=False,
                reason="reduce_only_requires_sell",
            )
        return RiskCheckResult(
            check_name="reduce_only",
            passed=True,
            reason="reduce_only_valid",
        )

    async def _pending_exit_check(self, signal: TradeSignal) -> RiskCheckResult:
        if signal.side.value != "buy":
            return RiskCheckResult(
                check_name="pending_exit",
                passed=True,
                reason="entry_not_requested",
            )
        lifecycle = await self._state_store.get_position_lifecycle(
            signal.market_id,
            signal.token_id,
        )
        pending = (
            lifecycle.pending_exit_client_order_id
            if lifecycle is not None
            else None
        )
        return RiskCheckResult(
            check_name="pending_exit",
            passed=pending is None,
            reason=(
                "no_pending_exit"
                if pending is None
                else f"entry_blocked_by_pending_exit:{pending}"
            ),
        )

    async def _single_position_check(
        self,
        signal: TradeSignal,
        proposed_size: Decimal,
    ) -> RiskCheckResult:
        position = await self._state_store.get_position(signal.market_id, signal.token_id)
        current_quantity = position.quantity if position is not None else Decimal("0")
        if self._config.bot.mode in {Mode.BACKTEST, Mode.REPLAY}:
            signed_delta = (
                -proposed_size if signal.side.value == "sell" else proposed_size
            )
            projected = abs(current_quantity + signed_delta)
            limit = self._config.risk.max_single_position_size
            return RiskCheckResult(
                check_name="max_single_position_size",
                passed=projected <= limit,
                reason=(
                    "single_position_within_limit"
                    if projected <= limit
                    else f"single_position_limit:{projected}>{limit}"
                ),
            )
        current = max(current_quantity, Decimal("0"))
        if signal.side.value == "sell":
            if proposed_size > current:
                return RiskCheckResult(
                    check_name="max_single_position_size",
                    passed=False,
                    reason=f"insufficient_position_to_sell:{current}<{proposed_size}",
                )
            return RiskCheckResult(
                check_name="max_single_position_size",
                passed=True,
                reason="position_reducing_sell",
            )
        limit = self._config.risk.max_single_position_size
        projected = current + proposed_size
        return RiskCheckResult(
            check_name="max_single_position_size",
            passed=projected <= limit,
            reason="single_position_within_limit" if projected <= limit else f"single_position_limit:{projected}>{limit}",
        )

    async def _total_exposure_check(
        self,
        signal: TradeSignal,
        proposed_size: Decimal,
        proposed_price: Decimal,
    ) -> RiskCheckResult:
        current = await self._state_store.total_marked_exposure()
        proposed_notional = abs(proposed_size * proposed_price)
        if self._config.bot.mode in {Mode.BACKTEST, Mode.REPLAY}:
            position = await self._state_store.get_position(
                signal.market_id, signal.token_id
            )
            current_quantity = (
                position.quantity if position is not None else Decimal("0")
            )
            signed_delta = (
                -proposed_size if signal.side.value == "sell" else proposed_size
            )
            projected_quantity = current_quantity + signed_delta
            current_position_notional = abs(current_quantity * proposed_price)
            projected_position_notional = abs(
                projected_quantity * proposed_price
            )
            projected = max(
                Decimal("0"),
                current - current_position_notional + projected_position_notional,
            )
        else:
            projected = (
                max(Decimal("0"), current - proposed_notional)
                if signal.side.value == "sell"
                else current + proposed_notional
            )
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
        if signal.is_maker_quote:
            # A market maker is *supposed* to keep a resting order on each
            # side and refresh it as the book moves. Its own quote tracker
            # owns replacement; the open-order cap still bounds the total.
            return RiskCheckResult(
                check_name="duplicate_guard",
                passed=True,
                reason="maker_quote_exempt",
            )
        if signal.reduce_only:
            # Exit retries are deliberate repeats of the same intent, and the
            # retry interval is far shorter than the duplicate window -- so
            # this guard would block every retry after the first and exhaust
            # the exit budget, latching the kill switch on a position that
            # was never actually re-sent. Concurrency is already prevented by
            # the exit reservation, which admits one live exit per position.
            return RiskCheckResult(
                check_name="duplicate_guard",
                passed=True,
                reason="exit_retry_exempt",
            )
        window = timedelta(seconds=self._config.risk.duplicate_signal_window_seconds)
        cutoff = self._now() - window
        signals = await self._state_store.get_signals()
        for existing in signals:
            if (
                existing.signal_id != signal.signal_id
                and existing.created_at >= cutoff
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
        executable_liquidity: Decimal | None = None,
    ) -> RiskCheckResult:
        if snapshot is None:
            return RiskCheckResult(
                check_name="top_of_book_liquidity",
                passed=False,
                reason="market_snapshot_missing",
            )
        if signal.is_maker_quote:
            # This check exists to stop a taker from sweeping a thin book.
            # A resting quote adds depth instead of consuming it, so opposing
            # liquidity is not a precondition for posting one.
            return RiskCheckResult(
                check_name="top_of_book_liquidity",
                passed=True,
                reason="maker_quote_adds_liquidity",
            )
        available = executable_liquidity
        if available is None:
            available = snapshot.top_ask_size if signal.side.value == "buy" else snapshot.top_bid_size
        minimum = max(self._config.risk.min_top_of_book_liquidity, proposed_size)
        return RiskCheckResult(
            check_name="top_of_book_liquidity",
            passed=available >= minimum,
            reason="top_of_book_sufficient" if available >= minimum else f"top_of_book_too_thin:{available}<{minimum}",
        )

    def _entry_spread_check(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
    ) -> RiskCheckResult:
        """
        Refuse to cross a book whose spread exceeds the configured maximum.

        Top-of-book *size* and top-of-book *tightness* are different things.
        A book quoting 0.09 / 0.91 can show plenty of depth on both sides and
        still cost 82 cents a share to cross -- a loss taken in full at the
        moment of entry, before any market move. The liquidity check passes
        such a book; only this one catches it.

        Resting maker quotes are exempt: they add liquidity to a wide book
        rather than paying to cross it, which is precisely when quoting is
        most profitable.
        """

        limit = self._config.risk.max_entry_spread_bps
        if limit <= 0 or signal.is_maker_quote or signal.reduce_only:
            return RiskCheckResult(
                check_name="entry_spread",
                passed=True,
                reason="entry_spread_not_applicable",
            )
        if snapshot is None:
            return RiskCheckResult(
                check_name="entry_spread",
                passed=False,
                reason="market_snapshot_missing",
            )
        if snapshot.best_ask <= 0 or snapshot.best_bid <= 0:
            return RiskCheckResult(
                check_name="entry_spread",
                passed=False,
                reason="entry_spread_book_one_sided",
            )
        spread_bps = float(
            (snapshot.best_ask - snapshot.best_bid)
            / snapshot.best_ask
            * Decimal("10000")
        )
        passed = spread_bps <= limit
        return RiskCheckResult(
            check_name="entry_spread",
            passed=passed,
            reason=(
                "entry_spread_within_limit"
                if passed
                else f"entry_spread_too_wide:{spread_bps:.0f}>{limit:.0f}"
            ),
        )

    def _slippage_check(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_price: Decimal,
    ) -> RiskCheckResult:
        if snapshot is None:
            return RiskCheckResult(check_name="slippage", passed=False, reason="market_snapshot_missing")
        if signal.is_maker_quote or signal.reduce_only:
            # Slippage measures how far a taker pays through the touch. A
            # maker quote sits away from the touch on purpose, and the order
            # builder already clamped it so it cannot cross. A reduce-only
            # exit is exempt for the same reason a taker exit never tripped
            # this check before maker-first exits existed: it priced at the
            # near-touch reference and showed zero distance. A maker-first
            # exit instead rests at the *far* side of the spread on purpose
            # (see PositionExitManager._emit_exit), so it needs the same
            # exemption already granted to reduce-only signals elsewhere in
            # this file (duplicate_guard, entry_spread).
            return RiskCheckResult(
                check_name="slippage",
                passed=True,
                reason="maker_quote_prices_are_intentional"
                if signal.is_maker_quote
                else "reduce_only_prices_are_intentional",
            )
        reference = snapshot.best_ask if signal.side.value == "buy" else snapshot.best_bid
        slippage_bps = _bps_distance(reference, proposed_price)
        passed = slippage_bps <= self._config.risk.max_slippage_bps
        return RiskCheckResult(
            check_name="slippage",
            passed=passed,
            reason="slippage_within_limit" if passed else f"slippage_limit:{slippage_bps:.2f}>{self._config.risk.max_slippage_bps:.2f}",
        )

    def _edge_gate_check(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_price: Decimal,
    ) -> RiskCheckResult:
        """
        Refuse signals whose expected edge cannot clear fees plus spread.

        Shadow mode passes the check while still reporting what enforce mode
        would have done, so the numbers accumulate without the gate starving
        the fill data needed to calibrate it.
        """

        mode = self._config.risk.edge_gate_mode
        if mode == "off":
            return RiskCheckResult(
                check_name="edge_gate", passed=True, reason="edge_gate_disabled"
            )
        if signal.reduce_only:
            # This gate prices a round trip; an exit has already half-paid
            # it by entering. Refusing an exit because it "isn't profitable
            # enough" strands the position into resolution -- on a market
            # with a fixed settlement time that leaves the full notional on
            # a coin flip, which is strictly worse than any cost this gate
            # protects against. Matches _entry_spread_check's reduce_only
            # exemption for the same reason.
            return RiskCheckResult(
                check_name="edge_gate", passed=True, reason="exit_not_cost_gated"
            )
        if signal.observed_move_bps == 0:
            # Zero directional edge means this is a liquidity-provision
            # quote, not a directional bet (see strategies/market_maker.py,
            # which always sets observed_move_bps=0.0) -- the round-trip
            # cost model doesn't describe its economics. Maker entries that
            # DO carry a move size (e.g. a post-only spike entry) stay
            # gated below.
            return RiskCheckResult(
                check_name="edge_gate", passed=True, reason="no_directional_claim"
            )
        if snapshot is None:
            return RiskCheckResult(
                check_name="edge_gate", passed=False, reason="market_snapshot_missing"
            )
        if snapshot.best_ask <= 0:
            return RiskCheckResult(
                check_name="edge_gate", passed=False, reason="edge_gate_book_one_sided"
            )

        spread_bps = (
            (snapshot.best_ask - snapshot.best_bid) / snapshot.best_ask
        ) * Decimal("10000")
        assessment = assess_edge(
            edge_bps=Decimal(str(signal.observed_move_bps)),
            price=proposed_price,
            spread_bps=spread_bps,
            fee_rate=self._config.execution.fee_rate,
            is_maker_entry=signal.is_maker_quote,
            safety_margin_bps=self._config.risk.safety_margin_bps,
        )
        approved = assessment.decision is EdgeDecision.APPROVE
        if mode == "shadow":
            return RiskCheckResult(
                check_name="edge_gate",
                passed=True,
                reason=f"shadow:{assessment.reason}",
            )
        return RiskCheckResult(
            check_name="edge_gate",
            passed=approved,
            reason=assessment.reason,
        )
