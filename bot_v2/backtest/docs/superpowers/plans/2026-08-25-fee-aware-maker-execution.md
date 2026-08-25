# Fee-Aware Maker Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot price its own trading costs correctly, refuse trades that cannot clear them, and place entries on the maker side of the fee schedule — so a live session produces information rather than a predictable loss.

**Architecture:** A pure fee module feeds two consumers: realised-P&L accounting and a new pre-trade edge gate. The gate has a shadow mode (dry-run only) so it cannot starve the measurement that calibrates it. Entries become post-only `MAKER_QUOTE` signals routed through the existing `QuotePlan` machinery; exits rest as maker orders first and escalate to IOC taker at a deadline or near expiry.

**Tech Stack:** Python 3.11+, Pydantic 2, asyncio, pytest, pytest-asyncio.

**Spec:** `backtest/docs/superpowers/specs/2026-08-25-fee-aware-maker-execution-design.md`

## Global Constraints

- Polymarket crypto taker fee: `fee = shares × feeRate × p × (1 − p)`, `feeRate = 0.07`. Makers pay zero.
- In basis points of notional this is exactly `feeRate × (1 − p) × 10000`.
- Shadow mode must be impossible in live mode; the guard lives in config validation, not in operator discipline.
- The taker exit fallback cannot be disabled. Inventory unexited at resolution is a coin flip on full notional.
- Every task ends green: `.venv/bin/python -m pytest -q -p no:cacheprovider`.
- Never commit `.env`, `config/operator.yaml`, or anything under `data/`.
- Out of scope for this plan: TWAP fair value (spec component 4). It stays gated behind the Path B spike.

---

### Task 1: Fee model

**Files:**
- Create: `bot_v2/models/fees.py`
- Test: `bot_v2/tests/test_fees.py`

**Interfaces:**
- Produces: `CATEGORY_FEE_RATES: dict[str, Decimal]`, `DEFAULT_FEE_RATE: Decimal`, `taker_fee(shares, price, fee_rate) -> Decimal`, `taker_fee_bps(price, fee_rate) -> Decimal`, `maker_fee(shares, price, fee_rate) -> Decimal`.

- [ ] **Step 1: Write the failing test**

```python
"""Polymarket per-category fee model."""

from __future__ import annotations

from decimal import Decimal

import pytest

from models.fees import (
    CATEGORY_FEE_RATES,
    DEFAULT_FEE_RATE,
    maker_fee,
    taker_fee,
    taker_fee_bps,
)

CRYPTO = Decimal("0.07")


def test_taker_fee_matches_the_published_formula() -> None:
    # fee = shares * rate * p * (1 - p); 100 shares at 0.50 on crypto.
    assert taker_fee(Decimal("100"), Decimal("0.50"), CRYPTO) == Decimal("1.7500")


def test_taker_fee_peaks_at_the_midpoint() -> None:
    mid = taker_fee(Decimal("100"), Decimal("0.50"), CRYPTO)
    for price in ("0.10", "0.30", "0.70", "0.90"):
        assert taker_fee(Decimal("100"), Decimal(price), CRYPTO) < mid


def test_fee_bps_identity_equals_rate_times_one_minus_price() -> None:
    # fee/notional = (shares*rate*p*(1-p)) / (shares*p) = rate*(1-p)
    for price in ("0.10", "0.30", "0.50", "0.70", "0.90"):
        p = Decimal(price)
        shares = Decimal("100")
        dollars = taker_fee(shares, p, CRYPTO)
        expected = dollars / (shares * p) * Decimal("10000")
        assert abs(taker_fee_bps(p, CRYPTO) - expected) < Decimal("0.0001")


def test_fee_bps_at_the_prices_that_motivated_this_work() -> None:
    assert round(taker_fee_bps(Decimal("0.50"), CRYPTO)) == 350
    assert round(taker_fee_bps(Decimal("0.30"), CRYPTO)) == 490
    assert round(taker_fee_bps(Decimal("0.70"), CRYPTO)) == 210


def test_makers_pay_nothing() -> None:
    assert maker_fee(Decimal("100"), Decimal("0.50"), CRYPTO) == Decimal("0")


def test_zero_price_does_not_divide_by_zero() -> None:
    assert taker_fee_bps(Decimal("0"), CRYPTO) == Decimal("0")


def test_crypto_rate_is_the_documented_one() -> None:
    assert CATEGORY_FEE_RATES["crypto"] == CRYPTO
    assert DEFAULT_FEE_RATE == CRYPTO
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fees.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.fees'`

- [ ] **Step 3: Write minimal implementation**

```python
"""
Polymarket trading fees.

Takers pay a per-category fee that peaks at the midpoint and falls to zero at
the price extremes. Makers pay nothing and earn a rebate funded by taker fees,
which is why the maker/taker distinction dominates the economics of any
short-horizon strategy on these markets.
"""

from __future__ import annotations

from decimal import Decimal

#: Per-category taker fee rates, as published by Polymarket in 2026.
CATEGORY_FEE_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "politics": Decimal("0.04"),
    "finance": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "mentions": Decimal("0.04"),
    "geopolitics": Decimal("0"),
}

#: This bot trades crypto up/down markets.
DEFAULT_FEE_RATE = CATEGORY_FEE_RATES["crypto"]


def taker_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Fee in dollars for crossing the spread, per the published formula."""

    return shares * fee_rate * price * (Decimal("1") - price)


def taker_fee_bps(price: Decimal, fee_rate: Decimal) -> Decimal:
    """
    Taker fee as basis points of notional.

    Dividing the dollar fee by notional cancels the share count and one factor
    of price, leaving ``fee_rate * (1 - price)``. Fees are therefore cheapest
    on lopsided markets and most expensive at even odds.
    """

    if price <= 0:
        return Decimal("0")
    return fee_rate * (Decimal("1") - price) * Decimal("10000")


def maker_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Makers pay nothing. Present so callers need not special-case the side."""

    _ = (shares, price, fee_rate)
    return Decimal("0")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fees.py`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add bot_v2/models/fees.py bot_v2/tests/test_fees.py
