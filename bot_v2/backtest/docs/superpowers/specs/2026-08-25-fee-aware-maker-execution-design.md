# Fee-Aware Maker Execution Design

## Goal

Make the bot able to distinguish a profitable trade from an unprofitable one,
and then place trades on the side of the fee schedule that can win.

## Why

Polymarket charges takers a per-category fee and pays makers zero:

```
fee = shares x feeRate x p x (1 - p)          crypto feeRate = 0.07
```

Expressed as basis points of notional this simplifies usefully:

```
fee_bps = feeRate x (1 - p) x 10000
```

| entry price | taker fee | round trip |
|---|---|---|
| 0.30 | 490 bps | 980 bps |
| 0.50 | 350 bps | 700 bps |
| 0.70 | 210 bps | 420 bps |

Against measured figures from `data/journal/events.jsonl` and 664k recorded
book observations:

- measured directional edge: **~120 bps** (60s horizon, excluding outliers)
- spread, round trip: **~200 bps**
- taker fees, round trip at p=0.50: **~700 bps**

```
900 bps of cost against 120 bps of edge  ->  -780 bps per trade
```

Taker directional trading on these markets is not marginal, it is off by
roughly 7x. No entry threshold, confirmation filter, or holding-period tweak
closes that gap. This explains why both the reversion and momentum
configurations lost, and why nothing in the trade data predicted outcome:
results were determined by costs, not by signal.

`config/bot.yaml` currently declares `taker_fee_bps: 10`. The real figure at
p=0.50 is 350. Every dry-run result produced so far understates cost by about
35x, so the bot has never been able to evaluate its own performance.

**Makers pay zero and collect a rebate funded by taker fees.** The maker/taker
distinction is not an optimisation here; it is the entire economics.

## Scope

In scope:

1. A fee model, used by risk and by accounting.
2. An edge gate with an explicit abstain state.
3. Maker-first execution: post-only entries, maker exits with a taker fallback.
4. An offline fill-rate measurement, since dry run cannot produce one.
5. A spike (Path B) measuring whether an exchange-spot proxy tracks the
   settlement oracle closely enough to compute fair value.

Out of scope: the market-maker strategy (`strategies/market_maker.py`) keeps
its inventory-skew semantics and is not modified. Backtest replay changes only
where the fee model is substituted.

## Component 1: Fee model

New `models/fees.py`, a pure module with no I/O.

```
CATEGORY_FEE_RATES = {"crypto": 0.07, "politics": 0.04, "sports": 0.05, ...}

taker_fee(shares, price, fee_rate)  -> Decimal   # dollars
taker_fee_bps(price, fee_rate)      -> Decimal   # bps of notional
maker_fee(...)                      -> Decimal   # always zero
```

`ExecutionConfig` gains `fee_rate` (default 0.07) and `assume_maker_rebate_bps`
(default 0; the rebate exists but its size is not documented, so it is not
counted as revenue until measured).

`BacktestConfig.taker_fee_bps` is replaced by the model. The fixed-bps field is
removed rather than defaulted, so no code silently keeps the wrong number.

Fees are applied in two places:

- `state/store.py` when a confirmed fill updates realised P&L, so journalled
  P&L is net of fees.
- The edge gate below, before an order is built.

## Component 2: Edge gate and abstain

New `risk/edge.py`. A signal must clear its own cost before it is routed:

```
required_bps = entry_cost + exit_cost + safety_margin_bps

  taker entry: taker_fee_bps(price) + half_spread_bps
  maker entry: 0                    - half_spread_bps   (earned, not paid)
```

`safety_margin_bps` defaults to 50: enough to absorb one tick of adverse
rounding at mid prices without swallowing the entire measured edge. It is
configuration, not a constant, so it can be tightened once live fill data
exists.

Exit cost is estimated at the current price, since the exit price is unknown at
entry. The estimate is deliberately pessimistic: it assumes a taker exit, so
maker exits that do fill are upside rather than a modelled assumption.

The gate returns one of `APPROVE`, `ABSTAIN`, or `REJECT`. Abstain is distinct
from "no signal" and is journalled with the numbers that produced it
(`edge_bps`, `required_bps`, `fee_bps`, `spread_bps`), so a quiet bot can be
audited rather than guessed at.

`PreTradeRiskEngine` gains this as one more check. Maker quotes are exempt from
the taker-fee component but not from the gate itself.

**Expected effect:** most current signals will be rejected. At p=0.50 a taker
round trip needs 700 bps of edge before spread. A much quieter bot is the
correct outcome of this change, not a regression.

### Shadow mode, and why it is required

The gate as specified deadlocks against its own measurement. Worked through at
p=0.50 with a ~200 bps spread:

```
maker entry   -100   (half spread earned)
taker exit    +350   (fee, modelled pessimistically)
              +100   (half spread paid on that exit)
margin         +50
              ----
required       400 bps   against a measured edge of ~121 bps
```

