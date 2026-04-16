"""Convert approved signals into concrete order requests."""

from __future__ import annotations

from decimal import Decimal

from config.schema import AppConfig
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

        chosen_size = size if size is not None else fixed_size(self._config.execution)
        normalized_size = clamp(
            chosen_size,
            self._config.execution.min_order_size,
            self._config.execution.max_order_size,
        )
        price = snapshot.best_ask if signal.side.value == "buy" else snapshot.best_bid
        client_order_id = f"{self._config.execution.client_order_id_prefix}-{signal.signal_id[:18]}"

        return OrderRequest(
            client_order_id=client_order_id,
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=OrderSide(signal.side.value),
            price=price,
            size=normalized_size,
            time_in_force=OrderTimeInForce(self._config.execution.time_in_force.value),
            signal_id=signal.signal_id,
            strategy_name=signal.strategy_name,
        )
