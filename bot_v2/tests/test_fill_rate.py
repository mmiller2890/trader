"""Offline fill-rate estimation from recorded books."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scripts.analyze_reversion import Observation
from scripts.measure_fill_rate import (
    QuoteOutcome,
    simulate_quote,
    summarize_fills,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def book(seconds: int, bid: str, ask: str) -> Observation:
    return Observation(
        token_id="t1",
        best_bid=Decimal(bid),
        best_ask=Decimal(ask),
        mid_price=(Decimal(bid) + Decimal(ask)) / 2,
        at=START + timedelta(seconds=seconds),
    )


def test_a_bid_fills_when_the_book_trades_down_to_it() -> None:
    series = [book(0, "0.49", "0.51"), book(5, "0.47", "0.49"), book(10, "0.46", "0.48")]

    outcome = simulate_quote(
        series, index=0, side="buy", offset_ticks=1,
        ttl_seconds=30, tick_size=Decimal("0.01"),
    )

    # Bid rests one tick under the bid, at 0.48; the book reaches it by t=5.
    assert outcome.filled is True
    assert outcome.seconds_to_fill == 5


def test_a_bid_does_not_fill_when_the_market_walks_away() -> None:
    series = [book(0, "0.49", "0.51"), book(5, "0.55", "0.57"), book(10, "0.60", "0.62")]

    outcome = simulate_quote(
        series, index=0, side="buy", offset_ticks=1,
        ttl_seconds=30, tick_size=Decimal("0.01"),
    )

    assert outcome.filled is False


def test_a_quote_expires_at_its_ttl() -> None:
    series = [book(0, "0.49", "0.51"), book(60, "0.40", "0.42")]

    outcome = simulate_quote(
        series, index=0, side="buy", offset_ticks=1,
        ttl_seconds=30, tick_size=Decimal("0.01"),
    )

    # Price reaches the quote, but only after the TTL has expired.
    assert outcome.filled is False


def test_summary_reports_fill_rate_and_median_latency() -> None:
    outcomes = [
        QuoteOutcome(filled=True, seconds_to_fill=4.0),
        QuoteOutcome(filled=True, seconds_to_fill=6.0),
        QuoteOutcome(filled=False, seconds_to_fill=None),
        QuoteOutcome(filled=False, seconds_to_fill=None),
    ]

    summary = summarize_fills(outcomes)

    assert summary["quotes"] == 4
    assert summary["fill_rate"] == 0.5
    assert summary["median_seconds_to_fill"] == 5.0


def test_summary_states_it_is_an_upper_bound() -> None:
    summary = summarize_fills([QuoteOutcome(filled=True, seconds_to_fill=1.0)])
    assert "upper bound" in summary["caveat"]


def test_empty_input_does_not_divide_by_zero() -> None:
    assert summarize_fills([])["quotes"] == 0
