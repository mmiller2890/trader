# Market making

The `market_maker` strategy quotes both sides of a Polymarket book and earns
the spread plus any liquidity-reward share, instead of predicting direction.
It ships **disabled**. Read this page before enabling it.

## What it does

On every book update, for each subscribed token:

1. **Fair value** starts at the mid and is skewed away from the side the bot is
   already long. Holding YES pulls fair value *down* by up to
   `max_skew_ticks`, which lowers both quotes and makes the ask more likely to
   trade. Inventory works itself off instead of compounding.
2. **Two quotes** are placed around fair value, each half of
   `quote_spread_ticks` away, both post-only GTC.
3. **Sizes** scale with inventory: long shrinks the bid and grows the ask.
   Neither side may push the position past `max_position_size`, and the ask can
   never exceed shares actually held.
4. **Refresh** happens only when the computed price moves more than
   `refresh_move_ticks`, or size changes by 25% or more, or the quote outlives
   `quote_ttl_seconds`. Cancel-replace forfeits queue position, so the bot does
   not churn on every tick.
5. **Unwind**: past `inventory_unwind_ratio` of the cap, the accumulating side
   stops quoting entirely and the reducing side tightens to
   `unwind_spread_ticks`. `PositionExitManager` still owns aggressive IOC exits
   on top of this.

## Quoting two sides without a borrow

Polymarket has no short selling. An ask only exists once the shares do, so with
flat inventory the strategy posts **a bid and no ask**. This is not a bug and
it is not a partial implementation.

Genuine two-sided exposure comes from the outcome pair. Buying NO at `1 - p` is
economically identical to selling YES at `p`. When both outcome tokens of a
market are subscribed — which automatic market rotation does by default — the
strategy quotes a bid on each, and the pair straddles fair value. That is the
two-sided quote.

## Configuration

`config/strategies/market_maker.yaml` documents every field inline. The
settings that change your risk, in order of how much:

| Setting | Where | Why it matters |
|---|---|---|
| `max_live_order_notional` | `bot.yaml` | Hard ceiling on a single order. The most one mistake can cost. |
| `max_position_size` | `market_maker.yaml` | Inventory cap per token. Must stay ≤ `risk.max_single_position_size`. |
| `max_total_exposure` | `risk.yaml` | Marked notional across all positions. |
| `base_quote_size` | `market_maker.yaml` | Size per side before inventory scaling. |
| `quote_spread_ticks` | `market_maker.yaml` | Tighter earns more reward share and fills more often — including adversely. |

`risk.max_open_orders` must admit one order per side per token. Two outcome
tokens quoted two-sided needs at least 4; the shipped value is 10.

## What dry run does and does not tell you

Dry run reports a post-only quote as **resting, never filled**. It does not
invent a fill, because whether a quote at the back of the queue would have
traded is a queue-position question this process cannot answer.

So a dry-run session validates plumbing: that quotes are priced on the tick
grid, that cancel-before-replace holds, that inventory limits bind, that the
kill switch withdraws quotes. It tells you **nothing** about fill rate, spread
capture, adverse selection, or reward share. Do not read a clean dry run as
evidence the strategy is profitable. It is evidence only that it is wired.

## Safety properties

These hold by construction and are covered by tests:

- **Cancel before replace.** A refresh cancels the stale order first. If the
  cancellation does not confirm, the replacement for that side is *withheld*
  and the original stays tracked — the bot never doubles a side because a
  cancel timed out.
- **Post-only.** Quotes carry `post_only=true` to the exchange, so a quote that
  would cross is rejected rather than paying the taker fee it exists to earn.
- **Tick-grid prices.** Every price is snapped to the token's real tick size
  before signing, and the adapter refuses to submit an off-grid price.
- **Maker exemptions are narrow.** Maker quotes skip exactly three taker-shaped
  risk checks — duplicate-signal, opposing-book liquidity, and slippage.
  Kill switch, operational state, stale data, position cap, exposure cap, open
  order cap, and inventory-backed selling all still apply.
- **Halt withdraws quotes.** A latched kill switch cancels resting orders and
  clears the local quote book; reconciliation remains the authority.

## Not implemented

**Batch submission.** The SDK exposes `post_orders` for up to 15 orders per
round trip, which would cut quote-refresh latency. It is deliberately not used.
Batching adds partial-failure modes — some orders accepted, some rejected, one
ambiguous — to an order path that has not yet completed a single clean live
submission. It is worth adding once acceptance is proven, not before.

## Before enabling

1. Run in `dry_run` and confirm quotes appear, refresh sanely, and stop on halt.
2. Check `base_quote_size × price × 2 sides × 2 tokens` against your collateral.
   Underfunded quotes are rejected by the exchange, not silently reduced.
3. Confirm `max_live_order_notional` covers one quote's notional, and that your
   balance covers every quote you intend to have resting at once.
4. Work through the live checklist in the README. Market making places far more
   orders than the spike strategy, so an unproven order path fails far louder.
