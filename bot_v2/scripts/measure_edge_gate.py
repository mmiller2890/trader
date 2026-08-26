"""
Measure how often the edge gate would refuse, from recorded books.

This is the offline stand-in for a shadow-mode session. Shadow mode exists to
answer one question -- how often would the gate abstain? -- and answering it
does not require the bot to be running: ``assess_edge`` is a pure function of
price and spread, and both are in every recorded book. Replaying them answers
the same question against the whole recording instead of one live window.

Two things this does NOT reproduce, and neither is a detail:

* **It assumes a constant edge for every book.** The strategy only raises
  signals on spikes, and a spike's edge is not the average edge, so the true
  approval rate on real signals differs from the rate reported here. Pass
  ``--edge-bps`` to see the sensitivity.
* **It reports per observed book, not per signal.** Signal arrival rate is a
  property of the strategy, not the book.

Read it as: of the book states the market actually presented, what fraction
could support a trade of a given edge net of fees and spread.

    python3 -m scripts.measure_edge_gate --input data/research/books.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from decimal import Decimal
from pathlib import Path

from config.loader import load_config
from risk.edge import EdgeDecision, assess_edge

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/research/books.jsonl")
    parser.add_argument(
        "--edge-bps",
        default="120",
        help="edge assumed for every book; the measured directional edge is ~120",
    )
    parser.add_argument("--config-dir", default=str(PROJECT_ROOT / "config"))
    args = parser.parse_args(argv)

    path = Path(args.input)
    if not path.exists():
        print(f"no such file: {path}. Run scripts.record_books first.")
        return 2

    config = load_config(Path(args.config_dir))
    fee_rate = config.execution.fee_rate
    margin = config.risk.safety_margin_bps
    edge_bps = Decimal(args.edge_bps)

    required: dict[str, list[float]] = {"maker": [], "taker": []}
    approvals: dict[str, int] = {"maker": 0, "taker": 0}
    spreads: list[float] = []
    skipped = 0

    with path.open() as handle:
        for line in handle:
            try:
                row = json.loads(line)
                bid = Decimal(row["best_bid"])
                ask = Decimal(row["best_ask"])
                mid = Decimal(row["mid_price"])
            except Exception:
                skipped += 1
                continue
            if bid <= 0 or ask <= 0 or mid <= 0 or ask <= bid:
                skipped += 1
                continue
            spread_bps = (ask - bid) / mid * Decimal("10000")
            spreads.append(float(spread_bps))
            for style, is_maker in (("maker", True), ("taker", False)):
                assessment = assess_edge(
                    edge_bps=edge_bps,
                    price=mid,
                    spread_bps=spread_bps,
                    fee_rate=fee_rate,
                    is_maker_entry=is_maker,
                    safety_margin_bps=margin,
                )
                required[style].append(float(assessment.required_bps))
                if assessment.decision == EdgeDecision.APPROVE:
                    approvals[style] += 1

    total = len(spreads)
    if total == 0:
        print("no usable observations")
        return 2

    print(
        json.dumps(
            {
                "observations": total,
                "skipped": skipped,
                "assumed_edge_bps": float(edge_bps),
                "fee_rate": float(fee_rate),
                "safety_margin_bps": float(margin),
                "spread_bps_median": round(statistics.median(spreads), 1),
                "approval_rate_maker_entry": round(approvals["maker"] / total, 4),
                "approval_rate_taker_entry": round(approvals["taker"] / total, 4),
                "required_bps_median_maker_entry": round(
                    statistics.median(required["maker"]), 1
                ),
                "required_bps_median_taker_entry": round(
                    statistics.median(required["taker"]), 1
                ),
                "required_bps_p90_maker_entry": round(
                    _percentile(required["maker"], 0.9), 1
                ),
            },
            indent=2,
        )
    )
    print(
        "\nPer observed book, not per signal, and assumes the same edge for "
        "every book.\nA spike's edge is not the average edge -- use --edge-bps "
        "to test sensitivity."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
