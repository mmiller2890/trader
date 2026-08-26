"""
Find resting orders that have outlived their usefulness.

A post-only entry rests until it fills, and nothing at the venue expires it.
That is fine for a quoting strategy that re-prices on a TTL, but a spike entry
is a bet on a move that has already happened: once the signal is stale the
resting order is no longer expressing a view, it is just an open offer.

On 2026-08-26 one rested for 45 minutes and filled into a market that had
already ended, on a signal from 08:30. This is what stops that.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from models.order import CancelIntent, OrderResult, OrderSide

#: Reason recorded on cancels issued because the quote outlived its TTL.
ENTRY_TTL_REASON = "entry_quote_ttl_expired"

#: Reason recorded on cancels issued because the market is closing.
MARKET_ENDED_REASON = "market_ended_with_resting_order"


def stale_resting_orders(
    *,
    open_orders: list[OrderResult],
    now: datetime,
    ttl_seconds: float,
    protected_client_order_ids: set[str] | None = None,
    market_end_lookup: object | None = None,
) -> list[CancelIntent]:
    """
    Return cancels for resting maker orders that should no longer be on the book.

    Two reasons qualify. The order has outlived ``ttl_seconds``, so the signal
    that justified it is gone. Or its market has ended, where a fill would land
    inventory in a market that can no longer be traded out of.

    ``protected_client_order_ids`` are left alone. Exits are swept on their own
    deadline by PositionExitManager, which cancels and then escalates to a
    taker cross; cancelling one here would race that and could release the
    reservation while the escalation is mid-flight.

    Orders without a resolvable market_id or side are skipped rather than
    guessed at -- a cancel aimed at the wrong order is worse than a late one.
    """

    protected = protected_client_order_ids or set()
    deadline = now - timedelta(seconds=max(0.0, ttl_seconds))
    intents: list[CancelIntent] = []

    for order in open_orders:
        if order.client_order_id in protected:
            continue
        # Only resting maker orders. A taker order is never on the book long
        # enough for this to be meaningful, and liquidity is derived from the
        # submitted post_only intent.
        if order.liquidity != "maker":
            continue
        if not order.market_id or not order.token_id or order.side is None:
            continue

        reason: str | None = None
        if order.created_at <= deadline:
            reason = ENTRY_TTL_REASON
        elif market_end_lookup is not None:
            try:
                market_end_at = market_end_lookup(order.market_id, order.token_id)
            except Exception:
                market_end_at = None
            if market_end_at is not None and now >= market_end_at:
                reason = MARKET_ENDED_REASON
        if reason is None:
            continue

        intents.append(
            CancelIntent(
                client_order_id=order.client_order_id,
                exchange_order_id=order.exchange_order_id,
                market_id=order.market_id,
                token_id=order.token_id,
                side=OrderSide(order.side),
                reason=reason,
            )
        )
    return intents