git commit -m "feat: model polymarket per-category taker fees"
```

---

### Task 2: Replace the fixed backtest fee with the real model

**Files:**
- Modify: `bot_v2/config/schema.py:128-137` (`BacktestConfig`)
- Modify: `bot_v2/config/bot.yaml:112-117` (`backtest` block)
- Modify: `bot_v2/backtest/orderbook.py:94-128` (`quote`)
- Modify: `bot_v2/backtest/replay.py:175-184`
- Modify: `bot_v2/backtest/test_models.py`, `test_portfolio.py`, `test_replay.py`, `test_metrics.py`, `test_orderbook.py`, `conftest.py` — every `taker_fee_bps=` becomes `fee_rate=`
- Test: `bot_v2/backtest/test_orderbook.py`

**Interfaces:**
- Consumes: `models.fees.taker_fee`, `DEFAULT_FEE_RATE`.
- Produces: `BacktestConfig.fee_rate: Decimal`; `OrderBookState.quote(order, *, max_slippage_bps, fee_rate)`.

`taker_fee_bps` is removed outright rather than defaulted. A wrong constant that silently persists is how the bot came to believe fees were 10 bps for its entire life; renaming forces every call site to be revisited.

- [ ] **Step 1: Write the failing test**

Append to `bot_v2/backtest/test_orderbook.py`:

```python
def test_quote_charges_the_price_dependent_taker_fee() -> None:
    from decimal import Decimal

    from backtest.orderbook import OrderBookState
    from models.order import OrderRequest, OrderSide, OrderTimeInForce

    book = OrderBookState(market_id="m1", token_id="t1")
    book.asks[Decimal("0.50")] = Decimal("100")
    order = OrderRequest(
        client_order_id="fee-test-00001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal("0.50"),
        size=Decimal("100"),
        time_in_force=OrderTimeInForce.GTC,
    )

    report = book.quote(
        order, max_slippage_bps=Decimal("0"), fee_rate=Decimal("0.07")
    )

    # 100 shares * 0.07 * 0.50 * 0.50 = 1.75, not notional * bps.
    assert report.total_fees == Decimal("1.7500")


def test_quote_fee_falls_toward_the_price_extremes() -> None:
    from decimal import Decimal

    from backtest.orderbook import OrderBookState
    from models.order import OrderRequest, OrderSide, OrderTimeInForce

    def fee_at(price: str) -> Decimal:
        book = OrderBookState(market_id="m1", token_id="t1")
        book.asks[Decimal(price)] = Decimal("100")
        order = OrderRequest(
            client_order_id="fee-test-00002",
            market_id="m1",
            token_id="t1",
            side=OrderSide.BUY,
            price=Decimal(price),
            size=Decimal("100"),
            time_in_force=OrderTimeInForce.GTC,
        )
        return book.quote(
            order, max_slippage_bps=Decimal("0"), fee_rate=Decimal("0.07")
        ).total_fees

    assert fee_at("0.90") < fee_at("0.50")
    assert fee_at("0.10") < fee_at("0.50")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider backtest/test_orderbook.py -k taker_fee`
Expected: FAIL — `quote() got an unexpected keyword argument 'fee_rate'`

- [ ] **Step 3: Write minimal implementation**

In `bot_v2/config/schema.py`, replace the `taker_fee_bps` field in `BacktestConfig`:

```python
    starting_cash: Decimal = Field(default=Decimal("1000"), gt=Decimal("0"))
    # Per-share fee rate, not basis points: the real fee is price-dependent
    # (rate * p * (1 - p)), so a flat bps figure is wrong at every price
    # except by coincidence. Set to 0 in tests that want fee-free arithmetic.
    fee_rate: Decimal = Field(default=DEFAULT_FEE_RATE, ge=Decimal("0"), le=Decimal("1"))
    allow_short_positions: bool = True
```

Add the import near the other model imports in `schema.py`:

```python
from models.fees import DEFAULT_FEE_RATE
```

In `bot_v2/backtest/orderbook.py`, change the signature and the fee line:

```python
    def quote(
        self,
        order: OrderRequest,
        *,
        max_slippage_bps: Decimal,
        fee_rate: Decimal,
    ) -> ExecutionReport:
```

```python
            take = min(remaining, size)
            notional = price * take
            fee = taker_fee(take, price, fee_rate)
```

with, at the top of `orderbook.py`:

```python
from models.fees import taker_fee
```

In `bot_v2/backtest/replay.py:182`:

```python
            fee_rate=self._config.backtest.fee_rate,
```

In `bot_v2/config/bot.yaml`, replace `taker_fee_bps: 10` with:

```yaml
  # Per-share fee rate. The real Polymarket fee is rate * p * (1 - p), which is
  # ~350 bps of notional at p=0.50 -- the previous flat 10 bps understated cost
  # by roughly 35x and made every backtest result meaningless.
  fee_rate: 0.07
