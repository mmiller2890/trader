"""Reversion measurement over recorded books."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from scripts.analyze_reversion import (
    MIN_EPISODES_FOR_A_VERDICT,
    Episode,
    Observation,
    find_episodes,
    load,
    summarize,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def series(mids: list[tuple[float, str]], *, spread: str = "0.01") -> list[Observation]:
    """Build observations from (seconds_offset, mid) pairs."""

    out = []
    for offset, mid in mids:
        m = Decimal(mid)
        half = Decimal(spread) / 2
        out.append(
            Observation(
                token_id="t1",
                best_bid=m - half,
                best_ask=m + half,
                mid_price=m,
                at=START + timedelta(seconds=offset),
            )
        )
    return out


def test_no_episodes_when_price_is_flat() -> None:
    data = series([(s, "0.50") for s in range(0, 120, 5)])

    episodes = find_episodes(
        data,
        lookback_seconds=20,
        threshold_bps=45,
        horizon_seconds=60,
        cooldown_seconds=15,
    )

    assert episodes == []


def test_a_spike_that_reverts_is_detected_and_scored() -> None:
    data = series(
        [(s, "0.50") for s in range(0, 25, 5)]
        + [(30, "0.60")]  # spike up
        + [(s, "0.50") for s in range(35, 160, 5)]  # full reversion
    )

    episodes = find_episodes(
        data,
        lookback_seconds=20,
        threshold_bps=45,
        horizon_seconds=60,
        cooldown_seconds=15,
    )

    assert len(episodes) >= 1
    spike = episodes[0]
    assert spike.move_bps > 0
    assert spike.forward_bps < 0
    assert spike.reverted is True


def test_a_spike_that_continues_is_not_counted_as_reversion() -> None:
    data = series(
        [(s, "0.50") for s in range(0, 25, 5)]
        + [(30, "0.60")]
        + [(s, "0.70") for s in range(35, 160, 5)]  # trend continues
    )

    episodes = find_episodes(
        data,
        lookback_seconds=20,
        threshold_bps=45,
        horizon_seconds=60,
        cooldown_seconds=15,
    )

    assert episodes
    assert episodes[0].reverted is False


def test_cooldown_suppresses_repeat_signals() -> None:
    data = series(
        [(s, "0.50") for s in range(0, 25, 1)]
        + [(s, "0.60") for s in range(25, 200, 1)]
    )

    without = find_episodes(
        data, lookback_seconds=20, threshold_bps=45,
        horizon_seconds=30, cooldown_seconds=0,
    )
    with_cooldown = find_episodes(
        data, lookback_seconds=20, threshold_bps=45,
        horizon_seconds=30, cooldown_seconds=60,
    )

    assert len(with_cooldown) < len(without)


def test_net_bps_subtracts_the_spread_paid_to_enter() -> None:
    episode = Episode(
        token_id="t1",
        move_bps=500.0,
        entry_spread_bps=172.0,
        forward_bps=-200.0,
    )

    # 200 bps of reversion, 172 bps of spread, leaves 28.
    assert round(episode.net_bps, 1) == 28.0


def test_summary_calls_out_reversion_that_does_not_clear_the_spread() -> None:
    episodes = [
        Episode(token_id="t1", move_bps=500, entry_spread_bps=172, forward_bps=-50)
        for _ in range(MIN_EPISODES_FOR_A_VERDICT)
    ]

    summary = summarize(episodes)

    assert summary["reversion_rate"] == 1.0
    assert summary["mean_net_bps_after_spread"] < 0
    assert "does NOT clear the spread" in summary["verdict"]


def test_summary_confirms_reversion_that_beats_the_spread() -> None:
    episodes = [
        Episode(token_id="t1", move_bps=500, entry_spread_bps=100, forward_bps=-400)
        for _ in range(MIN_EPISODES_FOR_A_VERDICT)
    ]

    summary = summarize(episodes)

    assert summary["mean_net_bps_after_spread"] > 0
    assert "clears the spread" in summary["verdict"]


def test_a_continuation_is_scored_as_a_loss_not_a_gain() -> None:
    # An upward spike is faded (short). If it keeps going up, the fade loses.
    continued = Episode(
        token_id="t1", move_bps=500, entry_spread_bps=100, forward_bps=+300
    )

    assert continued.reverted is False
    assert continued.signed_reversion_bps == -300
    assert continued.net_bps == -400


def test_a_downward_spike_that_bounces_is_scored_as_a_gain() -> None:
    # A downward spike is faded (long). A bounce up is profit.
    bounced = Episode(
        token_id="t1", move_bps=-500, entry_spread_bps=100, forward_bps=+300
    )

    assert bounced.reverted is True
    assert bounced.signed_reversion_bps == 300
    assert bounced.net_bps == 200


def test_a_small_sample_refuses_to_render_a_verdict() -> None:
    episodes = [
        Episode(token_id="t1", move_bps=500, entry_spread_bps=10, forward_bps=-5000)
        for _ in range(5)
    ]

    summary = summarize(episodes)

    # The numbers look spectacular; the tool declines to be fooled by five of
    # them.
    assert summary["mean_net_bps_after_spread"] > 0
    assert summary["underpowered"] is True
    assert "UNDERPOWERED" in summary["verdict"]


def test_empty_episode_list_summarizes_without_dividing_by_zero() -> None:
    assert summarize([])["episodes"] == 0


def test_load_reads_the_recorder_format(tmp_path: Path) -> None:
    path = tmp_path / "books.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "slug": "btc-updown-15m",
                    "market_id": "m1",
                    "token_id": "t1",
                    "tick_size": "0.01",
                    "best_bid": "0.49",
                    "best_ask": "0.51",
                    "mid_price": "0.50",
                    "top_bid_size": "100",
                    "top_ask_size": "100",
                    "source_ts": (START + timedelta(seconds=i)).isoformat(),
                    "received_ts": (START + timedelta(seconds=i)).isoformat(),
                }
            )
            for i in range(3)
        ),
        encoding="utf-8",
    )

    by_token = load(path)

    assert set(by_token) == {"t1"}
    assert len(by_token["t1"]) == 3
    assert by_token["t1"][0].spread_bps == 400.0


def wide_series(mids: list[tuple[float, str]], *, spread: str) -> list[Observation]:
    out = []
    for offset, mid in mids:
        m = Decimal(mid); half = Decimal(spread) / 2
        out.append(
            Observation(
                token_id="t1", best_bid=m - half, best_ask=m + half,
                mid_price=m, at=START + timedelta(seconds=offset),
            )
        )
    return out


def test_episodes_on_uncrossable_books_are_discarded() -> None:
    """
    A book gapping open makes the mid lurch, which reads as a price move.

    Scoring those as tradeable spikes is how a liquidity artefact gets
    mistaken for directional information.
    """

    data = wide_series(
        [(s, "0.50") for s in range(0, 25, 5)]
        + [(30, "0.60")]
        + [(s, "0.70") for s in range(35, 160, 5)],
        spread="0.40",   # 0.30 / 0.70 -- uncrossable
    )

    kept = find_episodes(
        data, lookback_seconds=20, threshold_bps=45,
        horizon_seconds=60, cooldown_seconds=15, max_spread_bps=600,
    )
    unfiltered = find_episodes(
        data, lookback_seconds=20, threshold_bps=45,
        horizon_seconds=60, cooldown_seconds=15, max_spread_bps=0,
    )

    assert unfiltered, "sanity: the spike is detectable without the filter"
    assert kept == []


def test_episodes_on_tight_books_survive_the_filter() -> None:
    data = wide_series(
        [(s, "0.50") for s in range(0, 25, 5)]
        + [(30, "0.60")]
        + [(s, "0.70") for s in range(35, 160, 5)],
        spread="0.01",
    )

    kept = find_episodes(
        data, lookback_seconds=20, threshold_bps=45,
        horizon_seconds=60, cooldown_seconds=15, max_spread_bps=600,
    )

    assert kept


def test_momentum_net_is_the_mirror_of_the_fade() -> None:
    """Both sides pay the spread; only the price component flips."""

    episodes = [
        Episode(token_id="t1", move_bps=500, entry_spread_bps=200, forward_bps=+400)
        for _ in range(MIN_EPISODES_FOR_A_VERDICT)
    ]

    summary = summarize(episodes)

    # Fade loses the 400 continuation and pays 200 spread.
    assert summary["mean_net_bps_after_spread"] == -600
    # Momentum earns the 400 and pays the same 200.
    assert summary["mean_momentum_net_bps"] == 200
    assert summary["continuation_rate"] == 1.0
    assert summary["reversion_rate"] == 0.0


def test_momentum_and_fade_are_not_simply_opposite_signs() -> None:
    """The spread is a cost to both, so the two do not sum to zero."""

    episodes = [
        Episode(token_id="t1", move_bps=500, entry_spread_bps=300, forward_bps=+100)
        for _ in range(MIN_EPISODES_FOR_A_VERDICT)
    ]

    summary = summarize(episodes)

    # A small continuation loses money on BOTH sides once the spread is paid.
    assert summary["mean_net_bps_after_spread"] < 0
    assert summary["mean_momentum_net_bps"] < 0


def test_complement_return_uses_its_own_price_frame() -> None:
    """
    A bearish view is expressed by buying the complement at 1 - p, where the
    same move is a completely different percentage.
    """

    # Token falls 0.50 -> 0.40. Momentum sells, i.e. buys complement 0.50->0.60.
    episode = Episode(
        token_id="t1",
        move_bps=-2000,
        entry_spread_bps=0,
        forward_bps=-2000,
        entry_price=Decimal("0.50"),
        exit_price=Decimal("0.40"),
    )

    # Complement went 0.50 -> 0.60 = +2000 bps, which here equals the token
    # move only because the entry sits at 0.50.
    assert round(episode.momentum_return_bps) == 2000


def test_complement_return_diverges_sharply_at_low_prices() -> None:
    """0.05 -> 0.12 is +140% in the token but only -7% in its complement."""

    episode = Episode(
        token_id="t1",
        move_bps=-3000,          # down-spike, so momentum is bearish on token
        entry_spread_bps=0,
        forward_bps=14000,       # token then ROSE 140% against the position
        entry_price=Decimal("0.05"),
        exit_price=Decimal("0.12"),
    )

    # The position is the complement: 0.95 -> 0.88, about -737 bps. Not -14000.
    assert -800 < episode.momentum_return_bps < -700


def test_a_long_can_never_lose_more_than_everything() -> None:
    """Returns past -10000 bps are a measurement bug, not a market move."""

    episode = Episode(
        token_id="t1",
        move_bps=3000,
        entry_spread_bps=0,
        forward_bps=-9900,
        entry_price=Decimal("0.50"),
        exit_price=Decimal("0.005"),
    )

    assert episode.momentum_return_bps > -10000


def test_exit_on_an_uncrossable_book_is_discarded() -> None:
    """Filtering only the entry lets a broken book supply the exit price."""

    data = [
        Observation(
            token_id="t1", best_bid=Decimal("0.49"), best_ask=Decimal("0.51"),
            mid_price=Decimal("0.50"), at=START + timedelta(seconds=s),
        )
        for s in range(0, 25, 5)
    ]
    data.append(
        Observation(
            token_id="t1", best_bid=Decimal("0.59"), best_ask=Decimal("0.61"),
            mid_price=Decimal("0.60"), at=START + timedelta(seconds=30),
        )
    )
    # The horizon lands on a dislocated book.
    data.append(
        Observation(
            token_id="t1", best_bid=Decimal("0.05"), best_ask=Decimal("0.95"),
            mid_price=Decimal("0.50"), at=START + timedelta(seconds=95),
        )
    )

    episodes = find_episodes(
        data, lookback_seconds=20, threshold_bps=45,
        horizon_seconds=60, cooldown_seconds=15, max_spread_bps=600,
    )

    assert episodes == []
