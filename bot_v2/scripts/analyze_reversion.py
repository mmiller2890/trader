"""
Measure whether short-duration crypto markets actually mean-revert.

Takes book observations from ``scripts.record_books`` and answers the question
the strategy depends on: **given a move of at least N bps over the lookback
window, where is the price T seconds later, and does the move back beat the
cost of trading it?**

The cost bar is the important part. Entry fills at the ask and is marked
against the bid, so a round trip pays the spread before it earns anything. A
reversion smaller than one spread is a losing trade no matter how reliably it
shows up.

    python3 -m scripts.analyze_reversion --input data/research/books.jsonl \\
        --lookback-seconds 20 --threshold-bps 45 --horizon-seconds 60
"""

from __future__ import annotations

import argparse
import json
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path


#: Below this, the summary refuses to render a verdict. A dozen episodes can
#: show any pattern you like; the point of this tool is to not be fooled.
MIN_EPISODES_FOR_A_VERDICT = 100


@dataclass(frozen=True)
class Observation:
    """One top-of-book observation."""

    token_id: str
    best_bid: Decimal
    best_ask: Decimal
    mid_price: Decimal
    at: datetime

    @property
    def spread_bps(self) -> float:
        if self.mid_price <= 0:
            return 0.0
        return float((self.best_ask - self.best_bid) / self.mid_price * 10000)


@dataclass
class Episode:
    """One detected spike and what happened afterwards."""

    token_id: str
    move_bps: float
    entry_spread_bps: float
    forward_bps: float
    #: Mid price at signal and at the horizon, in the observed token's frame.
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None

    def traded_return_bps(self, *, momentum: bool) -> float:
        """
        Return in the frame of the instrument actually bought.

        This is not a sign flip on ``forward_bps``. Polymarket has no borrow,
        so a bearish view on a token is expressed by buying its complement at
        ``1 - p``, and a move is a completely different percentage there:
        0.05 -> 0.12 is +140% in ``p`` but only -7% in ``1 - p``. Measuring
        the wrong frame inflates both tails and produces "losses" past 100%.
        """

        if self.entry_price is None or self.exit_price is None:
            # Fall back to the naive frame when prices were not captured.
            direction = 1.0 if self.move_bps > 0 else -1.0
            signed = direction * self.forward_bps
            return signed if momentum else -signed

        bullish_on_token = (self.move_bps > 0) if momentum else (self.move_bps < 0)
        if bullish_on_token:
            entry, exit_ = self.entry_price, self.exit_price
        else:
            entry, exit_ = Decimal("1") - self.entry_price, Decimal("1") - self.exit_price
        if entry <= 0:
            return 0.0
        return float((exit_ - entry) / entry * Decimal("10000"))

    @property
    def reverted(self) -> bool:
        """True when price moved back against the spike."""

        return self.forward_bps * self.move_bps < 0

    @property
    def signed_reversion_bps(self) -> float:
        """Return of the FADE, in the frame of the instrument it would buy."""

        return self.traded_return_bps(momentum=False)

    @property
    def momentum_return_bps(self) -> float:
        """Return of the MOMENTUM trade, in its own instrument's frame."""

        return self.traded_return_bps(momentum=True)

    @property
    def net_bps(self) -> float:
        """Reversion captured after paying one spread to get in and out."""

        return self.signed_reversion_bps - self.entry_spread_bps