```

Then update every test that constructs a fee: replace `taker_fee_bps="0"` with `fee_rate="0"`, and `taker_fee_bps="10"` with `fee_rate="0.07"`, across `backtest/conftest.py`, `test_models.py`, `test_portfolio.py`, `test_replay.py`, `test_metrics.py`, `test_orderbook.py`. In `test_models.py` the assertion becomes:

```python
    assert config.backtest.fee_rate == Decimal("0.07")
```

and the validation case becomes:

```python
        AppConfig(backtest={"fee_rate": "-1"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider backtest/ tests/test_config.py`
Expected: PASS. Some backtest P&L assertions will now differ because fees are real; update the expected numbers to the values the price-dependent formula produces, and add a one-line comment at each showing the arithmetic.

- [ ] **Step 5: Verify no reference survives**

Run: `grep -rn "taker_fee_bps" --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=.git .`
Expected: no output outside `backtest/docs/`.

- [ ] **Step 6: Commit**

```bash
git add bot_v2/config/schema.py bot_v2/config/bot.yaml bot_v2/backtest/
git commit -m "fix: charge the real price-dependent taker fee in backtests"
```

---

### Task 3: Charge fees on realised P&L

**Files:**
- Modify: `bot_v2/config/schema.py` (`ExecutionConfig`)
- Modify: `bot_v2/state/store.py:386-460` (`_apply_confirmed_fill_locked`)
- Test: `bot_v2/tests/test_position_accounting.py`

**Interfaces:**
- Consumes: `models.fees.taker_fee`, `models.fees.maker_fee`.
- Produces: `ExecutionConfig.fee_rate: Decimal`; realised P&L net of fees.

- [ ] **Step 1: Write the failing test**

Append to `bot_v2/tests/test_position_accounting.py`:

```python
@pytest.mark.asyncio
async def test_realised_pnl_is_net_of_taker_fees() -> None:
    """A round trip that looks flat gross is a loss once fees are charged."""

    from decimal import Decimal

    from config.schema import Mode
    from models.order import OrderResult, OrderSide, OrderStatus
    from state.store import InMemoryStateStore

    store = InMemoryStateStore(mode=Mode.DRY_RUN, fee_rate=Decimal("0.07"))
    now = datetime.now(tz=UTC)

    buy = OrderResult(
        client_order_id="fee-buy-000001",
        market_id="m1", token_id="t1", side=OrderSide.BUY,
        status=OrderStatus.FILLED, accepted=True,
        requested_size=Decimal("100"), filled_size=Decimal("100"),
        avg_fill_price=Decimal("0.50"),
    )
    await store.apply_confirmed_fill(
        buy, market_end_at=None, confirmed_at=now, confirmation_grace_seconds=30
    )

    sell = OrderResult(
        client_order_id="fee-sell-00001",
        market_id="m1", token_id="t1", side=OrderSide.SELL,
        status=OrderStatus.FILLED, accepted=True,
        requested_size=Decimal("100"), filled_size=Decimal("100"),
        avg_fill_price=Decimal("0.50"),
    )
    await store.apply_confirmed_fill(
        sell, market_end_at=None, confirmed_at=now, confirmation_grace_seconds=30
    )

    position = await store.get_position("m1", "t1")
    assert position is not None
    # Flat on price, but two taker fills at 0.50 cost 1.75 each.
    assert position.realized_pnl == Decimal("-3.5000")


@pytest.mark.asyncio
async def test_maker_fills_are_charged_nothing() -> None:
    from decimal import Decimal

    from config.schema import Mode
    from models.order import OrderResult, OrderSide, OrderStatus
    from state.store import InMemoryStateStore

    store = InMemoryStateStore(mode=Mode.DRY_RUN, fee_rate=Decimal("0.07"))
    now = datetime.now(tz=UTC)

    for side, cid in ((OrderSide.BUY, "mk-buy-0000001"), (OrderSide.SELL, "mk-sell-000001")):
        await store.apply_confirmed_fill(
            OrderResult(
                client_order_id=cid,
                market_id="m1", token_id="t1", side=side,
                status=OrderStatus.FILLED, accepted=True,
                requested_size=Decimal("100"), filled_size=Decimal("100"),
                avg_fill_price=Decimal("0.50"), liquidity="maker",
            ),
            market_end_at=None, confirmed_at=now, confirmation_grace_seconds=30,
        )

    position = await store.get_position("m1", "t1")
    assert position is not None
    assert position.realized_pnl == Decimal("0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_position_accounting.py -k fee`
Expected: FAIL — `InMemoryStateStore.__init__() got an unexpected keyword argument 'fee_rate'`

- [ ] **Step 3: Write minimal implementation**

Add to `ExecutionConfig` in `bot_v2/config/schema.py`:

```python
    fee_rate: Decimal = Field(default=DEFAULT_FEE_RATE, ge=Decimal("0"), le=Decimal("1"))
```

Add a `liquidity` field to `OrderResult` in `bot_v2/models/order.py`, so a fill can say which side of the fee schedule it landed on:

```python
    liquidity: Literal["taker", "maker"] = "taker"
```

with `from typing import Literal` added to that module's imports.

In `bot_v2/state/store.py`, accept the rate and charge it. In `__init__`:

```python
    def __init__(
        self,
        *,
        mode: Mode,
        kill_switch_active: bool = False,
        fee_rate: Decimal = Decimal("0"),
    ) -> None:
        ...
        self._fee_rate = fee_rate
```

Then inside `_apply_confirmed_fill_locked`, after `delta_notional` is computed and before realised P&L is updated, charge the delta's fee:

```python
        fee = (
            maker_fee(delta_size, result.avg_fill_price, self._fee_rate)
            if result.liquidity == "maker"
            else taker_fee(delta_size, result.avg_fill_price, self._fee_rate)
        )
```

and subtract `fee` wherever `realized_pnl` is accumulated for this fill. Import at the top:

```python
from models.fees import maker_fee, taker_fee
```

Finally, in `bot_v2/app/bootstrap.py`, pass the configured rate when the store is constructed:

```python
    state_store = InMemoryStateStore(
        mode=config.bot.mode,
        kill_switch_active=config.bot.kill_switch_on_startup,
        fee_rate=config.execution.fee_rate,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_position_accounting.py tests/test_bootstrap.py`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: PASS. Existing accounting tests construct the store without `fee_rate`, which defaults to 0 and leaves their arithmetic unchanged.

- [ ] **Step 6: Commit**

```bash
git add bot_v2/config/schema.py bot_v2/models/order.py bot_v2/state/store.py bot_v2/app/bootstrap.py bot_v2/tests/test_position_accounting.py
git commit -m "feat: charge trading fees against realised pnl"
```

---

### Task 4: Offline fill-rate measurement

**Files:**
- Create: `bot_v2/scripts/measure_fill_rate.py`
- Test: `bot_v2/tests/test_fill_rate.py`

**Interfaces:**
- Consumes: `scripts.analyze_reversion.load`, `scripts.analyze_reversion.Observation`.
- Produces: `QuoteOutcome`, `simulate_quote(series, index, side, offset_ticks, ttl_seconds, tick_size) -> QuoteOutcome`, `summarize_fills(outcomes) -> dict`.

Dry run returns post-only orders as `filled_size=0` forever, so fill rate has no in-process source. This replays recorded books instead: a resting bid at price X fills if the book later trades at or below X. Queue position is ignored, so the result is an **upper bound** — useful for rejecting the design, not for confirming it.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fill_rate.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.measure_fill_rate'`

- [ ] **Step 3: Write minimal implementation**

```python
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
        "queue position is ignored, so this is an UPPER BOUND on fill rate; "
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_fill_rate.py`
Expected: PASS (6 tests)

- [ ] **Step 5: Run it against real recorded data**

Run: `.venv/bin/python -m scripts.measure_fill_rate --input data/research/books.jsonl`
Expected: a JSON summary. If `data/research/books.jsonl` does not exist, first run
`.venv/bin/python -m scripts.record_books --minutes 60 --output data/research/books.jsonl`.
Record the reported `fill_rate` in the commit message — it is the number the rest of this plan depends on.

- [ ] **Step 6: Commit**

```bash
git add bot_v2/scripts/measure_fill_rate.py bot_v2/tests/test_fill_rate.py
git commit -m "feat: estimate resting-quote fill rate from recorded books"
```

---

### Task 5: Edge gate with shadow mode

**Files:**
- Create: `bot_v2/risk/edge.py`
- Modify: `bot_v2/config/schema.py` (`RiskConfig`, `AppConfig` validator)
- Test: `bot_v2/tests/test_edge_gate.py`

**Interfaces:**
- Consumes: `models.fees.taker_fee_bps`.
- Produces: `EdgeDecision` (`APPROVE`/`ABSTAIN`), `EdgeAssessment` dataclass with `decision`, `edge_bps`, `required_bps`, `fee_bps`, `spread_bps`, `reason`; `assess_edge(...) -> EdgeAssessment`; `RiskConfig.edge_gate_mode: Literal["enforce","shadow","off"]`, `RiskConfig.safety_margin_bps: Decimal`.

- [ ] **Step 1: Write the failing test**

```python
"""Cost-aware edge gating."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from config.schema import AppConfig, Mode
from risk.edge import EdgeDecision, assess_edge


def assess(**kwargs: object):
    base: dict[str, object] = {
        "edge_bps": Decimal("120"),
        "price": Decimal("0.50"),
        "spread_bps": Decimal("200"),
        "fee_rate": Decimal("0.07"),
        "is_maker_entry": True,
        "safety_margin_bps": Decimal("50"),
    }
    base.update(kwargs)
    return assess_edge(**base)  # type: ignore[arg-type]


def test_a_maker_entry_still_pays_a_modelled_taker_exit() -> None:
    result = assess()

    # -100 (half spread earned) +350 (exit fee) +100 (exit half spread) +50
    assert result.required_bps == Decimal("400")
    assert result.decision is EdgeDecision.ABSTAIN


def test_a_taker_entry_costs_more_than_a_maker_entry() -> None:
    maker = assess(is_maker_entry=True)
    taker = assess(is_maker_entry=False)

    assert taker.required_bps > maker.required_bps
    # Taker adds its own fee plus the half spread it pays to cross.
    assert taker.required_bps - maker.required_bps == Decimal("550")


def test_a_genuinely_profitable_signal_is_approved() -> None:
    result = assess(edge_bps=Decimal("900"))
    assert result.decision is EdgeDecision.APPROVE


def test_the_assessment_carries_the_numbers_that_decided_it() -> None:
    result = assess()

    assert result.fee_bps == Decimal("350")
    assert result.spread_bps == Decimal("200")
    assert result.edge_bps == Decimal("120")
    assert "required" in result.reason


def test_cheap_extremes_require_less_edge_than_the_midpoint() -> None:
    mid = assess(price=Decimal("0.50"))
    high = assess(price=Decimal("0.90"))

    assert high.required_bps < mid.required_bps


def test_shadow_mode_is_refused_in_live_mode() -> None:
    with pytest.raises(ValidationError, match="shadow"):
        AppConfig(
            bot={"mode": Mode.LIVE},
            execution={"allow_live_trading": True, "dry_run_force": False},
            risk={"edge_gate_mode": "shadow"},
        )


def test_shadow_mode_is_allowed_in_dry_run() -> None:
    config = AppConfig(bot={"mode": Mode.DRY_RUN}, risk={"edge_gate_mode": "shadow"})
    assert config.risk.edge_gate_mode == "shadow"


def test_enforce_mode_is_the_default() -> None:
    assert AppConfig().risk.edge_gate_mode == "enforce"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_edge_gate.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'risk.edge'`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

Add to `RiskConfig` in `bot_v2/config/schema.py`:

```python
    # "enforce" blocks trades that cannot clear cost. "shadow" journals the
    # decision and routes anyway, which is the only way to gather the fill data
    # needed to calibrate the exit-cost model -- see the design doc. "off"
    # disables the gate entirely.
    edge_gate_mode: Literal["enforce", "shadow", "off"] = "enforce"
    safety_margin_bps: Decimal = Field(default=Decimal("50"), ge=Decimal("0"))
```

Add to the `AppConfig` model validator that already checks the live-mode bundle:

```python
        if (
            self.bot.mode == Mode.LIVE
            and self.execution.allow_live_trading
            and not self.execution.dry_run_force
            and self.risk.edge_gate_mode == "shadow"
        ):
            raise ValueError(
                "edge gate shadow mode is dry-run only: it routes trades the "
                "gate rejected, which in live mode is a fee-gate bypass"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_edge_gate.py`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add bot_v2/risk/edge.py bot_v2/config/schema.py bot_v2/tests/test_edge_gate.py
git commit -m "feat: gate trades on cost-clearing edge with dry-run-only shadow mode"
```

---

### Task 6: Wire the edge gate into pre-trade risk

**Files:**
- Modify: `bot_v2/risk/pretrade.py`
- Test: `bot_v2/tests/test_risk_pretrade.py`

**Interfaces:**
- Consumes: `risk.edge.assess_edge`, `risk.edge.EdgeDecision`.
- Produces: a `RiskCheckResult` named `edge_gate`. Abstains surface through the
  existing `risk_decision` journal event, whose `reason` carries the deciding
  numbers -- no new event type is needed.

- [ ] **Step 1: Write the failing test**

Append to `bot_v2/tests/test_risk_pretrade.py`:

```python
@pytest.mark.asyncio
async def test_a_signal_that_cannot_clear_cost_is_refused() -> None:
    store = ready_state_store()
    engine = PreTradeRiskEngine(
        config=AppConfig(risk={"edge_gate_mode": "enforce"}), state_store=store
    )

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    assert decision.approved is False
    assert "edge" in decision.reason


@pytest.mark.asyncio
async def test_shadow_mode_records_the_refusal_but_approves() -> None:
    store = ready_state_store()
    engine = PreTradeRiskEngine(
        config=AppConfig(risk={"edge_gate_mode": "shadow"}), state_store=store
    )

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    gate = next(c for c in decision.checks if c.check_name == "edge_gate")
    assert gate.passed is True
    assert "shadow" in gate.reason
    assert decision.approved is True


@pytest.mark.asyncio
async def test_the_gate_can_be_switched_off_entirely() -> None:
    store = ready_state_store()
    engine = PreTradeRiskEngine(
        config=AppConfig(risk={"edge_gate_mode": "off"}), state_store=store
    )

    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"),
        proposed_price=Decimal("0.46"),
    )

    gate = next(c for c in decision.checks if c.check_name == "edge_gate")
    assert gate.passed is True
    assert "disabled" in gate.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_risk_pretrade.py -k "edge or shadow or switched"`
Expected: FAIL — no check named `edge_gate`

- [ ] **Step 3: Write minimal implementation**

In `bot_v2/risk/pretrade.py`, add the check to `evaluate` after the slippage check:

```python
        checks.append(self._edge_gate_check(signal, snapshot, proposed_price))
```

and the method:

```python
    def _edge_gate_check(
        self,
        signal: TradeSignal,
        snapshot: MarketSnapshot | None,
        proposed_price: Decimal,
    ) -> RiskCheckResult:
        """
        Refuse signals whose expected edge cannot clear fees plus spread.

        Shadow mode passes the check while still reporting what enforce mode
        would have done, so the numbers accumulate without the gate starving
        the fill data needed to calibrate it.
        """

        mode = self._config.risk.edge_gate_mode
        if mode == "off":
            return RiskCheckResult(
                check_name="edge_gate", passed=True, reason="edge_gate_disabled"
            )
        if snapshot is None:
            return RiskCheckResult(
                check_name="edge_gate", passed=False, reason="market_snapshot_missing"
            )
        if snapshot.best_ask <= 0:
            return RiskCheckResult(
                check_name="edge_gate", passed=False, reason="edge_gate_book_one_sided"
            )

        spread_bps = (
            (snapshot.best_ask - snapshot.best_bid) / snapshot.best_ask
        ) * Decimal("10000")
        assessment = assess_edge(
            edge_bps=Decimal(str(signal.observed_move_bps)),
            price=proposed_price,
            spread_bps=spread_bps,
            fee_rate=self._config.execution.fee_rate,
            is_maker_entry=signal.is_maker_quote,
            safety_margin_bps=self._config.risk.safety_margin_bps,
        )
        approved = assessment.decision is EdgeDecision.APPROVE
        if mode == "shadow":
            return RiskCheckResult(
                check_name="edge_gate",
                passed=True,
                reason=f"shadow:{assessment.reason}",
            )
        return RiskCheckResult(
            check_name="edge_gate",
            passed=approved,
            reason=assessment.reason,
        )
```

with imports at the top of `pretrade.py`:

```python
from risk.edge import EdgeDecision, assess_edge
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_risk_pretrade.py`
Expected: PASS. Existing tests in this file construct `AppConfig()` whose gate defaults to `enforce`; where an existing test now fails because its synthetic signal cannot clear cost, set `risk={"edge_gate_mode": "off"}` on that test's config and add a comment saying the test predates the gate and is not about cost.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add bot_v2/risk/pretrade.py bot_v2/tests/test_risk_pretrade.py
git commit -m "feat: refuse signals that cannot clear fees and spread"
```

---

### Task 7: Maker entries

**Files:**
- Modify: `bot_v2/config/schema.py` (`SpikeStrategyConfig`)
- Modify: `bot_v2/strategies/spike.py`
- Modify: `bot_v2/config/strategies/spike.yaml`
- Test: `bot_v2/tests/test_spike_strategy.py`

**Interfaces:**
- Consumes: `models.signal.SignalType.MAKER_QUOTE`, the existing post-only order builder.
- Produces: `SpikeStrategyConfig.entry_style: Literal["taker","maker"]`, `SpikeStrategyConfig.maker_offset_ticks: int`, `SpikeStrategyConfig.quote_ttl_seconds: float`.

- [ ] **Step 1: Write the failing test**

Append to `bot_v2/tests/test_spike_strategy.py`:

```python
@pytest.mark.asyncio
async def test_maker_entry_style_emits_a_post_only_quote() -> None:
    strategy = SpikeStrategy(
        reversion_config(
            direction="momentum", entry_style="maker", sell_via_complement=False
        ),
        tick_size_provider=lambda token_id: Decimal("0.01"),
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert len(signals) == 1
    quote = signals[0]
    assert quote.signal_type is SignalType.MAKER_QUOTE
    assert quote.post_only is True
    assert quote.limit_price is not None
    assert quote.requested_size is not None


@pytest.mark.asyncio
async def test_a_maker_bid_rests_behind_the_touch() -> None:
    strategy = SpikeStrategy(
        reversion_config(
            direction="momentum", entry_style="maker",
            maker_offset_ticks=1, sell_via_complement=False,
        ),
        tick_size_provider=lambda token_id: Decimal("0.01"),
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    # Book is 0.59/0.61; a maker bid rests one tick under the bid.
    assert signals[0].limit_price == Decimal("0.58")


@pytest.mark.asyncio
async def test_taker_entry_style_is_still_available() -> None:
    strategy = SpikeStrategy(
        reversion_config(
            direction="momentum", entry_style="taker", sell_via_complement=False
        )
    )
    for mid in ("0.50", "0.50", "0.50"):
        await strategy.on_market_update(mm_snapshot(mid=mid))
    signals = await strategy.on_market_update(mm_snapshot(mid="0.60"))

    assert signals[0].signal_type is SignalType.PRICE_SPIKE
    assert signals[0].post_only is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_spike_strategy.py -k "maker or taker_entry"`
Expected: FAIL — `SpikeStrategyConfig` has no `entry_style`

- [ ] **Step 3: Write minimal implementation**

Add to `SpikeStrategyConfig` in `bot_v2/config/schema.py`:

```python
    # Maker entries rest inside the spread and pay no fee; taker entries cross
    # and pay ~350 bps at even odds. See the fee-aware execution design.
    entry_style: Literal["taker", "maker"] = "maker"
    maker_offset_ticks: int = Field(default=1, ge=0, le=20)
    quote_ttl_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    # MAKER_QUOTE signals must carry their own size; the order builder clamps
    # it to execution min/max and the live notional cap.
    maker_quote_size: Decimal = Field(default=Decimal("5"), gt=Decimal("0"))
```

In `bot_v2/strategies/spike.py`, accept a tick provider in `__init__`:

```python
        tick_size_provider: TickSizeProvider | None = None,
```

storing it as `self._tick_size_provider`, with:

```python
TickSizeProvider = Callable[[str], Decimal]
```

and a resolver mirroring the one in `market_maker.py`:

```python
    def _tick_size(self, token_id: str) -> Decimal:
        if self._tick_size_provider is None:
            return DEFAULT_TICK_SIZE
        try:
            return self._tick_size_provider(token_id)
        except Exception:
            return DEFAULT_TICK_SIZE
```

importing `from models.tick import DEFAULT_TICK_SIZE`.

Then, where the signal is built, branch on entry style. Replace the final
`signal = TradeSignal(...)` construction with:

```python
        if self._config.entry_style == "maker":
            tick = self._tick_size(snapshot.token_id)
            offset = Decimal(self._config.maker_offset_ticks) * tick
            limit = (
                snapshot.best_bid - offset
                if side == SignalSide.BUY
                else snapshot.best_ask + offset
            )
            if limit <= 0 or limit >= 1:
                return []
            self._last_signal_at[key] = self._now()
            return [
                TradeSignal(
                    strategy_name=self.name,
                    signal_type=SignalType.MAKER_QUOTE,
                    market_id=snapshot.market_id,
                    token_id=snapshot.token_id,
                    side=side,
                    reference_price=reference,
                    target_price=limit,
                    observed_move_bps=abs(move_bps),
                    created_at=self._now(),
                    reason=reason,
                    requested_size=self._config.maker_quote_size,
                    limit_price=limit,
                    post_only=True,
                )
            ]

        self._last_signal_at[key] = self._now()
        signal = TradeSignal(
```

with `SignalType` added to the module's imports from `models.signal`.

Apply the same branch to the complement path so a complement-routed entry is
also a maker quote, priced at `1 - snapshot.best_ask - offset`.

In `bot_v2/config/strategies/spike.yaml`, add:

```yaml
  # Maker entries rest inside the spread and pay zero fee. Taker entries pay
  # ~350 bps at even odds, which exceeds the measured edge several times over.
  entry_style: maker
  maker_offset_ticks: 1
  quote_ttl_seconds: 30
  maker_quote_size: 5
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_spike_strategy.py`
Expected: PASS. Existing tests in this file assert taker behaviour; add `entry_style="taker"` to `reversion_config`'s base dict so they keep testing what they describe, and let the new tests set `maker` explicitly.

- [ ] **Step 5: Wire the tick provider in bootstrap**

In `bot_v2/app/bootstrap.py`, pass the same provider the order builder uses:

```python
    strategy = SpikeStrategy(
        strategy_config,
        complement_provider=complement_token_lookup,
        tick_size_provider=(
            clob_client.get_tick_size if is_live_mode(config.bot.mode) else None
        ),
    )
```

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add bot_v2/config/schema.py bot_v2/strategies/spike.py bot_v2/config/strategies/spike.yaml bot_v2/app/bootstrap.py bot_v2/tests/test_spike_strategy.py
git commit -m "feat: rest spike entries as post-only maker quotes"
```

---

### Task 8: Maker-first exits with a mandatory taker fallback

**Files:**
- Modify: `bot_v2/config/schema.py` (`PositionManagementConfig`)
- Modify: `bot_v2/portfolio/exit_policy.py`
- Modify: `bot_v2/config/bot.yaml`
- Test: `bot_v2/tests/test_exit_policy.py`

**Interfaces:**
- Consumes: `ExitDecision`.
- Produces: `PositionManagementConfig.exit_style: Literal["taker","maker_first"]`, `PositionManagementConfig.maker_exit_deadline_seconds: float`; `ExitDecision.use_maker: bool`.

- [ ] **Step 1: Write the failing test**

Append to `bot_v2/tests/test_exit_policy.py`:

```python
def test_a_fresh_exit_rests_as_a_maker_order() -> None:
    engine = policy(
        take_profit_bps="100", exit_style="maker_first",
        maker_exit_deadline_seconds=30,
    )

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=5)),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.should_exit is True
    assert decision.use_maker is True


