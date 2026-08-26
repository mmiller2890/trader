# Spike trading on short-duration crypto markets

The `spike` strategy trades sharp moves in Polymarket's BTC/ETH/SOL up-down
markets. `spike_strategy.direction` decides which way:

- **`momentum`** (shipped default) — go with the move.
- **`reversion`** — fade it. The original behaviour.

## What the data says

Measured on ~250k recorded book observations, 95 episodes, with uncrossable
books excluded:

Momentum net of spread, by holding horizon, with both the entry and exit
books required to be crossable and returns measured in the instrument actually
bought:

| horizon | episodes | continue | mean | median | **excluding top 3** |
|---|---|---|---|---|---|
| 30s | 89 | 56% | +2 | +198 | −117 |
| **60s** | 86 | 66% | **+338** | +661 | **+121** |
| 120s | 80 | 60% | +182 | +474 | −111 |
| 300s | 60 | 67% | +613 | +1999 | +25 |

That is why the default is momentum, and why the hold is 60 seconds.

**Read the last column first.** The mean is dominated by a handful of episodes
at every horizon; strip the three best and most horizons collapse to roughly
zero. 60s is the only one that clearly survives, and it survives at +121 bps,
not the +338 the mean advertises.

**The distribution is negatively skewed.** Median is well above mean
everywhere, so most trades win a little and a few lose a lot. Worst episodes
run to −70%. Sizing must assume the tail, not the median.

**Sample is thin.** 86 episodes is roughly 43 independent ones — the two
outcome tokens of a market are the same events counted twice. Below the 100
the tool itself requires before rendering a verdict.

**Not included:** path-dependent stops and slippage. This is a mid-to-mid
statistic, and the bot's own realized trades have not reproduced it.

**Fees are now included, and they dominate.** This number is a gross edge of
roughly +120 bps. Crossing the spread to capture it costs ~350 bps in taker
fees at even odds. See "Why entries rest" below: the gross edge is real and
the net edge, taken as a taker, is not.

### Two measurement bugs that inflated earlier numbers

Both are fixed; both are worth knowing about because they are easy to
reintroduce.

*Filtering only the entry book.* A dislocated book could still supply the
forward price, producing moves of impossible magnitude — a long "losing" 150%
— which then dominated the mean. Both ends are now required to be crossable.

*Measuring the wrong price frame.* A bearish view is expressed by buying the
complement at `1 - p`, where the same move is a completely different
percentage: 0.05 → 0.12 is +140% in the token but −7% in its complement.
Returns are now computed in the instrument actually bought.

## Why the clock is the exit, not the stop

The book data says 65% of spikes continue. The bot's own trades came out
13 stops to 13 profits — a coin flip. Both are true: the moves continue, but
the stop fires first.

The mean continuation is ~400 bps, and the minimum stop the spread permits is
about the same size. A stop set near the mean move is a stop that fires on
noise before the move being traded develops.

So `max_hold_seconds` is the primary exit, set to the 60-second horizon the
edge was actually measured over, and the brackets are deliberately wide
(800 bps) disaster guards. If you tighten them, re-measure — do not assume
tighter is safer.

## Four things that made it unable to work

### 1. Stops sat inside the spread

Exits score `return_bps = (best_bid - avg_entry) / avg_entry`, and entries fill
at `best_ask`. A round trip therefore pays the spread before the market moves
at all.

Measured on a live book — `bitcoin-up-or-down-on-august-25-2026`, bid 0.57 /
ask 0.58 — one spread is **172 bps**. Any stop tighter than that fires on
entry. At the 0.10–0.15 prices the thesis targets, one 0.01 tick is 667–1000
bps.

`take_profit_bps` and `stop_loss_bps` are now a *minimum ambition*. Both are
raised to clear whichever floor is larger:

| Setting | Effect |
|---|---|
| `min_edge_ticks` | Take profit must be worth at least N ticks |
| `min_stop_ticks` | Stop must be at least N ticks away |
| `spread_floor_multiple` | Both must clear N × the live spread |

`ExitDecision` reports `effective_take_profit_bps` and
`effective_stop_loss_bps` so the number actually in force is visible.

This also handles the tick-size difference: the 15m markets trade on a 0.001
grid, the daily on 0.01, so the same bps threshold means a 10× different
number of ticks between them.

### 2. Half the signals could not execute

Polymarket has no borrow. A sell only fills against shares already held — in
the 2026-08-24 live session **57 sell signals were rejected** with
`insufficient_position_to_sell`.

With `sell_via_complement: true`, an upward spike now BUYs the paired NO token
at `1 - p` instead. Same economic view, always executable. The complement is
resolved from the market rotator's outcome pair; if it is unknown the strategy
falls back to a plain sell.

