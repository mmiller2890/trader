"""Convert approved signals into concrete order requests."""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.order import OrderRequest, OrderSide, OrderTimeInForce
from models.signal import TradeSignal
from models.tick import SIZE_INCREMENT, quantize_price, quantize_size
from portfolio.sizing import clamp, fixed_size

TickSizeProvider = Callable[[str], Decimal]
MinSizeProvider = Callable[[str], Decimal]


class OrderBuilder:
    """Deterministic builder from signal to order request."""

    def __init__(
        self,
        config: AppConfig,
        *,
        tick_size_provider: TickSizeProvider | None = None,
        min_size_provider: MinSizeProvider | None = None,
    ) -> None:
        self._config = config
        self._tick_size_provider = tick_size_provider
        self._min_size_provider = min_size_provider

    def venue_minimum_for(self, market_id: str) -> Decimal | None:
        """
        The venue's own floor for this market, or None when it is unknown.

        Kept separate from the configured minimum so a refusal can say which
        of the two actually binds: a venue floor the notional cap cannot buy
        needs different numbers changed than a local limit does.
        """

        if self._min_size_provider is None:
            return None
        try:
            value = self._min_size_provider(market_id)
        except Exception:
            return None
        return value if value > 0 else None

    def minimum_size_for(self, market_id: str) -> Decimal:
        """
        Resolve the binding minimum order size for one market.

        The venue publishes its own floor per market and rejects anything
        under it. Taking the larger of that and the configured minimum means a
        looser local setting can never build an order the venue will refuse.
        """

        configured = self._config.execution.min_order_size
        venue = self.venue_minimum_for(market_id)
        return configured if venue is None else max(configured, venue)

    def tick_size_for(self, token_id: str) -> Decimal:
        """Resolve the exchange tick size for one token."""

        if self._tick_size_provider is None:
            return self._config.execution.default_tick_size
        try:
            return self._tick_size_provider(token_id)
        except Exception:
            return self._config.execution.default_tick_size

    def build(
        self,
        *,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        size: Decimal | None = None,
    ) -> OrderRequest:
        """Create validated order request from signal and current market snapshot."""

        side = OrderSide(signal.side.value)
        tick_size = self.tick_size_for(signal.token_id)
        live_trading_enabled = (
            self._config.bot.mode == Mode.LIVE
            and self._config.execution.allow_live_trading
            and not self._config.execution.dry_run_force
        )

        if signal.post_only and signal.limit_price is not None:
            return self._build_maker_quote(
                signal=signal,
                snapshot=snapshot,
                side=side,
                tick_size=tick_size,
                live_trading_enabled=live_trading_enabled,
            )

        chosen_size = (
            signal.requested_size
            if signal.requested_size is not None
            else (size if size is not None else fixed_size(self._config.execution))
        )
        normalized_size = clamp(
            chosen_size,
            self._config.execution.min_order_size,
            self._config.execution.max_order_size,
        )
        raw_price = snapshot.best_ask if side == OrderSide.BUY else snapshot.best_bid
        # Taker legs must stay marketable, so they round toward the touch.
        price = quantize_price(
            raw_price, tick_size=tick_size, side=side, aggressive=True
        )
        if live_trading_enabled:
            visible_size = (
                snapshot.top_ask_size
                if side == OrderSide.BUY
                else snapshot.top_bid_size
            )
            cap_size = (
                self._config.execution.max_live_order_notional / price
            ).quantize(SIZE_INCREMENT, rounding=ROUND_DOWN)
            venue_floor = self.venue_minimum_for(signal.market_id)
            required_size = self.minimum_size_for(signal.market_id)
            if side == OrderSide.BUY:
                required_size = max(
                    required_size,
                    (
                        self._config.execution.min_live_buy_notional / price
                    ).quantize(SIZE_INCREMENT, rounding=ROUND_UP),
                )
            maximum_size = quantize_size(
                min(
                    self._config.execution.max_order_size,
                    visible_size,
                    cap_size,
                )
            )
            if required_size > maximum_size:
                if venue_floor is not None and venue_floor > cap_size:
                    # The notional cap alone cannot buy the venue's floor, so
                    # every order in this market is rejected on arrival. Name
                    # the two numbers that have to change.
                    raise ValueError(
                        f"venue minimum order size {venue_floor} costs "
                        f"{(venue_floor * price).quantize(Decimal('0.01'))} at "
                        f"{price}, above max_live_order_notional "
                        f"{self._config.execution.max_live_order_notional}"
                    )
                if side == OrderSide.BUY:
                    raise ValueError(
                        "live execution cannot satisfy minimum live BUY notional "
                        "within order-size, notional-cap, and visible-liquidity limits"
                    )
                raise ValueError(
                    "live execution cannot satisfy minimum order size within "
                    "notional cap and visible liquidity"
                )
            normalized_size = quantize_size(
                min(max(normalized_size, required_size), maximum_size)
            )

        return OrderRequest(
            client_order_id=self.client_order_id_for(signal),
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=side,
            price=price,
            size=normalized_size,
            tick_size=tick_size,
            post_only=False,
            time_in_force=(
                signal.time_in_force
                if signal.time_in_force is not None
                else OrderTimeInForce(self._config.execution.time_in_force.value)
            ),
            signal_id=signal.signal_id,
            strategy_name=signal.strategy_name,
        )

    def _build_maker_quote(
        self,
        *,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        side: OrderSide,
        tick_size: Decimal,
        live_trading_enabled: bool,
    ) -> OrderRequest:
        """
        Build a resting post-only quote at the strategy's own limit price.

        A maker quote must not cross, so the price is clamped one tick inside
        the opposing best before quantization. Without that clamp a post-only
        order that crosses is rejected outright and the quote never rests.
        """

        assert signal.limit_price is not None  # enforced by TradeSignal validation
        assert signal.requested_size is not None
        price = signal.limit_price
        if side == OrderSide.BUY and snapshot.best_ask > 0:
            price = min(price, snapshot.best_ask - tick_size)
        elif side == OrderSide.SELL and snapshot.best_bid > 0:
            price = max(price, snapshot.best_bid + tick_size)
        if price <= 0:
            raise ValueError("maker quote price collapsed below the minimum tick")
        price = quantize_price(price, tick_size=tick_size, side=side)

        venue_floor = self.venue_minimum_for(signal.market_id)
        venue_minimum = self.minimum_size_for(signal.market_id)
        normalized_size = quantize_size(
            clamp(
                signal.requested_size,
                venue_minimum,
                self._config.execution.max_order_size,
            )
        )
        if live_trading_enabled:
            cap_size = quantize_size(
                self._config.execution.max_live_order_notional / price
            )
            if venue_floor is not None and cap_size < venue_floor:
                raise ValueError(
                    f"venue minimum order size {venue_floor} costs "
                    f"{(venue_floor * price).quantize(Decimal('0.01'))} at "
                    f"{price}, above max_live_order_notional "
                    f"{self._config.execution.max_live_order_notional}"
                )
            if cap_size < venue_minimum:
                raise ValueError(
                    "maker quote cannot satisfy minimum order size within the "
                    "live notional cap"
                )
            normalized_size = min(normalized_size, cap_size)
        if normalized_size <= 0:
            raise ValueError("maker quote size rounded down to zero")

        return OrderRequest(
            client_order_id=self.client_order_id_for(signal),
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=side,
            price=price,
            size=normalized_size,
            tick_size=tick_size,
            post_only=(
                signal.post_only and self._config.execution.post_only_maker_quotes
            ),
            time_in_force=(
                signal.time_in_force
                if signal.time_in_force is not None
                else OrderTimeInForce.GTC
            ),
            signal_id=signal.signal_id,
            strategy_name=signal.strategy_name,
        )

    def client_order_id_for(self, signal: TradeSignal) -> str:
        """Return the deterministic client order id used for one signal."""

        return (
            f"{self._config.execution.client_order_id_prefix}-{signal.signal_id[:18]}"
        )
