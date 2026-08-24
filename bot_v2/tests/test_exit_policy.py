from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.schema import PositionManagementConfig
from models.market import MarketSnapshot
from models.position import ExitReason, Position, PositionLifecycle
from portfolio.exit_policy import ExitDecision, PositionExitPolicy


NOW = datetime(2025, 1, 1, tzinfo=UTC)


def policy(
    *,
    take_profit_bps: str = "300",
    stop_loss_bps: str = "200",
    max_hold_seconds: float = 180,
    exit_before_market_end_seconds: float = 60,
    min_order_size: str = "1",
    max_data_age_seconds: float = 15,
) -> PositionExitPolicy:
    return PositionExitPolicy(
        PositionManagementConfig(
            take_profit_bps=Decimal(take_profit_bps),
            stop_loss_bps=Decimal(stop_loss_bps),
            max_hold_seconds=max_hold_seconds,
            exit_before_market_end_seconds=exit_before_market_end_seconds,
        ),
        min_order_size=Decimal(min_order_size),
        max_data_age_seconds=max_data_age_seconds,
    )


def position(*, quantity: str, average: str) -> Position:
    return Position(
        market_id="m1",
        token_id="t1",
        quantity=Decimal(quantity),
        average_entry_price=Decimal(average),
    )


def lifecycle(
    *,
    opened_at: datetime = NOW - timedelta(seconds=30),
    market_end_at: datetime | None = None,
    pending_exit: str | None = None,
) -> PositionLifecycle:
    return PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=opened_at,
        last_fill_at=opened_at,
        market_end_at=market_end_at,
        pending_exit_client_order_id=pending_exit,
    )


def snapshot(*, best_bid: str, mid: str, at: datetime = NOW) -> MarketSnapshot:
    bid = Decimal(best_bid)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=bid,
        best_ask=bid + Decimal("0.01"),
        mid_price=Decimal(mid),
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=at,
        received_ts=at,
    )


def test_take_profit_uses_executable_best_bid() -> None:
    decision = policy(take_profit_bps="300").evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=30)),
        snapshot=snapshot(best_bid="0.515", mid="0.52"), now=NOW,
    )
    assert decision.reason == ExitReason.TAKE_PROFIT
    assert decision.return_bps == Decimal("300")


def test_market_expiry_has_priority_over_take_profit() -> None:
    decision = policy().evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(market_end_at=NOW + timedelta(seconds=60)),
        snapshot=snapshot(best_bid="0.60", mid="0.60"), now=NOW,
    )
    assert decision.reason == ExitReason.MARKET_EXPIRY


def test_stop_loss_at_boundary() -> None:
    decision = policy(stop_loss_bps="200").evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.49", mid="0.49"), now=NOW,
    )
    assert decision.reason == ExitReason.STOP_LOSS
    assert decision.return_bps == Decimal("-200")


def test_take_profit_at_boundary() -> None:
    decision = policy(take_profit_bps="300").evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.515", mid="0.515"), now=NOW,
    )
    assert decision.reason == ExitReason.TAKE_PROFIT


def test_max_hold_at_boundary() -> None:
    decision = policy(max_hold_seconds=180).evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=180)),
        snapshot=snapshot(best_bid="0.50", mid="0.50"), now=NOW,
    )
    assert decision.reason == ExitReason.MAX_HOLD


def test_one_unit_inside_each_threshold_does_not_exit() -> None:
    decision = policy().evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=179)),
        snapshot=snapshot(best_bid="0.5149", mid="0.5149"), now=NOW,
    )
    assert decision.should_exit is False
    assert decision.reason is None


def test_pending_exit_blocks_new_decision() -> None:
    decision = policy().evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(pending_exit="exit-order-0001"),
        snapshot=snapshot(best_bid="0.60", mid="0.60"), now=NOW,
    )
    assert decision.should_exit is False
    assert "pending" in decision.explanation


def test_stale_snapshot_blocks_exit() -> None:
    decision = policy(max_data_age_seconds=15).evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.60", mid="0.60", at=NOW - timedelta(seconds=60)),
        now=NOW,
    )
    assert decision.should_exit is False
    assert "stale" in decision.explanation


def test_zero_quantity_never_exits() -> None:
    decision = policy().evaluate(
        position=position(quantity="0", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.60", mid="0.60"), now=NOW,
    )
    assert decision.should_exit is False


def test_sub_minimum_quantity_returns_dust() -> None:
    decision = policy(min_order_size="1").evaluate(
        position=position(quantity="0.5", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.60", mid="0.60"), now=NOW,
    )
    assert decision.should_exit is False
    assert decision.dust is True


def test_missing_snapshot_blocks_exit() -> None:
    decision = policy().evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=None, now=NOW,
    )
    assert decision.should_exit is False
    assert "snapshot" in decision.explanation


def test_zero_entry_price_blocks_exit() -> None:
    decision = policy().evaluate(
        position=position(quantity="2", average="0"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.60", mid="0.60"), now=NOW,
    )
    assert decision.should_exit is False
