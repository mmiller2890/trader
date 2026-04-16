from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from config.schema import AppConfig
from execution.order_builder import OrderBuilder
from models.market import MarketSnapshot
from models.signal import SignalSide, TradeSignal


def test_order_builder_creates_deterministic_request() -> None:
    builder = OrderBuilder(AppConfig())
    now = datetime.now(tz=UTC)
    snapshot = MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.44"),
        best_ask=Decimal("0.45"),
        mid_price=Decimal("0.445"),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        received_ts=now,
        source_ts=now,
    )
    signal = TradeSignal(
        signal_id="signal12345678",
        strategy_name="spike",
        market_id="m1",
        token_id="t1",
        side=SignalSide.BUY,
        reference_price=Decimal("0.40"),
        target_price=Decimal("0.45"),
        observed_move_bps=100,
        reason="test",
    )

    order = builder.build(signal=signal, snapshot=snapshot)

    assert order.client_order_id == "pm-bot-signal12345678"
    assert order.price == Decimal("0.45")
    assert order.size == Decimal("5")
    assert order.side.value == "buy"
