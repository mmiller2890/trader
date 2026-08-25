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
    min_edge_ticks: str = "0",
    min_stop_ticks: str = "0",
    spread_floor_multiple: str = "0",
    tick_size: str = "0.01",
    exit_style: str = "maker_first",
    maker_exit_deadline_seconds: float = 30.0,
) -> PositionExitPolicy:
    """
    Build a policy for tests.

    The tick and spread floors default to disabled here so each test exercises
    exactly the threshold it names. Tests that care about the floors turn them
    on explicitly. exit_style and maker_exit_deadline_seconds default to the
    same values as PositionManagementConfig's own defaults; tests that care
    about use_maker override them explicitly.
    """

    return PositionExitPolicy(
        PositionManagementConfig(
            take_profit_bps=Decimal(take_profit_bps),
            stop_loss_bps=Decimal(stop_loss_bps),
            max_hold_seconds=max_hold_seconds,
            exit_before_market_end_seconds=exit_before_market_end_seconds,
            min_edge_ticks=Decimal(min_edge_ticks),
            min_stop_ticks=Decimal(min_stop_ticks),
            spread_floor_multiple=Decimal(spread_floor_multiple),
            exit_style=exit_style,
            maker_exit_deadline_seconds=maker_exit_deadline_seconds,
        ),
        min_order_size=Decimal(min_order_size),
        max_data_age_seconds=max_data_age_seconds,
        tick_size_provider=lambda token_id: Decimal(tick_size),
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
    exit_first_attempted_at: datetime | None = None,
) -> PositionLifecycle:
    return PositionLifecycle(
        market_id="m1",
        token_id="t1",
        opened_at=opened_at,
        last_fill_at=opened_at,
        market_end_at=market_end_at,
        pending_exit_client_order_id=pending_exit,
        exit_first_attempted_at=exit_first_attempted_at,
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


def test_stop_inside_the_spread_is_raised_above_it() -> None:
    # The reported failure mode: entry fills at the ask and is marked against
    # the bid, so a stop narrower than the spread fires before the market moves.
    engine = policy(
        stop_loss_bps="110",
        take_profit_bps="180",
        spread_floor_multiple="2",
        tick_size="0.01",
    )
    book = snapshot(best_bid="0.57", mid="0.575")

    take_profit, stop_loss = engine.effective_thresholds(
        entry_price=Decimal("0.58"), snapshot=book
    )

    # One 0.01 spread on a 0.58 entry is ~172 bps; the floor is 2x that.
    assert stop_loss > Decimal("340")
    assert take_profit > Decimal("340")


def test_a_fresh_entry_does_not_immediately_stop_out() -> None:
    engine = policy(
        stop_loss_bps="110", spread_floor_multiple="2", tick_size="0.01"
    )
    # Bought at the ask (0.58), now marked against the bid (0.57).
    decision = engine.evaluate(
        position=position(quantity="5", average="0.58"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.57", mid="0.575"),
        now=NOW,
    )

    assert decision.should_exit is False
    assert decision.explanation == "within_thresholds"


def test_the_same_entry_would_stop_out_without_the_floor() -> None:
    # Documents precisely what the floor prevents.
    engine = policy(stop_loss_bps="110", spread_floor_multiple="0", min_stop_ticks="0")
    decision = engine.evaluate(
        position=position(quantity="5", average="0.58"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.57", mid="0.575"),
        now=NOW,
    )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.STOP_LOSS


def test_tick_floor_scales_with_price_not_bps() -> None:
    engine = policy(
        take_profit_bps="180", min_edge_ticks="2", tick_size="0.01"
    )
    cheap, _ = engine.effective_thresholds(
        entry_price=Decimal("0.10"), snapshot=snapshot(best_bid="0.09", mid="0.095")
    )
    dear, _ = engine.effective_thresholds(
        entry_price=Decimal("0.90"), snapshot=snapshot(best_bid="0.89", mid="0.895")
    )

    # Two ticks is 2000 bps at 0.10 but only 222 bps at 0.90.
    assert cheap > dear
    assert cheap >= Decimal("2000")


def test_finer_tick_market_gets_a_proportionally_smaller_floor() -> None:
    # The 15m markets trade on a 0.001 grid, the daily on 0.01.
    coarse = policy(min_edge_ticks="2", tick_size="0.01", take_profit_bps="1")
    fine = policy(min_edge_ticks="2", tick_size="0.001", take_profit_bps="1")
    book = snapshot(best_bid="0.49", mid="0.495")

    coarse_tp, _ = coarse.effective_thresholds(
        entry_price=Decimal("0.50"), snapshot=book
    )
    fine_tp, _ = fine.effective_thresholds(
        entry_price=Decimal("0.50"), snapshot=book
    )

    assert coarse_tp > fine_tp


def test_configured_threshold_wins_when_it_already_clears_the_floors() -> None:
    engine = policy(
        take_profit_bps="5000",
        stop_loss_bps="4000",
        min_edge_ticks="2",
        min_stop_ticks="2",
        spread_floor_multiple="2",
    )
    take_profit, stop_loss = engine.effective_thresholds(
        entry_price=Decimal("0.50"), snapshot=snapshot(best_bid="0.49", mid="0.495")
    )

    assert take_profit == Decimal("5000")
    assert stop_loss == Decimal("4000")


def test_effective_thresholds_are_reported_on_every_decision() -> None:
    decision = policy(spread_floor_multiple="2").evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(),
        snapshot=snapshot(best_bid="0.49", mid="0.495"),
        now=NOW,
    )

    assert decision.effective_take_profit_bps is not None
    assert decision.effective_stop_loss_bps is not None


def test_policy_falls_back_to_default_tick_when_lookup_fails() -> None:
    def broken(token_id: str) -> Decimal:
        raise RuntimeError("clob down")

    engine = PositionExitPolicy(
        PositionManagementConfig(min_edge_ticks=Decimal("2")),
        min_order_size=Decimal("1"),
        max_data_age_seconds=15,
        tick_size_provider=broken,
    )

    assert engine.tick_size_for("t1") == Decimal("0.01")


def test_a_fresh_exit_rests_as_a_maker_order() -> None:
    engine = policy(
        take_profit_bps="100", exit_style="maker_first",
        maker_exit_deadline_seconds=30,
    )

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=5)),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.should_exit is True
    assert decision.use_maker is True


def test_an_exit_past_its_deadline_crosses_the_spread() -> None:
    engine = policy(
        take_profit_bps="100", exit_style="maker_first",
        maker_exit_deadline_seconds=30,
    )

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(
            opened_at=NOW - timedelta(seconds=200),
            exit_first_attempted_at=NOW - timedelta(seconds=45),
        ),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.use_maker is False


def test_market_expiry_always_crosses_regardless_of_deadline() -> None:
    """
    Inventory left at resolution is a coin flip on full notional.

    maker_exit_deadline_seconds is capped at 600 by its schema Field(le=600.0),
    so 600 -- the largest value an operator can legally configure -- stands in
    for "arbitrarily huge" here: even at the maximum, an expiry exit must
    still cross.
    """

    engine = policy(exit_style="maker_first", maker_exit_deadline_seconds=600)

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(
            market_end_at=NOW + timedelta(seconds=10),
            exit_first_attempted_at=NOW,
        ),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.reason == ExitReason.MARKET_EXPIRY
    assert decision.use_maker is False


def test_taker_exit_style_never_rests() -> None:
    engine = policy(take_profit_bps="100", exit_style="taker")

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=5)),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.use_maker is False