def test_an_exit_past_its_deadline_crosses_the_spread() -> None:
    engine = policy(
        take_profit_bps="100", exit_style="maker_first",
        maker_exit_deadline_seconds=30,
    )

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(
            opened_at=NOW - timedelta(seconds=200),
            exit_first_attempted_at=NOW - timedelta(seconds=45),
        ),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.use_maker is False


def test_market_expiry_always_crosses_regardless_of_deadline() -> None:
    """Inventory left at resolution is a coin flip on full notional."""

    engine = policy(exit_style="maker_first", maker_exit_deadline_seconds=3600)

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(
            market_end_at=NOW + timedelta(seconds=10),
            exit_first_attempted_at=NOW,
        ),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.reason == ExitReason.MARKET_EXPIRY
    assert decision.use_maker is False


def test_taker_exit_style_never_rests() -> None:
    engine = policy(take_profit_bps="100", exit_style="taker")

    decision = engine.evaluate(
        position=position(quantity="5", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=5)),
        snapshot=snapshot(best_bid="0.60", mid="0.60"),
        now=NOW,
    )

    assert decision.use_maker is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_exit_policy.py -k "maker or expiry"`
Expected: FAIL — `policy() got an unexpected keyword argument 'exit_style'`

- [ ] **Step 3: Write minimal implementation**

Add to `PositionManagementConfig` in `bot_v2/config/schema.py`:

```python
    # Maker exits rest at the target and pay no fee. The taker fallback below
    # is mandatory: inventory unexited at resolution is a coin flip on full
    # notional and trips the rotation kill switch.
    exit_style: Literal["taker", "maker_first"] = "maker_first"
    maker_exit_deadline_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
