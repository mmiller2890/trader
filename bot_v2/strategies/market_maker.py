"""
Inventory-skewed two-sided market making.

The strategy keeps one resting bid and one resting ask per token, priced
around a fair value that is pulled away from the side it is already long.
Every quote is post-only so a refresh can never accidentally cross and pay
the taker fee it is trying to earn.

Cancel-before-replace is the invariant that keeps this safe: a refresh always
pulls the stale order first, so the strategy can never hold two live quotes on
the same side of the same token.

Planning is pure. ``plan_*`` never mutates the tracked quote book -- it only
describes what should happen. The router applies the plan and reports back
through ``forget_quote`` and ``register_submission``, so a cancellation that
never confirmed leaves the original quote tracked and still live.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from config.schema import MarketMakerConfig
from models.market import MarketSnapshot
from models.order import CancelIntent, OrderResult, OrderSide, OrderStatus
from models.position import Position
from models.signal import SignalSide, SignalType, TradeSignal
from models.tick import DEFAULT_TICK_SIZE, quantize_size
from strategies.base import StrategyBase
from strategies.quoting import QuotePlan, RestingQuote

logger = logging.getLogger(__name__)

QuoteKey = tuple[str, str, OrderSide]
PositionReader = Callable[[str, str], Awaitable[Position | None]]
TickSizeProvider = Callable[[str], Decimal]


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""

    return datetime.now(tz=UTC)


class MarketMakerStrategy(StrategyBase):
    """Two-sided quoting with inventory skew and forced unwind."""

    def __init__(
        self,
        config: MarketMakerConfig,
        *,
        position_reader: PositionReader,
        tick_size_provider: TickSizeProvider | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._position_reader = position_reader
        self._tick_size_provider = tick_size_provider
        self._now = now
        self._resting: dict[QuoteKey, RestingQuote] = {}
        self._pending: dict[str, QuoteKey] = {}

    @property
    def name(self) -> str:
        return "market_maker"

    def set_clock(self, now: Callable[[], datetime]) -> None:
        """Set the clock used for quote ages and emitted signal timestamps."""

        self._now = now

    def resting_quotes(self) -> list[RestingQuote]:
        """Return every quote this process believes is live, for inspection."""

        return list(self._resting.values())

    # ------------------------------------------------------------------
    # StrategyBase
    # ------------------------------------------------------------------

    async def on_market_update(self, snapshot: MarketSnapshot) -> list[TradeSignal]:
        """Return only the new quotes; callers needing cancels use plan_quotes."""

        plan = await self.plan_quotes(snapshot)
        return plan.quotes

    async def on_order_update(self, order_result: OrderResult) -> list[TradeSignal]:
        """Track acceptance, fills, and rejections of our own quotes."""

        self.record_order_result(order_result)
        return []

    async def on_timer(self) -> list[TradeSignal]:
        plan = await self.plan_maintenance()
        return plan.quotes

    # ------------------------------------------------------------------
    # Quoting
    # ------------------------------------------------------------------

    async def plan_quotes(
        self,
        snapshot: MarketSnapshot,
        *,
        market_end_at: datetime | None = None,
    ) -> QuotePlan:
        """Return the cancel/replace plan implied by a new book state."""

        if not self._config.enabled:
            return QuotePlan()
        if not self._targets(snapshot):
            return QuotePlan()

        if self._market_is_closing(market_end_at):
            return await self.plan_withdrawal("market_closing")
        if not self._book_is_quotable(snapshot):
            return await self.plan_withdrawal("book_not_quotable")

        tick = self._tick_size(snapshot.token_id)
        position = await self._position_reader(snapshot.market_id, snapshot.token_id)
        inventory = position.quantity if position is not None else Decimal("0")
        ratio = self._inventory_ratio(inventory)
        fair_value = self._fair_value(snapshot.mid_price, ratio=ratio, tick=tick)

        plan = QuotePlan()
        for side in (OrderSide.BUY, OrderSide.SELL):
            plan.extend(
                self._plan_side(
                    snapshot=snapshot,
                    side=side,
                    fair_value=fair_value,
                    ratio=ratio,
                    inventory=inventory,
                    tick=tick,
                )
            )
        return plan

    async def plan_maintenance(self) -> QuotePlan:
        """Pull quotes that have outlived their TTL so they get re-priced."""

        if not self._config.enabled:
            return QuotePlan()
        now = self._now()
        plan = QuotePlan()
        for quote in list(self._resting.values()):
            age = (now - quote.placed_at).total_seconds()
            if age >= self._config.quote_ttl_seconds:
                plan.cancels.append(self._cancel_for(quote, reason="quote_ttl_expired"))
        return plan

    async def plan_withdrawal(self, reason: str) -> QuotePlan:
        """Pull every resting quote, for shutdown, halt, or market close."""

        plan = QuotePlan()
        for quote in list(self._resting.values()):
            plan.cancels.append(self._cancel_for(quote, reason=reason))
        return plan

    def _plan_side(
        self,
        *,
        snapshot: MarketSnapshot,
        side: OrderSide,
        fair_value: Decimal,
        ratio: Decimal,
        inventory: Decimal,
        tick: Decimal,
    ) -> QuotePlan:
        key = (snapshot.market_id, snapshot.token_id, side)
        existing = self._resting.get(key)
        plan = QuotePlan()

        size = self._quote_size(side=side, ratio=ratio, inventory=inventory)
        if size <= 0:
            if existing is not None:
                plan.cancels.append(
                    self._cancel_for(existing, reason="side_suppressed_by_inventory")
                )
            return plan

        half_spread = self._half_spread(side=side, ratio=ratio, tick=tick)
        price = (
            fair_value - half_spread
            if side == OrderSide.BUY
            else fair_value + half_spread
        )
        if price <= 0 or price >= 1:
            if existing is not None:
                plan.cancels.append(
                    self._cancel_for(existing, reason="quote_price_out_of_bounds")
                )
            return plan

        if existing is not None:
            if not self._should_refresh(existing, price=price, size=size, tick=tick):
                return plan
            plan.cancels.append(self._cancel_for(existing, reason="quote_stale"))

        signal = TradeSignal(
            strategy_name=self.name,
            signal_type=SignalType.MAKER_QUOTE,
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            side=SignalSide.BUY if side == OrderSide.BUY else SignalSide.SELL,
            reference_price=snapshot.mid_price,
            target_price=price,
            observed_move_bps=0.0,
            created_at=self._now(),
            reason=f"maker_quote_{side.value}",
            requested_size=size,
            limit_price=price,
            post_only=True,
        )
        plan.quotes.append(signal)
        return plan

    # ------------------------------------------------------------------
    # Pricing and sizing
    # ------------------------------------------------------------------

    def _inventory_ratio(self, inventory: Decimal) -> Decimal:
        """Signed inventory as a fraction of the strategy position cap."""

        cap = self._config.max_position_size
        if cap <= 0:
            return Decimal("0")
        ratio = inventory / cap
        return max(Decimal("-1"), min(Decimal("1"), ratio))

    def _fair_value(
        self, mid_price: Decimal, *, ratio: Decimal, tick: Decimal
    ) -> Decimal:
        """
        Skew the mid away from the side we are already long.

        Long inventory pulls fair value down, which lowers both quotes and
        makes the ask more likely to trade -- the position works itself off
        instead of growing.
        """

        skew = ratio * self._config.max_skew_ticks * tick
        return mid_price - skew

    def _half_spread(
        self, *, side: OrderSide, ratio: Decimal, tick: Decimal
    ) -> Decimal:
        """Half the quoted spread, tightened on the reducing side in unwind."""

        spread_ticks = Decimal(self._config.quote_spread_ticks)
        if self._is_unwinding(ratio) and self._is_reducing_side(side, ratio):
            spread_ticks = Decimal(self._config.unwind_spread_ticks)
        return (spread_ticks * tick) / 2

    def _quote_size(
        self, *, side: OrderSide, ratio: Decimal, inventory: Decimal
    ) -> Decimal:
        """
        Size one side, shrinking the side that would add to inventory.

        Returns zero when the side must not be quoted at all: past the unwind
        threshold on the accumulating side, at the position cap, or with no
        inventory to sell.
        """

        if self._is_unwinding(ratio) and not self._is_reducing_side(side, ratio):
            return Decimal("0")

        base = self._config.base_quote_size
        # Long inventory (ratio > 0) shrinks the bid and grows the ask.
        scale = (Decimal("1") - ratio) if side == OrderSide.BUY else (
            Decimal("1") + ratio
        )
        size = base * max(Decimal("0"), scale)

        headroom = (
            self._config.max_position_size - inventory
            if side == OrderSide.BUY
            else max(Decimal("0"), inventory)
        )
        size = min(size, headroom)
        size = quantize_size(size)
        if size < self._config.min_quote_size:
            return Decimal("0")
        return size

    def _is_unwinding(self, ratio: Decimal) -> bool:
        return abs(ratio) >= Decimal(str(self._config.inventory_unwind_ratio))

    @staticmethod
    def _is_reducing_side(side: OrderSide, ratio: Decimal) -> bool:
        """True when this side moves inventory back toward flat."""

        if ratio > 0:
            return side == OrderSide.SELL
        if ratio < 0:
            return side == OrderSide.BUY
        return True

    def _should_refresh(
        self,
        existing: RestingQuote,
        *,
        price: Decimal,
        size: Decimal,
        tick: Decimal,
    ) -> bool:
        """
        Decide whether a resting quote is stale enough to replace.

        Cancel/replace is not free -- it costs two round trips and forfeits
        queue position -- so a quote only moves when the new price is more
        than ``refresh_move_ticks`` away or the size changed materially.
        """

        moved_ticks = (price - existing.price).copy_abs() / tick
        if moved_ticks > self._config.refresh_move_ticks:
            return True
        if existing.size <= 0:
            return True
        size_change = (size - existing.size).copy_abs() / existing.size
        return size_change >= Decimal("0.25")

    # ------------------------------------------------------------------
    # Quote lifecycle bookkeeping
    # ------------------------------------------------------------------

    def register_submission(
        self,
        *,
        client_order_id: str,
        signal: TradeSignal,
        price: Decimal,
        size: Decimal,
    ) -> None:
        """Record a quote we are about to send so refreshes can find it."""

        side = OrderSide(signal.side.value)
        key = (signal.market_id, signal.token_id, side)
        self._pending[client_order_id] = key
        self._resting[key] = RestingQuote(
            client_order_id=client_order_id,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=side,
            price=price,
            size=size,
            placed_at=self._now(),
        )

    def record_order_result(self, result: OrderResult) -> None:
        """Attach exchange ids, and drop quotes that are no longer on the book."""

        key = self._pending.get(result.client_order_id)
        if key is None and result.market_id and result.token_id and result.side:
            key = (result.market_id, result.token_id, result.side)
        if key is None:
            return
        quote = self._resting.get(key)
        if quote is None or quote.client_order_id != result.client_order_id:
            return

        if result.status in {
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.FILLED,
            OrderStatus.SIMULATED,
        }:
            self._resting.pop(key, None)
            self._pending.pop(result.client_order_id, None)
            return
        if result.status == OrderStatus.UNKNOWN:
            # Fail closed: an unknown submission may or may not be resting, so
            # forget it here and let reconciliation be the authority.
            self._resting.pop(key, None)
            self._pending.pop(result.client_order_id, None)
            return
        if result.exchange_order_id:
            self._resting[key] = quote.with_exchange_id(result.exchange_order_id)

    def forget_quote(self, client_order_id: str) -> None:
        """Drop a quote after a confirmed cancellation."""

        key = self._pending.pop(client_order_id, None)
        if key is None:
            for candidate, quote in list(self._resting.items()):
                if quote.client_order_id == client_order_id:
                    key = candidate
                    break
        if key is None:
            return
        quote = self._resting.get(key)
        if quote is not None and quote.client_order_id == client_order_id:
            self._resting.pop(key, None)

    def restore_quote(self, quote: RestingQuote) -> None:
        """Put a quote back after a cancellation failed and it is still live."""

        self._resting[(quote.market_id, quote.token_id, quote.side)] = quote
        self._pending[quote.client_order_id] = (
            quote.market_id,
            quote.token_id,
            quote.side,
        )

    def quote_for(self, client_order_id: str) -> RestingQuote | None:
        """Return the tracked quote for one client order id, if any."""

        for quote in self._resting.values():
            if quote.client_order_id == client_order_id:
                return quote
        return None

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------

    def _targets(self, snapshot: MarketSnapshot) -> bool:
        if (
            self._config.target_market_ids
            and snapshot.market_id not in self._config.target_market_ids
        ):
            return False
        if (
            self._config.target_token_ids
            and snapshot.token_id not in self._config.target_token_ids
        ):
            return False
        return True

    def _book_is_quotable(self, snapshot: MarketSnapshot) -> bool:
        if snapshot.best_bid <= 0 or snapshot.best_ask <= 0:
            return False
        if snapshot.best_ask <= snapshot.best_bid:
            return False
        minimum = self._config.min_book_liquidity
        if minimum <= 0:
            return True
        return (
            snapshot.top_bid_size >= minimum and snapshot.top_ask_size >= minimum
        )

    def _market_is_closing(self, market_end_at: datetime | None) -> bool:
        if market_end_at is None:
            return False
        remaining = (market_end_at - self._now()).total_seconds()
        return remaining <= self._config.stop_quoting_before_end_seconds

    def _tick_size(self, token_id: str) -> Decimal:
        if self._tick_size_provider is None:
            return DEFAULT_TICK_SIZE
        try:
            return self._tick_size_provider(token_id)
        except Exception:
            return DEFAULT_TICK_SIZE

    def _cancel_for(self, quote: RestingQuote, *, reason: str) -> CancelIntent:
        return CancelIntent(
            client_order_id=quote.client_order_id,
            exchange_order_id=quote.exchange_order_id,
            market_id=quote.market_id,
            token_id=quote.token_id,
            side=quote.side,
            reason=reason,
        )
