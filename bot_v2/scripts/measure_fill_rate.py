"""
Estimate how often a resting post-only quote would have filled.

Dry run cannot answer this: post-only orders there return filled_size=0
permanently by design, because inventing fills at the quoted price is the most
flattering fiction available to a maker strategy. This replays recorded books
instead.

A resting bid at price X is treated as filled if the book's best bid later
reaches X or below within the quote's TTL -- someone was willing to sell there.
Queue position is ignored, so a real quote would fill less often than this
reports. The number is an UPPER BOUND: failing it is conclusive, passing it is
not.

    python3 -m scripts.measure_fill_rate --input data/research/books.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from scripts.analyze_reversion import Observation, load


@dataclass(frozen=True)
class QuoteOutcome:
    """What happened to one hypothetical resting quote."""

    filled: bool
    seconds_to_fill: float | None


def simulate_quote(
    series: list[Observation],
    *,
    index: int,
    side: Literal["buy", "sell"],
    offset_ticks: int,
    ttl_seconds: float,
    tick_size: Decimal,
) -> QuoteOutcome:
    """Rest a quote at ``offset_ticks`` behind the touch and see if it trades."""

    start = series[index]
    offset = Decimal(offset_ticks) * tick_size
    price = (
        start.best_bid - offset if side == "buy" else start.best_ask + offset
    )
    deadline = start.at + timedelta(seconds=ttl_seconds)

    for future in series[index + 1 :]:
        if future.at > deadline:
            break
        reached = (
            future.best_bid <= price if side == "buy" else future.best_ask >= price
        )
        if reached:
            return QuoteOutcome(
                filled=True,
                seconds_to_fill=(future.at - start.at).total_seconds(),
            )
    return QuoteOutcome(filled=False, seconds_to_fill=None)


def summarize_fills(outcomes: list[QuoteOutcome]) -> dict[str, object]:
    """Reduce simulated quotes to a fill rate and a latency."""

    caveat = (
        "queue position is ignored, so this is an upper bound on fill rate; "
        "a real quote fills less often"
    )
    if not outcomes:
        return {"quotes": 0, "caveat": caveat}
    filled = [o for o in outcomes if o.filled]
    latencies = [o.seconds_to_fill for o in filled if o.seconds_to_fill is not None]
    return {
        "quotes": len(outcomes),
        "fill_rate": round(len(filled) / len(outcomes), 4),
        "median_seconds_to_fill": (
            round(statistics.median(latencies), 2) if latencies else None
        ),
        "caveat": caveat,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/research/books.jsonl")
    parser.add_argument("--offset-ticks", type=int, default=1)
    parser.add_argument("--ttl-seconds", type=float, default=30.0)
    parser.add_argument("--tick-size", default="0.01")
    parser.add_argument(
        "--sample-every",
        type=int,
        default=500,
        help="place a hypothetical quote every N observations",
    )
    args = parser.parse_args(argv)

    path = Path(args.input)
    if not path.exists():
        print(f"no such file: {path}. Run scripts.record_books first.")
        return 2

    tick = Decimal(args.tick_size)
    outcomes: list[QuoteOutcome] = []
    for series in load(path).values():
        for index in range(0, len(series), max(1, args.sample_every)):
            for side in ("buy", "sell"):
                outcomes.append(
                    simulate_quote(
                        series,
                        index=index,
                        side=side,  # type: ignore[arg-type]
                        offset_ticks=args.offset_ticks,
                        ttl_seconds=args.ttl_seconds,
                        tick_size=tick,
                    )
                )

    summary = summarize_fills(outcomes)
    print(json.dumps(summary, indent=2))
    print()
    print("Pre-registered reading (set before this was run):")
    print("  fill >= 20% and P&L positive -> thesis holds, proceed")
    print("  fill >= 20% and P&L negative -> adverse selection; maker entry dead")
    print("  fill <  20% and P&L positive -> viable but capital-starved")
    print("  fill <  20% and P&L negative -> dead; stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