```

Add to `PositionLifecycle` in `bot_v2/models/position.py`:

```python
    exit_first_attempted_at: datetime | None = None
```

Add to `ExitDecision` in `bot_v2/portfolio/exit_policy.py`:

```python
    use_maker: bool = False
```

Add the helper and set the flag on every exiting decision:

```python
    def _use_maker(
        self,
        *,
        lifecycle: PositionLifecycle,
        reason: ExitReason,
        now: datetime,
    ) -> bool:
        """
        Rest the exit only when there is time for it to fill.

        Expiry always crosses: an unfilled resting exit at resolution leaves the
        full notional on a coin flip, which is strictly worse than paying the
        spread to be certain.
        """

        if self._config.exit_style != "maker_first":
            return False
        if reason == ExitReason.MARKET_EXPIRY:
            return False
        started = lifecycle.exit_first_attempted_at
        if started is None:
            return True
        elapsed = (now - started).total_seconds()
        return elapsed < self._config.maker_exit_deadline_seconds
```

Then pass `use_maker=self._use_maker(lifecycle=lifecycle, reason=<that reason>, now=now)` into each of the four exiting `ExitDecision(...)` constructions (`MARKET_EXPIRY`, `STOP_LOSS`, `TAKE_PROFIT`, `MAX_HOLD`).

Update the `policy()` helper in `bot_v2/tests/test_exit_policy.py` to accept and forward `exit_style` and `maker_exit_deadline_seconds`, and its `lifecycle()` helper to accept `exit_first_attempted_at`.

In `bot_v2/config/bot.yaml`, under `position_management`:

```yaml
  # Rest the exit at the target first; cross only when the deadline passes or
  # the market is about to resolve. The fallback is not optional.
  exit_style: maker_first
  maker_exit_deadline_seconds: 30
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_exit_policy.py`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider -W error::RuntimeWarning`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add bot_v2/config/schema.py bot_v2/models/position.py bot_v2/portfolio/exit_policy.py bot_v2/config/bot.yaml bot_v2/tests/test_exit_policy.py
git commit -m "feat: rest exits as maker orders before crossing the spread"
```

---

### Task 9: Verification and dry-run session

**Files:**
- Modify: `bot_v2/README.md`
- Modify: `bot_v2/docs/spike-trading.md`
- Test: `bot_v2/tests/test_config.py`

**Interfaces:**
- Consumes: everything above.
- Produces: config guard tests pinning the shipped profile.

- [ ] **Step 1: Write the failing test**

Append to `bot_v2/tests/test_config.py`:

```python
def test_shipped_config_prices_fees_correctly() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    assert config.execution.fee_rate == Decimal("0.07")
    assert config.backtest.fee_rate == Decimal("0.07")


