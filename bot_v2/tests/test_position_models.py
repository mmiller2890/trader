from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from models.order import OrderSide, OrderTimeInForce
from models.position import FillCheckpoint, FillApplication, PositionLifecycle
from models.signal import SignalSide, SignalType, TradeSignal


NOW = datetime(2025, 1, 1, tzinfo=UTC)


def test_position_exit_signal_carries_execution_intent() -> None:
    signal = TradeSignal(
        strategy_name="position_exit",
        signal_type=SignalType.POSITION_EXIT,
        market_id="m1", token_id="t1", side=SignalSide.SELL,
        reference_price=Decimal("0.50"), target_price=Decimal("0.55"),
        observed_move_bps=Decimal("1000"), reason="take_profit",
        requested_size=Decimal("2.5"), reduce_only=True,
        time_in_force=OrderTimeInForce.IOC,
    )
    assert signal.requested_size == Decimal("2.5")
    assert signal.reduce_only is True


def test_fill_checkpoint_rejects_negative_cumulative_values() -> None:
    with pytest.raises(ValidationError):
        FillCheckpoint(
            order_key="0xorder0001", market_id="m1", token_id="t1",
            side=OrderSide.BUY, accounted_filled_size=Decimal("-1"),
            accounted_fill_notional=Decimal("0"),
        )


def test_position_exit_signal_requires_reduce_only() -> None:
    with pytest.raises(ValidationError):
        TradeSignal(
            strategy_name="position_exit",
            signal_type=SignalType.POSITION_EXIT,
            market_id="m1", token_id="t1", side=SignalSide.SELL,
            reference_price=Decimal("0.50"), target_price=Decimal("0.55"),
            observed_move_bps=Decimal("1000"), reason="take_profit",
            reduce_only=False,
        )


def test_reduce_only_signal_requires_sell_side() -> None:
    with pytest.raises(ValidationError):
        TradeSignal(
            strategy_name="position_exit",
            signal_type=SignalType.POSITION_EXIT,
            market_id="m1", token_id="t1", side=SignalSide.BUY,
            reference_price=Decimal("0.50"), target_price=Decimal("0.55"),
            observed_move_bps=Decimal("1000"), reason="take_profit",
            reduce_only=True,
        )


def test_position_lifecycle_models_validate() -> None:
    lifecycle = PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=NOW,
        last_fill_at=NOW,
    )
    assert lifecycle.exit_attempt_count == 0
    assert lifecycle.pending_exit_client_order_id is None

    application = FillApplication(
        order_key="0xorder0001",
        delta_size=Decimal("1"),
        delta_notional=Decimal("0.5"),
        duplicate=False,
    )
    assert application.position is None