That is not selective, it is total abstention. And abstaining means no resting
quotes, which means no fills, which means the exit-cost model that caused the
abstention can never be calibrated. Step 2 forecloses step 3.

The gate therefore supports `shadow` mode: evaluate normally, journal the full
decision with its numbers, and route the order anyway.

**Shadow mode is hard-guarded to dry run.** A flag that logs `REJECT` and
trades regardless is, in live mode, a fee-gate bypass with real money. The
guard belongs in the config validator, not in operator discipline:

```
if mode is LIVE and edge_gate_mode is SHADOW:
    raise ConfigError("edge gate shadow mode is dry-run only")
```

This mirrors the existing live-arming bundle validation, which already refuses
incoherent mode combinations at load time.

## Component 3: Maker-first execution

Entries become post-only. `SpikeStrategyConfig` gains
`entry_style: taker | maker` (default `maker`).

In maker mode the strategy emits a `MAKER_QUOTE` signal priced inside the
spread on the favoured side, reusing the existing signal type, the
`QuotePlan` cancel-before-replace path, and the post-only order builder. The
quote carries a TTL; unfilled quotes are cancelled and re-priced rather than
left resting through a regime change.

Exits gain an escalation, in `portfolio/exit_policy.py`:

```
1. post-only reduce-only quote at the target price
2. after maker_exit_deadline_seconds, or within
   exit_before_market_end_seconds of expiry, escalate to IOC taker
```

`PositionManagementConfig` gains `exit_style: taker | maker_first` and
`maker_exit_deadline_seconds` (default 30).

The taker fallback is mandatory and non-configurable in the sense that it
cannot be disabled: inventory left unexited at resolution is a coin flip on
full notional and trips the rotation kill switch by design. Earning a spread is
not worth that risk.

### Known hazard: adverse selection

A resting bid fills when the market comes to it, which is disproportionately
when the directional view was wrong. A post-only entry therefore gets filled on
its losers and misses its winners.

This design accepts that, because the strategy is no longer betting on
direction. The spike detector fires on dislocations -- mean detected move is
1336 bps and 38 of 61 trades in an early run landed on dislocated books -- so
the quote is being paid the spread for supplying liquidity that was just
consumed. Direction is incidental; the spread and the absent fee are the
return.

This is the assumption most likely to be wrong, and the fill-rate measurement
below is what tests it.

### Fill rate cannot be measured in dry run

Dry run returns post-only orders as `SUBMITTED` with `filled_size=0`
permanently -- `execution/submitter.py` calls this `simulated_resting_quote`,
and `test_dry_run_post_only_quote_rests_instead_of_filling` pins it. That was
deliberate: inventing fills at the quoted price is the most flattering fiction
available to a maker strategy. The consequence is that **dry-run fill rate is
structurally zero**, so no amount of shadow-mode routing produces the number
this design depends on.

Fill rate has exactly two honest sources:

**Offline replay against recorded books (do this first).** For a hypothetical
quote at price X placed at time T, scan forward in the recorded book: if the
best bid later trades at or below X, a resting bid at X would have filled.
This ignores queue position, so it is an *upper bound* on fill rate -- which is
the useful direction, because a strategy that fails on its upper bound is dead
without risking anything. Runs against the 665k observations already recorded
by `scripts/record_books.py`; needs neither the gate nor shadow mode nor live
credentials.

**Live at minimum size.** The only ground truth, and it collides with the
0-of-110 live submission record. It confirms the offline estimate rather than
replacing it.

New `scripts/measure_fill_rate.py` implements the offline version.

### Pre-registered interpretation

Written before the measurement runs, so no result can be rationalised
afterwards. "High" is >=20% of quotes filling within their TTL; "low" is below
that. The threshold is arbitrary but fixed in advance, which is the point.

| fill rate | P&L | meaning |
|---|---|---|
| high | positive | thesis holds; proceed to live confirmation |
| high | negative | adverse selection confirmed -- resting bids are catching falling knives. Maker entry is dead |
| low | positive | viable but capital-starved; widen quotes or add markets |
| low | negative | dead; stop |

## Component 4: TWAP fair value (spike first, Path B)

Polymarket crypto up/down markets settle against a Chainlink TWAP: 60-second
window for 15-minute markets, 30-second for 5-minute, effective 7 August 2026.
The market description names the settlement source explicitly and warns it is
"not according to other sources or spot markets."

Two Chainlink products exist and only one is affordable:

| | Data Streams | On-chain Price Feed |
|---|---|---|
| settles Polymarket | yes | no |
| access | credentialed, paid, no free tier | free |
| cadence | sub-second | **~33 s, measured** |

The free on-chain feed was read successfully from
`polygon-bor-rpc.publicnode.com` at contract
`0xc907E116054Ad103354f2D350FD2514433D57F6f`. Ten consecutive rounds showed a
median gap of 33 s and price movement of ~$100 between updates on a ~$78.7k
asset. Two samples inside a 60-second settlement window cannot produce a
meaningful partial TWAP, so the free feed is **too coarse, not inaccessible**.

