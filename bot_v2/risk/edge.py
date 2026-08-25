"""
Refuse trades that cannot clear their own cost.

Polymarket taker fees are ~350 bps of notional at even odds, against a measured
directional edge nearer 120 bps. Without this gate the bot approves trades that
lose by construction and reports them as small losses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from models.fees import taker_fee_bps


class EdgeDecision(str, Enum):
    """Outcome of a cost assessment."""

    APPROVE = "approve"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class EdgeAssessment:
    """A decision plus every number that produced it, for the journal."""

    decision: EdgeDecision
    edge_bps: Decimal
    required_bps: Decimal
    fee_bps: Decimal
    spread_bps: Decimal
    reason: str


def assess_edge(
    *,
    edge_bps: Decimal,
    price: Decimal,
    spread_bps: Decimal,
    fee_rate: Decimal,
    is_maker_entry: bool,
    safety_margin_bps: Decimal,
) -> EdgeAssessment:
    """
    Compare expected edge against the full round-trip cost.

    The exit is modelled as a taker fill even when the entry is a maker quote.
    That is deliberately pessimistic: maker exits that do fill are upside rather
    than an assumption baked into the gate.
    """

    fee_bps = taker_fee_bps(price, fee_rate)
    half_spread = spread_bps / Decimal("2")

    # A maker entry earns half the spread instead of paying it, and pays no fee.
    entry_cost = -half_spread if is_maker_entry else fee_bps + half_spread
    exit_cost = fee_bps + half_spread

    required_bps = entry_cost + exit_cost + safety_margin_bps
    approved = edge_bps >= required_bps
    return EdgeAssessment(
        decision=EdgeDecision.APPROVE if approved else EdgeDecision.ABSTAIN,
        edge_bps=edge_bps,
        required_bps=required_bps,
        fee_bps=fee_bps,
        spread_bps=spread_bps,
        reason=(
            f"edge {edge_bps:.0f}bps "
            f"{'clears' if approved else 'below'} required {required_bps:.0f}bps"
        ),
    )
