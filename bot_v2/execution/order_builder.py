"""Convert approved signals into concrete order requests."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from config.schema import AppConfig, Mode
from models.market import MarketSnapshot
from models.order import OrderRequest, OrderSide, OrderTimeInForce
from models.signal import TradeSignal
from portfolio.sizing import clamp, fixed_size


class OrderBuilder:
    """Deterministic builder from signal to order request."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def build(
        self,
        *,
        signal: TradeSignal,
        snapshot: MarketSnapshot,
        size: Decimal | None = None,
    ) -> OrderRequest:
        """Create validated order request from signal and current market snapshot."""

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
        price = snapshot.best_ask if signal.side.value == "buy" else snapshot.best_bid
        live_trading_enabled = (
            self._config.bot.mode == Mode.LIVE
            and self._config.execution.allow_live_trading
            and not self._config.execution.dry_run_force
        )
        if live_trading_enabled:
            visible_size = (
                snapshot.top_ask_size
                if signal.side.value == "buy"
                else snapshot.top_bid_size
            )
            size_increment = Decimal("0.01")
            cap_size = (
                self._config.execution.max_live_order_notional / price
            ).quantize(size_increment, rounding=ROUND_DOWN)
            required_size = self._config.execution.min_order_size
            if signal.side.value == "buy":
                required_size = max(
                    required_size,
                    (
                        self._config.execution.min_live_buy_notional / price
                    ).quantize(size_increment, rounding=ROUND_UP),
                )
            maximum_size = min(
                self._config.execution.max_order_size,
                visible_size,
                cap_size,
            ).quantize(
                size_increment,
                rounding=ROUND_DOWN,
            )
            if required_size > maximum_size:
                if signal.side.value == "buy":
                    raise ValueError(
                        "live execution cannot satisfy minimum live BUY notional "
                        "within order-size, notional-cap, and visible-liquidity limits"
                    )
                raise ValueError(
                    "live execution cannot satisfy minimum order size within "
                    "notional cap and visible liquidity"
                )
            normalized_size = min(
                max(normalized_size, required_size),
                maximum_size,
            ).quantize(size_increment, rounding=ROUND_DOWN)
        client_order_id = f"{self._config.execution.client_order_id_prefix}-{signal.signal_id[:18]}"

        return OrderRequest(
            client_order_id=client_order_id,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=OrderSide(signal.side.value),
            price=price,
            size=normalized_size,
            time_in_force=(
                signal.time_in_force
                if signal.time_in_force is not None
                else OrderTimeInForce(self._config.execution.time_in_force.value)
            ),
            signal_id=signal.signal_id,
            strategy_name=signal.strategy_name,
        )