def test_shipped_config_enters_as_maker_and_exits_maker_first() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    assert config.spike_strategy.entry_style == "maker"
    assert config.position_management.exit_style == "maker_first"


def test_shipped_edge_gate_is_enforcing() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # Shadow mode is a measurement tool, never the shipped default.
    assert config.risk.edge_gate_mode == "enforce"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_config.py -k shipped`
Expected: FAIL until `config/bot.yaml` and `config/strategies/spike.yaml` carry the values from Tasks 2, 7 and 8.

- [ ] **Step 3: Make the shipped config satisfy them**

Confirm `config/bot.yaml` contains `fee_rate: 0.07` under both `execution` and `backtest`, `exit_style: maker_first`, and `maker_exit_deadline_seconds: 30`; and that `config/strategies/spike.yaml` contains `entry_style: maker`. Add `edge_gate_mode: enforce` and `safety_margin_bps: 50` under `risk:` in `config/risk.yaml`.

- [ ] **Step 4: Run the full verification gates**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider -W error::RuntimeWarning
```

```bash
PYTHONPYCACHEPREFIX=/tmp/pm-pyc .venv/bin/python -m compileall -q app backtest clients config dashboard execution models notifications persistence portfolio risk scripts state strategies tests
```

Expected: all tests pass, compile exit 0.