### 3. The lookback measured milliseconds, not time

`lookback_ticks` counts book updates. Measured from the runtime journal, those
arrive at **~250 per second per token**, so:

| lookback_ticks | actual window |
|---|---|
| 3 | 11 ms |
| 8 | 30 ms |
| ~2,600 | 10 seconds |

`lookback_seconds` measures wall clock instead and requires the window to be
genuinely spanned, not just one stale point sitting inside it.

A related trap: the history cache was bounded at 200 points, which at 250
updates/sec is **0.8 seconds**. A 20-second window could never fill and the
strategy went silently dead. History is now evicted by age when a time window
is configured.

### 4. Two supervision bugs halted the runtime

Neither is strategy logic, but both stopped the bot dead and were only visible
by running it rather than by reading it.

**The rotation loop never signalled liveness.** `Btc15mMarketRotator.run()`
blocks for nearly a whole market window while it waits for the current market
to approach its end. That idle silence is indistinguishable from a hung task,
so the supervisor's watchdog halted a healthy runtime as soon as the wait
exceeded its timeout — at exactly 300 seconds, every time. Long waits are now
chunked and beat between chunks; a genuine hang still starves the heartbeat,
which is the point.

**The duplicate guard blocked every exit retry.** Retries fire every
`exit_retry_interval_seconds` (2s) while the duplicate window is 15s, so retry
2 and 3 were rejected as duplicates, the exit budget exhausted, and the kill
switch latched on a position the bot had genuinely tried to exit only once.
`reduce_only` signals are now exempt; concurrency is still prevented by the
exit reservation, which admits one live exit per position.

### 5. Positions were too big to exit

An order is only as good as its exit. A 100-share position needs 100 shares of
bid depth to unwind; observed depth on these books is a median of ~100 with a
10th percentile of 13. When the depth was not there the exit budget exhausted
and the kill switch latched.

`default_order_size` is now 5 with `max_order_size` 10, and a config test
asserts `max_order_size <= risk.min_top_of_book_liquidity`.

## The entry price band

`min_entry_price` / `max_entry_price` (default 0.10–0.90) refuse entries near
the payout bounds. A fade entered at 0.97 risks 97 cents to make 3; no
reversion frequency rescues that reward/risk. This bit in practice — a YES
token collapsing to 0.03 routed into a 0.97 NO entry — so the band is applied
in the complement's own price frame, not the observed token's.

## Measuring the edge

Record real books, then measure whether the pattern beats its own cost:

```bash
python3 -m scripts.record_books --minutes 120 --output data/research/books.jsonl
```

```bash
python3 -m scripts.analyze_reversion --input data/research/books.jsonl --sweep
```

`--max-spread-bps` defaults to 600 to match the risk engine. Setting it to 0
scores book dislocations as price moves and will overstate the edge by roughly
2x.

The analyzer answers one question: **given a move of at least N bps over the
lookback window, where is the price T seconds later, and does the move back
beat the spread?**

Read `mean_net_bps_after_spread`. It is signed in the direction the **fade**
is positioned, so for momentum you want it clearly *negative* -- momentum's
net is `-mean_signed_reversion_bps - mean_entry_spread_bps`. A value near zero
either way means no tradeable structure at that configuration, however
striking `reversion_rate` looks.

The summary refuses to render a verdict below 100 episodes and reports
`underpowered: true` instead. A dozen episodes can show any pattern you like.

## Configuration

`config/strategies/spike.yaml` documents every field inline. The shipped
profile:

| Setting | Value | Why |
|---|---|---|
| `lookback_seconds` | 20 | Real window, not an update count |
| `spike_threshold_bps` | 45 | Measured over a real window, so lower works |
| `cooldown_seconds` | 15 | Short-lived markets need quick re-entry |
| `min_entry_price` / `max_entry_price` | 0.10 / 0.90 | Avoid lopsided payoffs |
| `sell_via_complement` | true | Makes the sell leg executable |
| `min_top_of_book_liquidity` | 20 | Passes ~71% of observed book states |
| `direction` | momentum | 65% of spikes continue on measured data |
| `max_entry_spread_bps` | 600 | See below -- this one dominated everything |

## The spread guard

Depth and tightness are different things. A book can show 100 shares on each
side while quoting **0.09 / 0.91**, and crossing it loses 82 cents a share the
instant you enter, before any market move.

Only 0.1% of observed book states are that wide -- yet in a 20-minute run the
strategy found them in **38 of 61 trades**, because a book gapping open makes
the mid lurch and that reads as a spike. The signal was substantially
detecting liquidity dislocations rather than price information.