def load(path: Path) -> dict[str, list[Observation]]:
    """Group observations by token, preserving arrival order."""

    by_token: dict[str, list[Observation]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            obs = Observation(
                token_id=str(raw["token_id"]),
                best_bid=Decimal(raw["best_bid"]),
                best_ask=Decimal(raw["best_ask"]),
                mid_price=Decimal(raw["mid_price"]),
                at=datetime.fromisoformat(raw["received_ts"]),
            )
            by_token.setdefault(obs.token_id, []).append(obs)
    for series in by_token.values():
        series.sort(key=lambda o: o.at)
    return by_token


def _bps(reference: Decimal, current: Decimal) -> float:
    if reference <= 0:
        return 0.0
    return float((current - reference) / reference * 10000)


def find_episodes(
    series: list[Observation],
    *,
    lookback_seconds: float,
    threshold_bps: float,
    horizon_seconds: float,
    cooldown_seconds: float,
    max_spread_bps: float = 0.0,
) -> list[Episode]:
    """
    Detect spikes and measure the move over the following horizon.

    ``max_spread_bps`` discards episodes whose entry book is too wide to
    cross. This is not a nicety: a book gapping open makes the mid lurch, so
    an unfiltered scan detects liquidity dislocations and scores them as
    price moves. Set it to the same limit the risk engine enforces, or the
    measurement describes trades the bot would never be allowed to take.
    """

    episodes: list[Episode] = []
    last_signal_at: datetime | None = None
    lookback = timedelta(seconds=lookback_seconds)
    horizon = timedelta(seconds=horizon_seconds)

    # Timestamps are sorted, so the forward lookup is a binary search. Scanning
    # a slice per candidate instead is quadratic, and on a few hundred thousand
    # observations that is the difference between seconds and hours.
    timestamps = [observation.at for observation in series]

    left = 0
    for index, current in enumerate(series):
        while series[left].at < current.at - lookback:
            left += 1
        if left >= index:
            continue
        window_start = series[left]
        if (current.at - window_start.at).total_seconds() < lookback_seconds / 2:
            continue

        move_bps = _bps(window_start.mid_price, current.mid_price)
        if abs(move_bps) < threshold_bps:
            continue
        if max_spread_bps > 0 and current.spread_bps > max_spread_bps:
            continue
        if last_signal_at is not None and (
            current.at - last_signal_at
        ) < timedelta(seconds=cooldown_seconds):
            continue

        target_at = current.at + horizon
        future_index = bisect_left(timestamps, target_at, lo=index + 1)
        if future_index >= len(series):
            continue
        future = series[future_index]
        # The exit book has to be crossable too. Filtering only the entry lets
        # a dislocated book supply the forward price, which produces moves of
        # impossible magnitude -- a long "losing" 150% -- and those outliers
        # then dominate the mean. The trade could not be exited there, so the
        # episode is not evidence about anything.
        if max_spread_bps > 0 and future.spread_bps > max_spread_bps:
            continue

        last_signal_at = current.at
        episodes.append(
            Episode(
                token_id=current.token_id,
                move_bps=move_bps,
                entry_spread_bps=current.spread_bps,
                forward_bps=_bps(current.mid_price, future.mid_price),
                entry_price=current.mid_price,
                exit_price=future.mid_price,
            )
        )
    return episodes


def summarize(episodes: list[Episode]) -> dict[str, object]:
    """Reduce episodes to the numbers that decide whether to trade this."""

    if not episodes:
        return {"episodes": 0, "verdict": "no episodes detected"}

    reverted = [e for e in episodes if e.reverted]
    nets = [e.net_bps for e in episodes]
    signed = [e.signed_reversion_bps for e in episodes]
    spreads = [e.entry_spread_bps for e in episodes]
    profitable = [n for n in nets if n > 0]

    mean_net = statistics.fmean(nets)
    # A handful of episodes cannot separate an edge from noise. This is the
    # number that decides whether the rest of the summary means anything.
    underpowered = len(episodes) < MIN_EPISODES_FOR_A_VERDICT

    if underpowered:
        verdict = (
            f"UNDERPOWERED: {len(episodes)} episodes is too few to conclude "
            f"anything. Record more data (want >= {MIN_EPISODES_FOR_A_VERDICT})."
        )
    elif mean_net > 0:
        verdict = "reversion clears the spread on average"
    else:
        verdict = (
            "reversion does NOT clear the spread; not tradeable as configured"
        )

    # Momentum is the exact mirror: it earns what the fade loses, and pays the
    # same spread to enter. Reporting it directly avoids the easy mistake of
    # reading a fade-signed number as if it described the trade being placed.
    momentum_nets = [
        e.momentum_return_bps - e.entry_spread_bps for e in episodes
    ]
    mean_momentum_net = statistics.fmean(momentum_nets)

    return {
        "episodes": len(episodes),
        "underpowered": underpowered,
        "reversion_rate": round(len(reverted) / len(episodes), 4),
        "continuation_rate": round(1 - len(reverted) / len(episodes), 4),
        "mean_momentum_net_bps": round(mean_momentum_net, 1),
        "momentum_share_net_positive": round(
            len([n for n in momentum_nets if n > 0]) / len(episodes), 4
        ),
        # Signed in the direction the fade is positioned: positive means the
        # spike came back, negative means it kept going.
        "mean_signed_reversion_bps": round(statistics.fmean(signed), 1),
        "median_signed_reversion_bps": round(statistics.median(signed), 1),
        "mean_entry_spread_bps": round(statistics.fmean(spreads), 1),
        "mean_net_bps_after_spread": round(mean_net, 1),
        "share_net_positive": round(len(profitable) / len(episodes), 4),
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/research/books.jsonl")
    parser.add_argument("--lookback-seconds", type=float, default=20.0)
    parser.add_argument("--threshold-bps", type=float, default=45.0)
    parser.add_argument("--horizon-seconds", type=float, default=60.0)
    parser.add_argument("--cooldown-seconds", type=float, default=15.0)
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=600.0,
        help=(
            "discard episodes whose entry book is wider than this; matches "
            "risk.max_entry_spread_bps. 0 disables (and will score book "
            "dislocations as price moves)"
        ),
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="scan a grid of thresholds and horizons instead of one setting",
    )
    args = parser.parse_args(argv)

    path = Path(args.input)
    if not path.exists():
        print(f"no such file: {path}. Run scripts.record_books first.")
        return 2
    by_token = load(path)
    total = sum(len(series) for series in by_token.values())
    print(f"loaded {total} observations across {len(by_token)} tokens\n")

    def run(threshold: float, horizon: float) -> dict[str, object]:
        episodes: list[Episode] = []
        for series in by_token.values():
            episodes += find_episodes(
                series,
                lookback_seconds=args.lookback_seconds,
                threshold_bps=threshold,
                horizon_seconds=horizon,
                cooldown_seconds=args.cooldown_seconds,
                max_spread_bps=args.max_spread_bps,
            )
        return summarize(episodes)

    if args.sweep:
        print(
            f"{'thr_bps':>8}{'horizon_s':>11}{'episodes':>10}"
            f"{'contin%':>9}{'fade_net':>10}{'MOM_net':>9}{'mom>0':>8}"
        )
        for threshold in (30, 45, 60, 100, 150):
            for horizon in (30, 60, 120, 300):
                summary = run(float(threshold), float(horizon))
                if not summary.get("episodes"):
                    continue
                print(
                    f"{threshold:>8}{horizon:>11}{summary['episodes']:>10}"
                    f"{summary['continuation_rate'] * 100:>8.0f}%"
                    f"{summary['mean_net_bps_after_spread']:>10.0f}"
                    f"{summary['mean_momentum_net_bps']:>+9.0f}"
                    f"{summary['momentum_share_net_positive']:>8}"
                )
        return 0

    summary = run(args.threshold_bps, args.horizon_seconds)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