- [ ] **Step 5: Run a shadow-mode dry-run session**

Set `edge_gate_mode: shadow` in `config/risk.yaml` temporarily, then:

```bash
BOT_DATA_DIR=data .venv/bin/python -m app.main
```

Let it run at least 30 minutes. Then count how often the gate would have refused:

```bash
.venv/bin/python -c "
import json, collections
c = collections.Counter()
for line in open('data/journal/events.jsonl'):
    e = json.loads(line)
    if e['event_type'] == 'risk_decision' and 'shadow' in (e.get('reason') or ''):
        c['below' if 'below' in e['reason'] else 'clears'] += 1
print(dict(c))
"
```

**Pre-registered reading:** if more than 95% of assessments are `below`, these markets carry no signal that clears cost. That is an answer about the markets, not a prompt to loosen the gate. Restore `edge_gate_mode: enforce` afterwards.

- [ ] **Step 6: Update the docs**

In `bot_v2/docs/spike-trading.md`, add a "Costs" section stating the fee formula, the 350 bps figure at even odds, and that entries are now maker by default. In `bot_v2/README.md`, add `models/fees.py` and `risk/edge.py` to the project layout and `scripts/measure_fill_rate.py` to the strategy-research section.

- [ ] **Step 7: Commit**

```bash
git add bot_v2/config/ bot_v2/tests/test_config.py bot_v2/README.md bot_v2/docs/spike-trading.md
git commit -m "feat: ship fee-aware maker-first execution profile"
```

---

## After this plan

Live arming is unchanged: preflight, a Telegram test delivered within five
minutes, and the exact `START LIVE` confirmation, all performed by the operator.

The first live session's job is to answer two questions that no amount of
offline work can settle:

1. Does a post-only order now complete cleanly on the real CLOB? The record
   stands at 0 of 110, and the tick-size, signing-options and post-only fixes
   are believed to address it but are unproven.
2. What do fills actually cost, and does the maker side behave as modelled?

Both are answerable at the $2 per-order cap. Neither requires the TWAP work,
which stays gated behind the Path B spike in the design doc.