| | no guard | guard at 600 bps |
|---|---|---|
| Trades | 61 | 21 |
| Sub-second round trips | 38 (−$30.30) | 9 (−$0.80) |
| Stop / take-profit | 40 / 23 | **13 / 13** |
| P&L | −$30.00 | −$1.30 |

Note the last two rows. Without the guard the exit split looks like strong
evidence for momentum; with it, the same strategy over the same window is a
coin flip. Any directional conclusion drawn from an unguarded run is measuring
the guard's absence.

Two related fixes: the entry price band is judged on the ask you would
actually pay rather than the mid, and complement-routed signals are risked
against their own book -- previously a tight book vouched for a broken one.

## Why entries rest

Polymarket charges the taker `shares × 0.07 × p × (1 - p)`. At p=0.50 that is
about **350 bps of notional**, against a measured directional edge of about
**120 bps**. Paying it is a ~7x loss on the thing being captured, so a strategy
that crosses to enter cannot be profitable no matter how good the signal is.
Makers pay nothing.

Everything below follows from that one arithmetic fact:

| Behaviour | Setting | Effect |
|---|---|---|
| Entries rest inside the spread as post-only quotes | `spike_strategy.entry_style: maker` | Pays no fee; risks not filling |
| Exits rest at the ask before crossing | `position_management.exit_style: maker_first` | Pays no fee when it fills in time |
| A resting exit escalates to a taker cross | `maker_exit_deadline_seconds: 30` | Bounds how long an exit may hang |
| Signals that cannot clear fees + spread are refused | `risk.edge_gate_mode: enforce` | Blocks structurally unprofitable trades |

Two exemptions are deliberate. **Exits are never gated** — refusing an exit
strands inventory into resolution, which is worse than paying to leave. And an
exit at **market expiry always crosses**, because an unfilled resting exit at
resolution leaves the full notional on a coin flip.

### What the fill rate has to be

Resting instead of crossing trades a certain 350 bps cost for an uncertain
fill. Replaying 161,881 recorded book observations puts the fill rate for a
one-tick-inside quote at **70.7%**, median **3.0s** to fill:

```bash
.venv/bin/python -m scripts.measure_fill_rate --input data/research/books.jsonl
```

Treat that as an **upper bound**: the replay ignores queue position, so a real
quote fills less often. Failing this measurement is conclusive; passing it is
not. Note also that **dry run cannot measure fill rate at all** — post-only
orders there return `filled_size=0` permanently by design, so a clean dry-run
session is evidence about wiring, never about fills.

The measurement answers only half the question. The decision rule fixed before
the number was known:

| fill rate | P&L | meaning |
|---|---|---|
| >=20% | positive | thesis holds, proceed |
| >=20% | negative | adverse selection; maker entry dead |
| <20% | positive | viable but capital-starved |
| <20% | negative | dead, stop |

The fill-rate axis is now measured and clears the bar. **The P&L axis is not
measured yet**, and the two live quadrants point opposite ways, so maker entry
is not yet shown to be viable — only not yet falsified.

### How often the gate actually refuses

Shadow mode exists to answer one question — how often would the edge gate
abstain? — and it does not need the bot running to answer it: `assess_edge` is
a pure function of price and spread, both of which are in every recorded book.

```bash
.venv/bin/python -m scripts.measure_edge_gate --input data/research/books.jsonl
```

Across 161,883 observations, with the shipped `fee_rate: 0.07` and
`safety_margin_bps: 50`, the fraction of book states that can support a trade:

| assumed edge | maker entry | taker entry |
|---|---|---|
| 120 bps (average directional edge) | 6.4% | 0.0% |
| 187 bps (measured spike edge) | 25.6% | 0.8% |
| 600 bps | 71.6% | 46.4% |

The median book demands **264 bps** to justify a maker entry and **1033 bps**
for a taker one. Two things follow. **Taker entry is dead at any plausible
edge** — the gate refuses essentially all of it, which is the plan working as
designed rather than a misconfiguration. And **maker entry at the strategy's
own measured 187 bps clears on about a quarter of books**, so enforcing the
gate is binding without being prohibitive: the bot still trades, selectively.

Read this per *observed book*, not per signal — signal arrival is a property of
the strategy, not the book — and note it assumes one edge for every book, while
a spike's edge is not the average edge. `--edge-bps` tests the sensitivity.

## Before running it live

The bot ships in `dry_run` with all three live gates closed. Dry run exercises
signalling, risk, exits, and the kill switch against real market data — it does
not tell you whether the strategy makes money, because simulated taker fills
assume you get the quoted price.

Work through the live checklist in the README, and note that
`max_live_order_notional` is held at 2 until the live order path has
demonstrated a clean acceptance rate.