**Path B is therefore a spike, not a build.** Record, do not trade:

- Chainlink on-chain BTC/USD (free, ~33 s) as the oracle reference
- Coinbase and Binance BTC/USD WebSocket (free, sub-second) as candidate proxies
- the Polymarket book, already recorded by `scripts/record_books.py`

The question the spike answers:

> Does an exchange-spot proxy track the Chainlink oracle closely enough that a
> fair value computed from the proxy beats the market's quoted price by more
> than fees plus spread?

Pass bar: proxy-vs-oracle basis materially below the measured edge, and a
systematic gap between proxy-implied fair value and quoted mid that survives
cost. Kill criterion: if the basis is comparable to or larger than the edge,
Path B is dead and the alternatives are Path A (pay for Data Streams) or
Path C (drop the TWAP edge and ship components 1-3 alone).

No fair-value code is written until the spike reports.

## Data flow

```
Polymarket WS --> book snapshot --> SpikeStrategy --> signal
                                                        |
                                    fee model + spread --+--> EdgeGate
                                                        |     |
                                                        |     +--> ABSTAIN (journalled)
                                                        v
                                              MAKER_QUOTE signal
                                                        |
                                    existing QuotePlan / cancel-before-replace
                                                        |
                                              post-only order --> CLOB
                                                        |
                                              fill --> maker exit at target
                                                        |
                                     deadline or expiry --> IOC taker fallback
```

Component 4, if the spike passes, inserts a fair-value source ahead of the edge
gate and replaces the spike detector as the signal.

## Testing

Every component is unit-testable without network access.

- **Fees:** the bps identity `feeRate x (1 - p)` against the dollar formula at
  several prices; maker fee is always zero; the removed fixed-bps field cannot
  be reintroduced by config.
- **Edge gate:** a signal whose edge is below cost abstains; the abstain record
  carries the numbers; maker quotes skip the taker-fee term but not the gate; a
  genuinely profitable signal still approves; shadow mode routes a rejected
  signal while still journalling the rejection; **shadow mode plus live mode is
  refused at config load**.
- **Maker execution:** an entry is post-only and priced inside the spread; an
  unfilled quote is cancelled at TTL; a filled position exits as a maker first;
  the deadline escalates to IOC; expiry escalates regardless of deadline; a
  cancellation that does not confirm withholds the replacement (already
  covered).
- **Regression:** a *taker* round trip at p=0.50 requires >700 bps of edge
  before it is approved, pinning the arithmetic that motivated this design. The
  equivalent maker round trip requires far less, and the test asserts the gap
  between them rather than either number alone.

Integration: a dry-run session asserting that the abstain rate rises sharply
and that no taker entry is routed while `entry_style: maker`.

## Rollout

1. **Fee model**, with `taker_fee_bps` removed. Restate past recorded results
   net of real fees.
2. **Offline fill-rate measurement** against recorded books. Needs no gate and
   no execution changes, and answers the load-bearing question first. Interpret
   strictly against the pre-registered table above.
3. **Edge gate**, shadow-guarded to dry run. Success criterion stated in
   advance: **if more than 95% of signals abstain across a full session, that
   is an answer about these markets, not a tuning problem** -- they carry no
   signal that clears cost, and the correct response is to stop rather than to
   loosen the gate.
4. **Maker-first execution**, calibrated by the step 2 numbers.
5. **Live fill-rate confirmation** at minimum size, which is also the first
   real test of whether the tick-size and post-only fixes cleared the
   0-of-110 rejection rate.
6. **Path B spike**, then component 4 only on a pass.

Steps 1-3 are worth doing regardless of how 2 turns out: the bot currently
cannot evaluate its own performance at any fee assumption, and that is true
whether or not maker entry survives.

Live arming is unchanged and remains a manual operator action behind preflight,
the Telegram gate, and an explicit confirmation phrase.

## Risks and open questions

- **Fill rate is unmeasured**, and cannot be measured in dry run at all (see
  above). The offline replay gives an upper bound; only live fills give truth.
  If resting quotes rarely fill, maker execution trades a known loss for no
  trades at all.
- **The offline fill estimate ignores queue position** and so is optimistic by
  an unknown margin. It is useful for rejecting the design, not for confirming
  it: failing the upper bound is conclusive, passing it is not.
- **The maker rebate is undocumented.** It is modelled as zero. If it is
  material, the economics improve; the design does not depend on it.
- **Adverse selection**, as described above.
- **Fee rates change.** Crypto moved from 0.072 to 0.07 in July 2026. The rate
  is configuration, not a constant, and should be re-checked periodically.
- **The account holds ~$10** against a $2 per-order cap. Every conclusion here
  is about whether the strategy can work in principle; it cannot be sized
  meaningfully at current collateral.
- **The live order path has never completed a clean submission** (0 of 110 on
  2026-08-24). Tick-size and post-only fixes are believed to address it but are
  unproven live.
