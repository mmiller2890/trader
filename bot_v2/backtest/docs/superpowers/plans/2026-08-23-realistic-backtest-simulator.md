# Realistic Backtest Simulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade the existing offline backtest from immediate top-of-book fills to a deterministic paper exchange that reconstructs historical books, consumes depth, produces partial fills, charges fees, and tracks cash, collateral, positions, and equity.

**Architecture:** Historical JSON is normalized into full-book and price-level-delta events. A per-token `OrderBookState` reconstructs books and supplies both strategy snapshots and depth-aware execution quotes. A separate `PortfolioLedger` is the source of truth for cash, collateral, fees, positions, and equity; `BacktestEngine` coordinates strategy, risk, matching, book mutation, and ledger updates without wiring any live client.

**Tech Stack:** Python 3.11+, asyncio, `Decimal`, dataclasses, Pydantic v2, pytest, pytest-asyncio.

**Spec:** This plan is self-contained; the Requirements and Accounting Invariants sections below are the approved design specification.

## Global Constraints

- Build on the current uncommitted backtest work. Do not reset, checkout, clean, or discard the dirty worktree.
- Keep the backtest fully offline. It must never instantiate `WebSocketManager`, `ClobClientAdapter`, `OrderSubmitter`, or make network calls.
- Preserve the current `BacktestEngine.run(strategy=..., snapshots=...)` and `ReplayEngine()` entry points.
- Preserve the current legacy JSON snapshot format while adding a richer event format.
- Use `Decimal` for prices, sizes, fees, cash, collateral, equity, and P&L. Never convert financial values through binary `float`.
- Process events deterministically by `(received_ts, source_ts, sequence_id)` without mutating the caller's input list.
- Use the existing `PreTradeRiskEngine`, but evaluate the size that can actually fill and the depth-weighted execution price.
- Approved buys consume asks from lowest price upward; approved sells consume bids from highest price downward.
- The backtest limit is derived from the current best quote and `execution.max_slippage_bps`.
- `FOK` orders either fill completely or do not mutate the book or portfolio. `IOC` and `GTC` may partially fill; this version does not model resting queue priority, so all remainder is reported as unfilled.
- Default starting cash is `1000` USDC and default taker fee is `10` basis points. Both must be configurable.
- Preserve signed positions for compatibility. A negative position is a synthetic short and must reserve `1 USDC` per short share, representing maximum prediction-market payout liability.
- Fees reduce cash and net P&L. Existing position `realized_pnl` and `unrealized_pnl` remain gross of fees.
- After every event, equity must satisfy `equity == cash + sum(position.quantity * last_mark_price)`.
- `net_pnl == equity - starting_cash`; `gross_pnl == realized_pnl + unrealized_pnl`; `net_pnl == gross_pnl - fees_paid` when every position has a current mark.
- Follow test-driven development: write one focused failing test, run it and confirm the expected failure, then write the minimum production code.
- Run tests with `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` to avoid adding generated files to the dirty worktree.
- The repository currently has an unrelated untracked `../.DS_Store`; leave it untouched.

## Approved Requirements

1. Accept two input forms:
   - the existing array of serialized `MarketSnapshot` values;
   - an array of `book_snapshot` and `book_delta` events containing all price levels.
2. Reconstruct one order book per `(market_id, token_id)`.
3. Replace a book on a full snapshot. Apply a delta by upserting non-zero levels and deleting zero-size levels.
4. Reject crossed books, stale/out-of-order sequence numbers, and—by default—sequence gaps.
5. Do not call the strategy until both sides of a valid book exist.
6. Quote execution without mutating the book. Mutate depth only after capital and risk checks approve the quote.
7. Report individual fills, VWAP, fees, filled size, unfilled remainder, and total executable liquidity inside the limit.
8. Reject insufficiently funded trades without mutating the book, ledger, or position state.
9. Track cash, reserved short collateral, available cash, gross position value, gross P&L, fees, net P&L, equity, fill rate, and maximum drawdown.
10. Serialize all new reports through the CLI while retaining existing output fields.
11. Keep the existing live and dry-run execution behavior unchanged.

## Accounting Invariants

For a fill with `notional = price * size` and `fee = notional * fee_bps / 10000`:

- Buy: `cash_after = cash_before - notional - fee`.
- Sell: `cash_after = cash_before + notional - fee`.
- Long mark value: positive `quantity * mark_price`.
- Short mark value: negative `quantity * mark_price` because quantity is negative.
- Short reserve: `abs(min(quantity, 0)) * max_payout_per_share` for every position.
- Available cash: `cash - reserved_cash`.
- A proposed fill is fundable only when projected `cash >= projected_reserved_cash`.
- A buy at `0.51` marked at `0.50` immediately loses `0.01 * size + fees` in net equity.
- A synthetic short sale at `0.55` marked at `0.56` immediately loses `0.01 * size + fees` in net equity.

## Current Baseline

Before editing, inspect these current uncommitted files:

- `backtest/replay.py`: deterministic clock, risk integration, immediate top-of-book fills, signed-position accounting, equity points.
- `backtest/metrics.py`: signal/order counts and gross P&L.
- `backtest/cli.py`: legacy snapshot JSON loader and JSON output.
- `backtest/test_replay.py`: 17-test suite coverage when combined with `tests/`.
- `risk/pretrade.py`: injectable historical clock.
- `strategies/spike.py`: injectable historical clock.
- `pyproject.toml`: explicit package discovery, YAML package data, and `testpaths = ["tests", "backtest"]`.

Baseline verification:

```bash
cd /Users/ghost/Projects/trader/bot_v2
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

Expected before this plan is implemented: `17 passed`.

If dependencies are unavailable, create a temporary environment outside the repository:

```bash
python3 -m venv /private/tmp/bot_v2_backtest_venv
/private/tmp/bot_v2_backtest_venv/bin/python -m pip install -e ".[dev]"
```

## Target File Map

- Create `backtest/models.py`: historical event, fill, execution-report, portfolio-snapshot, and expanded equity-point contracts.
- Create `backtest/orderbook.py`: deterministic book reconstruction, sequence validation, snapshot generation, quote generation, and committed depth reduction.
- Create `backtest/portfolio.py`: cash/collateral/position accounting and marking.
- Modify `config/schema.py`: add `BacktestConfig` and `AppConfig.backtest`.
- Modify `config/bot.yaml`: provide explicit backtest defaults.
- Modify `models/order.py`: add `OrderStatus.PARTIALLY_FILLED` only; do not add backtest-only fields to live order models.
- Modify `risk/policy.py` and `risk/pretrade.py`: accept an optional executable-liquidity override while preserving all existing callers.
- Modify `backtest/replay.py`: orchestrate normalized events, order-book matching, risk, ledger, and compatibility wrappers.
- Modify `backtest/metrics.py`: compute capital-aware metrics from portfolio snapshots and execution reports.
- Modify `backtest/cli.py`: parse both input formats and serialize new reports.
- Modify `backtest/example_snapshots.json`: retain this legacy compatibility fixture.
- Create `backtest/example_orderbook_events.json`: document the richer snapshot/delta input.
- Split or extend tests under `backtest/` as described in each task.
- Modify `README.md`: document configuration, data format, accounting, and modeling limitations.

## Shared Test Fixture Contract

Keep reusable factories in `backtest/conftest.py` so Tasks 2, 3, 5, and 6 do not depend on undefined pseudo-helpers. Create the file during Task 2 and extend it during Task 3. Use these exact imports and base factories:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backtest.models import (
    BookDeltaEvent,
    BookSnapshotEvent,
    ExecutionReport,
    ExecutionStatus,
    SimulatedFill,
)
from backtest.orderbook import OrderBookState
from models.market import MarketSnapshot, OrderBookLevel
from models.order import OrderRequest, OrderSide, OrderTimeInForce


NOW = datetime(2025, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(seconds=1)


def levels(values: list[tuple[str, str]]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=Decimal(price), size=Decimal(size)) for price, size in values]


def snapshot_event(
    *,
    sequence: int,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    at: datetime = NOW,
) -> BookSnapshotEvent:
    return BookSnapshotEvent(
        market_id="m1",
        token_id="t1",
        bids=levels([("0.49", "8"), ("0.50", "4")] if bids is None else bids),
        asks=levels([("0.52", "6")] if asks is None else asks),
        sequence_id=sequence,
        source_ts=at,
        received_ts=at,
    )


def delta_event(
    *,
    sequence: int,
    bid_updates: list[tuple[str, str]] | None = None,
    ask_updates: list[tuple[str, str]] | None = None,
    at: datetime = LATER,
) -> BookDeltaEvent:
    return BookDeltaEvent(
        market_id="m1",
        token_id="t1",
        bid_updates=levels(bid_updates or []),
        ask_updates=levels(ask_updates or []),
        sequence_id=sequence,
        source_ts=at,
        received_ts=at,
    )


def seeded_book(*, sequence: int, reject_sequence_gaps: bool = True) -> OrderBookState:
    book = OrderBookState("m1", "t1", reject_sequence_gaps=reject_sequence_gaps)
    book.apply_snapshot(snapshot_event(sequence=sequence))
    return book


def book_with_asks(values: list[tuple[str, str]]) -> OrderBookState:
    book = OrderBookState("m1", "t1")
    book.apply_snapshot(snapshot_event(sequence=1, bids=[("0.49", "100")], asks=values))
    return book


def buy_order(*, price: str, size: str, tif: str = "IOC") -> OrderRequest:
    return OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        price=Decimal(price),
        size=Decimal(size),
        time_in_force=OrderTimeInForce(tif),
        strategy_name="test",
        created_at=NOW,
    )


def market_snapshot(*, mid: str, at: datetime = NOW) -> MarketSnapshot:
    price = Decimal(mid)
    return MarketSnapshot(
        market_id="m1",
        token_id="t1",
        best_bid=price,
        best_ask=price,
        mid_price=price,
        top_bid_size=Decimal("100"),
        top_ask_size=Decimal("100"),
        source_ts=at,
        received_ts=at,
    )
```

During Task 3, add one report factory and thin named wrappers:

```python
def fill_report(
    *,
    side: OrderSide,
    requested: str,
    filled: str,
    price: str,
    fee_bps: str = "0",
) -> ExecutionReport:
    requested_size = Decimal(requested)
    filled_size = Decimal(filled)
    fill_price = Decimal(price)
    notional = fill_price * filled_size
    fee = notional * Decimal(fee_bps) / Decimal("10000")
    order = OrderRequest(
        client_order_id="test-order-0001",
        market_id="m1",
        token_id="t1",
        side=side,
        price=fill_price,
        size=requested_size,
        time_in_force=OrderTimeInForce.IOC,
        strategy_name="test",
        created_at=NOW,
    )
    fills = (
        [SimulatedFill(price=fill_price, size=filled_size, notional=notional, fee=fee)]
        if filled_size > 0
        else []
    )
    status = (
        ExecutionStatus.FILLED
        if filled_size == requested_size
        else ExecutionStatus.PARTIAL
        if filled_size > 0
        else ExecutionStatus.UNFILLED
    )
    return ExecutionReport(
        order=order,
        status=status,
        fills=fills,
        requested_size=requested_size,
        filled_size=filled_size,
        remaining_size=requested_size - filled_size,
        executable_liquidity=filled_size,
        average_fill_price=fill_price if filled_size > 0 else None,
        total_notional=notional,
        total_fees=fee,
        reason=status.value,
    )


def filled_buy(*, size: str, price: str, fee_bps: str = "0") -> ExecutionReport:
    return fill_report(side=OrderSide.BUY, requested=size, filled=size, price=price, fee_bps=fee_bps)


def filled_sell(*, size: str, price: str, fee_bps: str = "0") -> ExecutionReport:
    return fill_report(side=OrderSide.SELL, requested=size, filled=size, price=price, fee_bps=fee_bps)


def partial_buy(*, requested: str, filled: str, price: str) -> ExecutionReport:
    return fill_report(side=OrderSide.BUY, requested=requested, filled=filled, price=price)
```

Tests that use these names must explicitly import them from `backtest.conftest`. Where the tests below say `snapshot(mid=...)`, import `market_snapshot as snapshot`. Define `deep_snapshot_event()` as `snapshot_event(sequence=1, bids=[("0.49", "100")], asks=[("0.50", "100")])`. Keep the existing `BuyOnceStrategy` in `backtest/test_replay.py`; for repeat-run tests create a fresh strategy instance for each run because strategy lifecycle state is not owned by the engine.

---

### Task 1: Configuration and Backtest Data Contracts

**Files:**
- Create: `backtest/models.py`
- Create: `backtest/test_models.py`
- Modify: `config/schema.py:74-176`
- Modify: `config/bot.yaml:20-30`
- Modify: `models/order.py:33-42`

**Interfaces:**
- Produces `BacktestConfig(starting_cash, taker_fee_bps, allow_short_positions, reject_sequence_gaps, max_payout_per_share)`.
- Produces discriminated historical event models `BookSnapshotEvent` and `BookDeltaEvent`.
- Produces `SimulatedFill`, `ExecutionReport`, `PortfolioSnapshot`, and the `ExecutionStatus` enum.
- Adds the backward-compatible enum value `OrderStatus.PARTIALLY_FILLED`.

- [ ] **Step 1: Write failing configuration and model tests**

Create `backtest/test_models.py` with these focused tests:

```python
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from backtest.models import (
    BookDeltaEvent,
    BookSnapshotEvent,
    HistoricalBookEvent,
)
from config.schema import AppConfig
from models.market import OrderBookLevel


NOW = datetime(2025, 1, 1, tzinfo=UTC)


def test_backtest_config_has_conservative_defaults() -> None:
    config = AppConfig()
    assert config.backtest.starting_cash == Decimal("1000")
    assert config.backtest.taker_fee_bps == Decimal("10")
    assert config.backtest.allow_short_positions is True
    assert config.backtest.reject_sequence_gaps is True
    assert config.backtest.max_payout_per_share == Decimal("1")


def test_historical_event_union_uses_event_type_discriminator() -> None:
    payload = {
        "event_type": "book_snapshot",
        "market_id": "m1",
        "token_id": "t1",
        "bids": [{"price": "0.49", "size": "5"}],
        "asks": [{"price": "0.51", "size": "7"}],
        "sequence_id": 10,
        "source_ts": NOW.isoformat(),
        "received_ts": NOW.isoformat(),
    }
    event = TypeAdapter(HistoricalBookEvent).validate_python(payload)
    assert isinstance(event, BookSnapshotEvent)


def test_delta_accepts_zero_size_as_level_deletion() -> None:
    event = BookDeltaEvent(
        market_id="m1",
        token_id="t1",
        bid_updates=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("0"))],
        ask_updates=[],
        sequence_id=11,
        source_ts=NOW,
        received_ts=NOW,
    )
    assert event.bid_updates[0].size == 0


def test_backtest_fee_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        AppConfig(backtest={"taker_fee_bps": "-1"})
```

- [ ] **Step 2: Run the tests and verify the expected import failure**

```bash
cd /Users/ghost/Projects/trader/bot_v2
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_models.py
```

Expected: collection fails because `backtest.models` and `AppConfig.backtest` do not exist.

- [ ] **Step 3: Add exact configuration fields**

Add to `config/schema.py` immediately before `RiskConfig`:

```python
class BacktestConfig(BaseModel):
    """Deterministic paper-exchange settings."""

    model_config = ConfigDict(extra="forbid")

    starting_cash: Decimal = Field(default=Decimal("1000"), gt=Decimal("0"))
    taker_fee_bps: Decimal = Field(default=Decimal("10"), ge=Decimal("0"), le=Decimal("1000"))
    allow_short_positions: bool = True
    reject_sequence_gaps: bool = True
    max_payout_per_share: Decimal = Field(default=Decimal("1"), gt=Decimal("0"), le=Decimal("1"))
```

Add to `AppConfig`:

```python
backtest: BacktestConfig = Field(default_factory=BacktestConfig)
```

Add to `config/bot.yaml`:

```yaml
backtest:
  starting_cash: 1000
  taker_fee_bps: 10
  allow_short_positions: true
  reject_sequence_gaps: true
  max_payout_per_share: 1
```

Add `PARTIALLY_FILLED = "partially_filled"` to `OrderStatus` without changing any existing value.

- [ ] **Step 4: Implement the historical and execution contracts**

Create `backtest/models.py`. Use Pydantic models so CLI serialization and validation stay consistent:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from models.market import OrderBookLevel
from models.order import OrderRequest
from models.position import Position


class _HistoricalEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    sequence_id: int = Field(ge=0)
    source_ts: datetime
    received_ts: datetime


class BookSnapshotEvent(_HistoricalEventBase):
    event_type: Literal["book_snapshot"] = "book_snapshot"
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]


class BookDeltaEvent(_HistoricalEventBase):
    event_type: Literal["book_delta"] = "book_delta"
    bid_updates: list[OrderBookLevel] = Field(default_factory=list)
    ask_updates: list[OrderBookLevel] = Field(default_factory=list)


HistoricalBookEvent = Annotated[
    BookSnapshotEvent | BookDeltaEvent,
    Field(discriminator="event_type"),
]


class ExecutionStatus(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"
    UNFILLED = "unfilled"
    REJECTED = "rejected"


class SimulatedFill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: Decimal = Field(gt=Decimal("0"), le=Decimal("1"))
    size: Decimal = Field(gt=Decimal("0"))
    notional: Decimal = Field(gt=Decimal("0"))
    fee: Decimal = Field(ge=Decimal("0"))


class ExecutionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: OrderRequest
    status: ExecutionStatus
    fills: list[SimulatedFill] = Field(default_factory=list)
    requested_size: Decimal = Field(gt=Decimal("0"))
    filled_size: Decimal = Field(ge=Decimal("0"))
    remaining_size: Decimal = Field(ge=Decimal("0"))
    executable_liquidity: Decimal = Field(ge=Decimal("0"))
    average_fill_price: Decimal | None = Field(default=None, gt=Decimal("0"), le=Decimal("1"))
    total_notional: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    total_fees: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    reason: str = Field(min_length=1)


class PortfolioSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: datetime
    cash: Decimal
    reserved_cash: Decimal = Field(ge=Decimal("0"))
    available_cash: Decimal
    position_value: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    gross_pnl: Decimal
    fees_paid: Decimal = Field(ge=Decimal("0"))
    net_pnl: Decimal
    positions: list[Position] = Field(default_factory=list)
```

- [ ] **Step 5: Run model and full regression tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_models.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

Expected: model tests pass and all existing tests remain green.

- [ ] **Step 6: Create a checkpoint commit if commits are authorized**

The worktree already contains approved uncommitted backtest changes. Do not commit unless the user explicitly authorizes it. If authorized:

```bash
git -C /Users/ghost/Projects/trader add bot_v2/config/schema.py bot_v2/config/bot.yaml bot_v2/models/order.py bot_v2/backtest/models.py bot_v2/backtest/test_models.py
git -C /Users/ghost/Projects/trader commit -m "feat(backtest): define event and execution contracts"
```

---

### Task 2: Deterministic Order-Book Reconstruction

**Files:**
- Create: `backtest/conftest.py`
- Create: `backtest/orderbook.py`
- Create: `backtest/test_orderbook.py`
- Consume: `backtest/models.py`

**Interfaces:**
- Produces `OrderBookState(market_id, token_id, reject_sequence_gaps=True)`.
- Produces `apply_snapshot(event)`, `apply_delta(event)`, `to_market_snapshot()`, `quote(order, max_slippage_bps, fee_bps)`, and `commit(report)`.
- `quote` is pure with respect to book depth; `commit` is the only execution method that reduces levels.

- [ ] **Step 1: Write failing reconstruction tests**

Import the snapshot/delta helpers from the Shared Test Fixture Contract, then add these tests:

```python
def test_snapshot_replaces_book_and_selects_true_best_levels() -> None:
    book = OrderBookState("m1", "t1")
    book.apply_snapshot(snapshot_event(
        sequence=10,
        bids=[("0.48", "10"), ("0.50", "4"), ("0.49", "8")],
        asks=[("0.54", "9"), ("0.52", "6"), ("0.53", "7")],
    ))
    current = book.to_market_snapshot()
    assert current is not None
    assert current.best_bid == Decimal("0.50")
    assert current.best_ask == Decimal("0.52")
    assert current.top_bid_size == Decimal("4")
    assert current.top_ask_size == Decimal("6")


def test_delta_upserts_and_deletes_levels() -> None:
    book = seeded_book(sequence=10)
    book.apply_delta(delta_event(
        sequence=11,
        bid_updates=[("0.50", "0"), ("0.495", "12")],
        ask_updates=[("0.52", "3")],
    ))
    assert book.bids == {Decimal("0.49"): Decimal("8"), Decimal("0.495"): Decimal("12")}
    assert book.asks[Decimal("0.52")] == Decimal("3")


def test_out_of_order_and_gapped_deltas_are_rejected() -> None:
    book = seeded_book(sequence=10, reject_sequence_gaps=True)
    with pytest.raises(ValueError, match="sequence gap"):
        book.apply_delta(delta_event(sequence=12))
    with pytest.raises(ValueError, match="out of order"):
        book.apply_delta(delta_event(sequence=9))


def test_crossed_book_is_rejected_without_changing_previous_state() -> None:
    book = seeded_book(sequence=10)
    before = (book.bids.copy(), book.asks.copy(), book.sequence_id)
    with pytest.raises(ValueError, match="crossed book"):
        book.apply_delta(delta_event(sequence=11, bid_updates=[("0.60", "1")]))
    assert (book.bids, book.asks, book.sequence_id) == before
```

- [ ] **Step 2: Verify red**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_orderbook.py -k "snapshot or delta or sequence or crossed"
```

Expected: import failure because `OrderBookState` does not exist.

- [ ] **Step 3: Implement reconstruction atomically**

Create `backtest/orderbook.py` with this public shape:

```python
class OrderBookState:
    def __init__(self, market_id: str, token_id: str, *, reject_sequence_gaps: bool = True) -> None:
        self.market_id = market_id
        self.token_id = token_id
        self.reject_sequence_gaps = reject_sequence_gaps
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.sequence_id: int | None = None
        self.source_ts: datetime | None = None
        self.received_ts: datetime | None = None

    def apply_snapshot(self, event: BookSnapshotEvent) -> None: ...
    def apply_delta(self, event: BookDeltaEvent) -> None: ...
    def to_market_snapshot(self) -> MarketSnapshot | None: ...
```

Implementation rules:

1. Validate event IDs match this book.
2. Build candidate `dict` copies before mutation.
3. Drop zero-size levels.
4. Reject negative sizes through Pydantic validation.
5. If a sequence already exists, reject a snapshot or delta with `sequence_id <= current sequence_id`.
6. A newer full snapshot is an authoritative resynchronization and need not be contiguous. For deltas only, if gap rejection is enabled, require `sequence_id == current + 1`.
7. Validate `max(candidate_bids) <= min(candidate_asks)` when both sides exist.
8. Commit candidates and timestamps only after all checks pass.
9. `to_market_snapshot()` returns `None` if either side is empty; otherwise it uses `max(bids)` and `min(asks)` explicitly.

- [ ] **Step 4: Implement pure depth quoting and committed consumption**

Add to `OrderBookState`:

```python
def quote(
    self,
    order: OrderRequest,
    *,
    max_slippage_bps: Decimal,
    fee_bps: Decimal,
) -> ExecutionReport: ...

def commit(self, report: ExecutionReport) -> None: ...
```

Quoting algorithm:

```python
slippage = max_slippage_bps / Decimal("10000")
if order.side == OrderSide.BUY:
    levels = sorted(self.asks.items())
    limit = min(Decimal("1"), order.price * (Decimal("1") + slippage))
    eligible = ((price, size) for price, size in levels if price <= limit)
else:
    levels = sorted(self.bids.items(), reverse=True)
    limit = max(Decimal("0"), order.price * (Decimal("1") - slippage))
    eligible = ((price, size) for price, size in levels if price >= limit)
```

Materialize all eligible levels and set `executable_liquidity` to the sum of their sizes, even when it exceeds the requested size. Then fill `min(remaining, level_size)` until the request is satisfied. Calculate per-fill notional and fee with Decimal arithmetic. For `FOK`, if total quoted size is less than requested size, discard all quoted fills and return `UNFILLED` while preserving the computed `executable_liquidity`. For `IOC` and `GTC`, return `PARTIAL` when `0 < filled < requested`; this version explicitly cancels the remainder rather than resting it.

`commit(report)` must verify report IDs and subtract only recorded fill sizes from the appropriate side. Remove a price level when its remaining size is zero. Raise without mutation if a report attempts to consume unavailable depth.

- [ ] **Step 5: Add depth/VWAP/fee/partial/FOK tests**

```python
def test_buy_quote_walks_asks_and_calculates_vwap_and_fees() -> None:
    book = book_with_asks([("0.50", "2"), ("0.51", "3"), ("0.55", "10")])
    order = buy_order(price="0.50", size="5", tif="IOC")
    report = book.quote(order, max_slippage_bps=Decimal("300"), fee_bps=Decimal("10"))
    assert [fill.size for fill in report.fills] == [Decimal("2"), Decimal("3")]
    assert report.total_notional == Decimal("2.53")
    assert report.average_fill_price == Decimal("0.506")
    assert report.total_fees == Decimal("0.00253")
    assert report.executable_liquidity == Decimal("15")
    assert report.status == ExecutionStatus.FILLED


def test_partial_quote_does_not_mutate_until_commit() -> None:
    book = book_with_asks([("0.50", "2")])
    report = book.quote(buy_order(price="0.50", size="5", tif="IOC"), max_slippage_bps=Decimal("0"), fee_bps=Decimal("0"))
    assert report.status == ExecutionStatus.PARTIAL
    assert report.filled_size == Decimal("2")
    assert book.asks[Decimal("0.50")] == Decimal("2")
    book.commit(report)
    assert Decimal("0.50") not in book.asks


def test_fok_insufficient_depth_has_no_fills_and_cannot_consume_book() -> None:
    book = book_with_asks([("0.50", "2")])
    report = book.quote(buy_order(price="0.50", size="5", tif="FOK"), max_slippage_bps=Decimal("0"), fee_bps=Decimal("0"))
    assert report.status == ExecutionStatus.UNFILLED
    assert report.fills == []
    assert book.asks[Decimal("0.50")] == Decimal("2")
```

- [ ] **Step 6: Run order-book and full tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_orderbook.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

- [ ] **Step 7: Create a checkpoint commit if authorized**

```bash
git -C /Users/ghost/Projects/trader add bot_v2/backtest/conftest.py bot_v2/backtest/orderbook.py bot_v2/backtest/test_orderbook.py
git -C /Users/ghost/Projects/trader commit -m "feat(backtest): reconstruct and consume historical books"
```

---

### Task 3: Capital, Collateral, Position, and Fee Ledger

**Files:**
- Create: `backtest/portfolio.py`
- Create: `backtest/test_portfolio.py`
- Consume: `backtest/models.py`

**Interfaces:**
- Produces `PortfolioLedger(config: BacktestConfig)`.
- Produces `can_apply(report) -> tuple[bool, str]`, `apply(report, timestamp) -> Position`, `mark(snapshot)`, and `snapshot(timestamp) -> PortfolioSnapshot`.
- The ledger owns positions and last marks. The engine mirrors updated positions into `InMemoryStateStore` for existing risk checks.

- [ ] **Step 1: Write failing ledger tests**

```python
def test_buy_reduces_cash_by_notional_and_fee() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="10"))
    report = filled_buy(size="5", price="0.50", fee_bps="10")
    allowed, reason = ledger.can_apply(report)
    assert (allowed, reason) == (True, "funded")
    ledger.apply(report, NOW)
    ledger.mark(snapshot(mid="0.50", at=NOW))
    state = ledger.snapshot(NOW)
    assert state.cash == Decimal("97.4975")
    assert state.position_value == Decimal("2.50")
    assert state.equity == Decimal("99.9975")
    assert state.fees_paid == Decimal("0.0025")
    assert state.net_pnl == Decimal("-0.0025")


def test_partial_fill_only_books_executed_size() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="0"))
    report = partial_buy(requested="10", filled="3", price="0.50")
    ledger.apply(report, NOW)
    assert ledger.positions[("m1", "t1")].quantity == Decimal("3")
    assert ledger.cash == Decimal("98.50")


def test_synthetic_short_reserves_full_payout_liability() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="10", taker_fee_bps="0", allow_short_positions=True))
    report = filled_sell(size="5", price="0.60", fee_bps="0")
    assert ledger.can_apply(report) == (True, "funded")
    ledger.apply(report, NOW)
    state = ledger.snapshot(NOW)
    assert state.cash == Decimal("13")
    assert state.reserved_cash == Decimal("5")
    assert state.available_cash == Decimal("8")


def test_insufficient_short_collateral_is_rejected_without_mutation() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="1", taker_fee_bps="0", allow_short_positions=True))
    report = filled_sell(size="5", price="0.10", fee_bps="0")
    assert ledger.can_apply(report) == (False, "insufficient_short_collateral")
    assert ledger.cash == Decimal("1")
    assert ledger.positions == {}


def test_short_is_rejected_when_disabled() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", allow_short_positions=False))
    assert ledger.can_apply(filled_sell(size="1", price="0.50")) == (False, "short_positions_disabled")
```

- [ ] **Step 2: Verify red**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_portfolio.py
```

Expected: import failure because `PortfolioLedger` does not exist.

- [ ] **Step 3: Implement a pure projected-state check before mutation**

Create `backtest/portfolio.py` with:

```python
class PortfolioLedger:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.starting_cash = config.starting_cash
        self.cash = config.starting_cash
        self.total_fees = Decimal("0")
        self.positions: dict[tuple[str, str], Position] = {}
        self.marks: dict[tuple[str, str], Decimal] = {}

    def can_apply(self, report: ExecutionReport) -> tuple[bool, str]: ...
    def apply(self, report: ExecutionReport, timestamp: datetime) -> Position: ...
    def mark(self, snapshot: MarketSnapshot) -> None: ...
    def snapshot(self, timestamp: datetime) -> PortfolioSnapshot: ...
```

Factor position arithmetic into one private pure function used by both `can_apply` and `apply`:

```python
def _project_position(existing: Position | None, report: ExecutionReport, timestamp: datetime) -> Position:
    # Use report.filled_size and report.average_fill_price.
    # Preserve the current weighted-entry and realized-PnL rules from backtest/replay.py.
    # A zero resulting quantity must have average_entry_price Decimal("0").
```

`can_apply` must calculate projected cash, all projected positions, and projected short reserve without mutating instance fields. Return exact stable reasons: `funded`, `no_fills`, `short_positions_disabled`, `insufficient_cash`, or `insufficient_short_collateral`.

`apply` must call `can_apply` again, raise `ValueError(reason)` when rejected, then atomically update cash, fees, position, and mark at the fill VWAP.

`mark` updates the matching position's mark price and gross unrealized P&L.

`snapshot` computes:

```python
reserved = sum(abs(min(position.quantity, Decimal("0"))) * config.max_payout_per_share for position in positions)
position_value = sum(position.quantity * marks.get(key, position.average_entry_price) for key, position in positions.items())
realized = sum(position.realized_pnl for position in positions.values())
unrealized = sum(position.unrealized_pnl for position in positions.values())
equity = cash + position_value
net_pnl = equity - starting_cash
```

- [ ] **Step 4: Add closing and reversal accounting tests**

Test a long full close, short full close, partial reduction, and reversal. Use exact expected Decimal values. At minimum:

```python
def test_long_round_trip_realizes_gross_pnl_and_net_pnl_includes_fees() -> None:
    ledger = PortfolioLedger(BacktestConfig(starting_cash="100", taker_fee_bps="10"))
    ledger.apply(filled_buy(size="5", price="0.50", fee_bps="10"), NOW)
    ledger.apply(filled_sell(size="5", price="0.60", fee_bps="10"), LATER)
    state = ledger.snapshot(LATER)
    assert state.realized_pnl == Decimal("0.50")
    assert state.fees_paid == Decimal("0.0055")
    assert state.net_pnl == Decimal("0.4945")
    assert state.positions[0].quantity == Decimal("0")
```

- [ ] **Step 5: Run ledger and full tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_portfolio.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

- [ ] **Step 6: Create a checkpoint commit if authorized**

```bash
git -C /Users/ghost/Projects/trader add bot_v2/backtest/conftest.py bot_v2/backtest/portfolio.py bot_v2/backtest/test_portfolio.py
git -C /Users/ghost/Projects/trader commit -m "feat(backtest): account for capital collateral and fees"
```

---

### Task 4: Risk Interface for Executable Depth

**Files:**
- Modify: `risk/policy.py:13-24`
- Modify: `risk/pretrade.py:35-61,177-195`
- Modify: `tests/test_risk_pretrade.py`

**Interfaces:**
- Extends `PreTradeRiskPolicy.evaluate(..., executable_liquidity: Decimal | None = None)`.
- Existing live/dry-run callers omit the argument and retain current top-of-book behavior.
- Backtest passes the quote's total `executable_liquidity`, so risk sees all eligible depth without pretending the top level contains it or confusing requested size with market depth.

- [ ] **Step 1: Write a failing executable-liquidity risk test**

Append to `tests/test_risk_pretrade.py`:

```python
@pytest.mark.asyncio
async def test_pretrade_can_evaluate_depth_liquidity_override() -> None:
    config = AppConfig(risk={"min_top_of_book_liquidity": "2"})
    state = InMemoryStateStore(mode=Mode.BACKTEST)
    engine = PreTradeRiskEngine(config=config, state_store=state)
    item = fresh_snapshot().model_copy(update={"top_ask_size": Decimal("1")})
    decision = await engine.evaluate(
        signal=make_signal(),
        snapshot=item,
        proposed_size=Decimal("3"),
        proposed_price=Decimal("0.46"),
        executable_liquidity=Decimal("3"),
    )
    assert next(check for check in decision.checks if check.check_name == "top_of_book_liquidity").passed
```

- [ ] **Step 2: Verify red**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_risk_pretrade.py::test_pretrade_can_evaluate_depth_liquidity_override
```

Expected: `TypeError` for the unexpected keyword.

- [ ] **Step 3: Add the optional argument without changing defaults**

Update the protocol and concrete method signatures:

```python
async def evaluate(
    self,
    *,
    signal: TradeSignal,
    snapshot: MarketSnapshot | None,
    proposed_size: Decimal,
    proposed_price: Decimal,
    executable_liquidity: Decimal | None = None,
) -> RiskDecision:
```

Pass the override into `_top_of_book_liquidity_check`. Inside that method:

```python
available = executable_liquidity
if available is None:
    available = snapshot.top_ask_size if signal.side.value == "buy" else snapshot.top_bid_size
minimum = max(self._config.risk.min_top_of_book_liquidity, proposed_size)
```

Do not rename the check or change any other existing risk behavior.

- [ ] **Step 4: Run risk and full regression tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_risk_pretrade.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

- [ ] **Step 5: Create a checkpoint commit if authorized**

```bash
git -C /Users/ghost/Projects/trader add bot_v2/risk/policy.py bot_v2/risk/pretrade.py bot_v2/tests/test_risk_pretrade.py
git -C /Users/ghost/Projects/trader commit -m "feat(risk): evaluate executable depth in backtests"
```

---

### Task 5: Integrate Paper Matching and Portfolio Accounting

**Files:**
- Modify: `backtest/replay.py`
- Modify: `backtest/metrics.py`
- Modify: `backtest/test_replay.py`
- Consume: `backtest/models.py`, `backtest/orderbook.py`, `backtest/portfolio.py`

**Interfaces:**
- Adds `BacktestEngine.run_events(strategy, events) -> ReplayResult`.
- Preserves `BacktestEngine.run(strategy, snapshots) -> ReplayResult` by converting legacy snapshots to one-level full-book events.
- Extends `ReplayResult` with `execution_reports` and `portfolio_snapshots` while retaining `signals`, `order_results`, `positions`, `equity_curve`, and `metrics`.

- [ ] **Step 1: Write a failing depth-aware integration test**

Add a strategy that emits one buy signal, then test:

```python
@pytest.mark.asyncio
async def test_backtest_consumes_depth_and_records_partial_fill_and_fees() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5", "max_slippage_bps": 300, "time_in_force": "IOC"},
        backtest={"starting_cash": "100", "taker_fee_bps": "10"},
        risk={"min_top_of_book_liquidity": "1"},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(
        strategy=BuyOnceStrategy(),
        events=[snapshot_event(
            sequence=1,
            bids=[("0.49", "10")],
            asks=[("0.50", "2"), ("0.51", "1")],
        )],
    )
    order = result.order_results[0]
    report = result.execution_reports[0]
    assert order.status == OrderStatus.PARTIALLY_FILLED
    assert order.requested_size == Decimal("5")
    assert order.filled_size == Decimal("3")
    assert report.average_fill_price == Decimal("0.5033333333333333333333333333")
    assert report.total_fees == Decimal("0.00151")
    assert result.positions[0].quantity == Decimal("3")
    assert result.metrics.fill_rate == Decimal("0.6")
```

- [ ] **Step 2: Write a failing insufficient-capital atomicity test**

```python
@pytest.mark.asyncio
async def test_unfunded_quote_does_not_consume_book_or_create_position() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={"default_order_size": "5"},
        backtest={"starting_cash": "1", "taker_fee_bps": "10"},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(strategy=BuyOnceStrategy(), events=[deep_snapshot_event()])
    assert result.order_results[0].status == OrderStatus.REJECTED
    assert result.order_results[0].message == "insufficient_cash"
    assert result.positions == []
    assert result.portfolio_snapshots[-1].cash == Decimal("1")
    assert engine._books[("m1", "t1")].asks[Decimal("0.50")] == Decimal("100")
```

- [ ] **Step 3: Verify red**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_replay.py -k "consumes_depth or unfunded_quote"
```

Expected: failures because `run_events`, `execution_reports`, and `portfolio_snapshots` do not exist.

- [ ] **Step 4: Refactor engine initialization into repeatable run state**

The current engine stores state in `__init__`, so a second run can inherit positions/signals. Add `_reset_run_state()`, call it from `__init__`, and call it at the start of `run_events`. The legacy `run` wrapper delegates to `run_events` and therefore must not reset a second time. `_reset_run_state()` must recreate:

```python
self._clock = _BacktestClock()
self._state_store = InMemoryStateStore(...)
self._risk = PreTradeRiskEngine(..., now=self._clock.now)
self._order_builder = OrderBuilder(self._config)
self._ledger = PortfolioLedger(self._config.backtest)
self._books: dict[tuple[str, str], OrderBookState] = {}
```

- [ ] **Step 5: Implement legacy conversion and event execution**

Preserve the old method:

```python
async def run(self, *, strategy: StrategyBase, snapshots: list[MarketSnapshot]) -> ReplayResult:
    events = [
        BookSnapshotEvent(
            market_id=item.market_id,
            token_id=item.token_id,
            bids=[OrderBookLevel(price=item.best_bid, size=item.top_bid_size)],
            asks=[OrderBookLevel(price=item.best_ask, size=item.top_ask_size)],
            sequence_id=index,
            source_ts=item.source_ts,
            received_ts=item.received_ts,
        )
        for index, item in enumerate(sorted(snapshots, key=lambda value: value.received_ts))
    ]
    return await self.run_events(strategy=strategy, events=events)
```

Start `run_events` with `ordered_events = sorted(list(events), key=lambda item: (item.received_ts, item.source_ts, item.sequence_id))`; this creates a new list and leaves caller order untouched. Implement this exact ordering for each event:

1. Advance historical clock.
2. Get/create the book by `(market_id, token_id)`.
3. Atomically apply snapshot or delta.
4. Obtain `MarketSnapshot`. When either side is empty, skip marking/strategy/execution for this event but still append a `PortfolioSnapshot` and compatibility `EquityPoint` using the ledger's existing marks; every valid historical event must produce an equity observation.
5. Update in-memory market snapshot and heartbeat.
6. Mark ledger at the current midpoint and mirror the position into `InMemoryStateStore`.
7. Run strategy.
8. For each signal, append it to `ReplayResult.signals`, add it to `InMemoryStateStore` (preserving duplicate-signal risk behavior), and build the full requested order.
9. Quote book depth without mutation.
10. If no fills, append the `UNFILLED` execution report and compatibility order result, then continue.
11. Ask ledger `can_apply`; if unfunded, replace the candidate quote with a no-fill `REJECTED` report carrying the stable ledger reason, append it, and continue without mutation.
12. Run pre-trade risk with `proposed_size=report.filled_size`, `proposed_price=report.average_fill_price`, and `executable_liquidity=report.executable_liquidity`.
13. If risk rejects, replace the candidate quote with a no-fill `REJECTED` report carrying `decision.reason`, append it, and continue without committing book or ledger.
14. Commit book depth.
15. Apply report to ledger.
16. Mirror the changed position into `InMemoryStateStore`.
17. Append exactly one final `ExecutionReport` per built order and convert it to one compatibility `OrderResult` (`FILLED`, `PARTIALLY_FILLED`, or `REJECTED`). Candidate quote fills must never appear as executed fills after a funding/risk rejection.
18. Re-mark at midpoint.
19. Append `PortfolioSnapshot` and compatibility `EquityPoint` after all signals for the event.

Never commit depth before both ledger funding and risk approval succeed.

Use a helper for funding/risk rejection so quote mutation is consistent:

```python
def _reject_report(candidate: ExecutionReport, reason: str) -> ExecutionReport:
    return candidate.model_copy(update={
        "status": ExecutionStatus.REJECTED,
        "fills": [],
        "filled_size": Decimal("0"),
        "remaining_size": candidate.requested_size,
        "average_fill_price": None,
        "total_notional": Decimal("0"),
        "total_fees": Decimal("0"),
        "reason": reason,
    })
```

- [ ] **Step 6: Convert reports to compatibility order results**

Add one helper:

```python
def _to_order_result(report: ExecutionReport) -> OrderResult:
    if report.status in {ExecutionStatus.REJECTED, ExecutionStatus.UNFILLED}:
        status = OrderStatus.REJECTED
        accepted = False
    elif report.status == ExecutionStatus.FILLED:
        status = OrderStatus.FILLED
        accepted = True
    elif report.status == ExecutionStatus.PARTIAL:
        status = OrderStatus.PARTIALLY_FILLED
        accepted = True
    else:
        status = OrderStatus.REJECTED
        accepted = False
    return OrderResult(
        client_order_id=report.order.client_order_id,
        market_id=report.order.market_id,
        token_id=report.order.token_id,
        side=report.order.side,
        status=status,
        accepted=accepted,
        message=report.reason,
        signal_id=report.order.signal_id,
        strategy_name=report.order.strategy_name,
        requested_size=report.requested_size,
        filled_size=report.filled_size,
        avg_fill_price=report.average_fill_price,
        created_at=report.order.created_at,
    )
```

- [ ] **Step 7: Expand metrics from portfolio and execution reports**

Extend `ReplayMetrics` with:

```python
starting_cash: Decimal
ending_cash: Decimal
ending_equity: Decimal
reserved_cash: Decimal
fees_paid: Decimal
gross_pnl: Decimal
net_pnl: Decimal
fill_rate: Decimal
max_drawdown: Decimal
max_drawdown_pct: Decimal
```

Keep `total_pnl` as a compatibility alias value equal to `net_pnl`. Compute fill rate as `sum(filled_size) / sum(requested_size)` across final execution reports, or zero when there are no reports. Seed the running equity peak with `starting_cash`, then define drawdown as a positive loss from that peak: `drawdown = peak_equity - current_equity`; `max_drawdown` is the maximum value and `max_drawdown_pct = drawdown / peak_equity` at the worst point (zero when `peak_equity <= 0`). Sort final positions and each portfolio snapshot's copied positions by `(market_id, token_id)` before serialization so otherwise-identical runs have stable output ordering.

- [ ] **Step 8: Add repeatability, delta, FOK, and short-collateral integration tests**

Add separate tests proving:

- two runs on the same engine, each with a fresh strategy that emits an explicit fixed `signal_id`, produce identical serialized results and no inherited position;
- a `book_delta` changes the next strategy snapshot and fill depth;
- insufficient `FOK` depth produces zero fills and no mutation;
- a synthetic short reserves collateral and an unfundable short is rejected;
- live/dry-run modules are never imported or constructed by backtest paths (inspect `sys.modules` or inject a sentinel only if necessary; prefer structural assertions over mocks).

- [ ] **Step 9: Run focused and full tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_replay.py
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

- [ ] **Step 10: Create a checkpoint commit if authorized**

```bash
git -C /Users/ghost/Projects/trader add bot_v2/backtest/replay.py bot_v2/backtest/metrics.py bot_v2/backtest/test_replay.py
git -C /Users/ghost/Projects/trader commit -m "feat(backtest): integrate matching risk and portfolio ledger"
```

---

### Task 6: CLI, Fixtures, Output, and Documentation

**Files:**
- Modify: `backtest/cli.py`
- Create: `backtest/test_cli.py`
- Create: `backtest/example_orderbook_events.json`
- Modify: `README.md:680-720`

**Interfaces:**
- Replaces `_load_snapshots` internally with `_load_events`, while retaining the `--snapshots` CLI flag.
- Legacy objects without `event_type` become one-level `BookSnapshotEvent` values with deterministic sequence IDs.
- New event objects validate through `TypeAdapter(HistoricalBookEvent)`.
- Output retains current keys and adds `execution_reports` and `portfolio_snapshots`.

- [ ] **Step 1: Write failing CLI compatibility tests**

```python
from datetime import timedelta


def full_book_payload(*, sequence: int) -> dict[str, object]:
    return snapshot_event(sequence=sequence).model_dump(mode="json")


def delta_payload(
    *,
    sequence: int,
    ask_updates: list[tuple[str, str]] | None = None,
) -> dict[str, object]:
    return delta_event(sequence=sequence, ask_updates=ask_updates).model_dump(mode="json")


def cli_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bot.yaml").write_text("bot:\n  mode: dry_run\n", encoding="utf-8")
    return config_dir, tmp_path / "events.json", tmp_path / "result.json"


def run_cli(*, config_dir: Path, input_path: Path, output_path: Path) -> int:
    return backtest_main([
        "--snapshots", str(input_path),
        "--config-dir", str(config_dir),
        "--output", str(output_path),
    ])


def test_cli_accepts_legacy_snapshots_and_emits_capital_metrics(tmp_path: Path) -> None:
    config_dir, input_path, output_path = cli_paths(tmp_path)
    legacy = []
    for index, price_text in enumerate(("0.50", "0.50", "0.50", "0.56")):
        price = Decimal(price_text)
        legacy.append(MarketSnapshot(
            market_id="m1",
            token_id="t1",
            best_bid=price - Decimal("0.01"),
            best_ask=price + Decimal("0.01"),
            mid_price=price,
            top_bid_size=Decimal("100"),
            top_ask_size=Decimal("100"),
            source_ts=NOW + timedelta(seconds=index),
            received_ts=NOW + timedelta(seconds=index),
        ).model_dump(mode="json"))
    input_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert run_cli(config_dir=config_dir, input_path=input_path, output_path=output_path) == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["metrics"]["starting_cash"] == "1000"
    assert "execution_reports" in payload
    assert "portfolio_snapshots" in payload


def test_cli_accepts_snapshot_and_delta_events(tmp_path: Path) -> None:
    config_dir, input_path, output_path = cli_paths(tmp_path)
    input_path.write_text(json.dumps([
        full_book_payload(sequence=10),
        delta_payload(sequence=11, ask_updates=[("0.51", "0"), ("0.52", "4")]),
    ]), encoding="utf-8")
    assert run_cli(config_dir=config_dir, input_path=input_path, output_path=output_path) == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(payload["portfolio_snapshots"]) == 2


def test_cli_returns_two_for_sequence_gap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_dir, input_path, output_path = cli_paths(tmp_path)
    input_path.write_text(json.dumps([
        full_book_payload(sequence=10),
        delta_payload(sequence=12),
    ]), encoding="utf-8")
    assert run_cli(config_dir=config_dir, input_path=input_path, output_path=output_path) == 2
    assert "sequence gap" in capsys.readouterr().err
    assert not output_path.exists()
```

Import `json`, `Decimal`, `Path`, `pytest`, `backtest_main`, `MarketSnapshot`, and the shared `NOW`, `snapshot_event`, and `delta_event` helpers at the top of the file.

- [ ] **Step 2: Verify red**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider backtest/test_cli.py
```

Expected: failures because the current loader validates every object as `MarketSnapshot`, the result has no capital reports, and the CLI does not call `run_events`.

- [ ] **Step 3: Implement dual-format parsing**

Use:

```python
event_adapter = TypeAdapter(HistoricalBookEvent)
for index, item in enumerate(payload):
    if not isinstance(item, dict):
        raise ValueError(f"historical event at index {index} must be an object")
    if "event_type" in item:
        events.append(event_adapter.validate_python(item))
    else:
        snapshot = MarketSnapshot.model_validate(item)
        events.append(BookSnapshotEvent(
            market_id=snapshot.market_id,
            token_id=snapshot.token_id,
            bids=[OrderBookLevel(price=snapshot.best_bid, size=snapshot.top_bid_size)],
            asks=[OrderBookLevel(price=snapshot.best_ask, size=snapshot.top_ask_size)],
            sequence_id=index,
            source_ts=snapshot.source_ts,
            received_ts=snapshot.received_ts,
        ))
```

Sort only after validation. Do not silently repair duplicate or gapped sequence IDs.

- [ ] **Step 4: Serialize all result contracts**

Update `_serialize_result`:

```python
return {
    "signals": [item.model_dump(mode="json") for item in result.signals],
    "order_results": [item.model_dump(mode="json") for item in result.order_results],
    "execution_reports": [item.model_dump(mode="json") for item in result.execution_reports],
    "positions": [item.model_dump(mode="json") for item in result.positions],
    "equity_curve": [asdict(item) for item in result.equity_curve],
    "portfolio_snapshots": [item.model_dump(mode="json") for item in result.portfolio_snapshots],
    "metrics": asdict(result.metrics),
}
```

Continue using `json.dumps(..., default=str)` so Decimals remain exact strings.

- [ ] **Step 5: Add a rich example event fixture**

Create `backtest/example_orderbook_events.json` containing:

- sequence 100 full book with at least three bid and ask levels;
- sequence 101 delta that deletes one level with size zero and updates another;
- sequence 102 delta that moves the best bid/ask enough to trigger the configured spike strategy;
- ISO-8601 UTC `source_ts` and `received_ts` values.

Keep `backtest/example_snapshots.json` unchanged as the legacy-compatibility fixture.

- [ ] **Step 6: Update README with exact usage and limitations**

Document:

```bash
python3 -m backtest.cli \
  --snapshots backtest/example_orderbook_events.json \
  --output backtest/results/realistic-backtest.json
```

Explain the new `backtest:` YAML settings, event schemas, sequence behavior, cash/collateral equations, partial-fill rules, and output fields. State these intentional limitations:

- no maker queue or resting-order model;
- no latency model;
- fixed configurable taker fee rather than market-specific Polymarket fee curves;
- synthetic shorts reserve full payout collateral but do not emulate token minting;
- no settlement/resolution event yet.

- [ ] **Step 7: Run CLI smoke tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m backtest.cli \
  --snapshots backtest/example_snapshots.json \
  --output /private/tmp/legacy-backtest.json

PYTHONDONTWRITEBYTECODE=1 python3 -m backtest.cli \
  --snapshots backtest/example_orderbook_events.json \
  --output /private/tmp/depth-backtest.json
```

Read both output files and assert in a short verification command that they contain `metrics`, `execution_reports`, and `portfolio_snapshots`, and that `ending_equity == starting_cash + net_pnl`.

- [ ] **Step 8: Run full tests and create a checkpoint commit if authorized**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
git diff --check
```

If commits are authorized:

```bash
git -C /Users/ghost/Projects/trader add bot_v2/backtest/cli.py bot_v2/backtest/test_cli.py bot_v2/backtest/example_orderbook_events.json bot_v2/README.md
git -C /Users/ghost/Projects/trader commit -m "docs(backtest): expose realistic historical simulation"
```

---

### Task 7: Final Verification and Handoff

**Files:**
- Inspect all modified files.
- Do not create generated artifacts in the repository.

**Interfaces:**
- Confirms all requirements and accounting invariants from this plan.

- [ ] **Step 1: Run the complete test suite with normal discovery**

```bash
cd /Users/ghost/Projects/trader/bot_v2
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```

Expected: all legacy and new tests pass with zero failures.

- [ ] **Step 2: Run syntax and whitespace checks**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/bot_v2_verify_pycache python3 -m compileall -q .
git diff --check
```

- [ ] **Step 3: Verify package installation in a temporary environment**

```bash
python3 -m venv /private/tmp/bot_v2_package_verify
/private/tmp/bot_v2_package_verify/bin/python -m pip install --no-deps -e /Users/ghost/Projects/trader/bot_v2
/private/tmp/bot_v2_package_verify/bin/python -c "from backtest.cli import main; from backtest.models import BookSnapshotEvent; print('imports-ok')"
```

Expected: editable build succeeds and prints `imports-ok`.

- [ ] **Step 4: Audit accounting invariants from a CLI result**

For every `portfolio_snapshots` entry, independently assert:

```python
snapshot["available_cash"] == snapshot["cash"] - snapshot["reserved_cash"]
snapshot["equity"] == snapshot["cash"] + snapshot["position_value"]
snapshot["net_pnl"] == snapshot["equity"] - metrics["starting_cash"]
```

At the final point assert:

```python
metrics["ending_equity"] == metrics["starting_cash"] + metrics["net_pnl"]
metrics["total_pnl"] == metrics["net_pnl"]
```

- [ ] **Step 5: Confirm live isolation structurally**

```bash
rg -n "WebSocketManager|ClobClientAdapter|OrderSubmitter|submit_order|websockets|httpx" backtest
```

Expected: no production import or call in `backtest/*.py`; documentation/tests may mention names only as assertions.

- [ ] **Step 6: Review the final diff for scope and generated files**

```bash
git status --short
git diff --stat
git diff --check
```

Confirm:

- the unrelated `../.DS_Store` is untouched;
- no new `__pycache__`, `.pytest_cache`, `build/`, or `*.egg-info` paths exist;
- no live-trading guard was relaxed;
- the existing uncommitted backtest baseline remains present;
- every new public model and method has direct test coverage.

- [ ] **Step 7: Request a read-only code review**

Ask the reviewer to focus on sequence atomicity, quote/book mutation order, fee/cash signs, short collateral, partial-fill status mapping, Decimal-only arithmetic, repeatability, and live isolation. Resolve Important findings with regression tests before handoff.

## Completion Criteria

The work is complete only when all of the following are proven by current command output:

- legacy snapshot input still runs;
- snapshot-plus-delta input runs;
- sequence gaps and crossed books fail with actionable errors;
- multi-level fills produce exact VWAP and fee values;
- partial fills preserve an explicit remainder;
- FOK failure leaves book and portfolio unchanged;
- capital and synthetic-short collateral rejections are atomic;
- fees reduce cash and net P&L;
- equity and net-P&L invariants hold at every event;
- repeated runs are deterministic and isolated;
- normal `python3 -m pytest` discovers all backtest tests;
- CLI smoke tests succeed for both bundled fixtures;
- backtest code has no live client imports or network calls;
- `git diff --check` passes and no generated artifacts remain.

## Execution Handoff

Recommended execution mode: use `superpowers:subagent-driven-development`, one fresh implementer per task, with a requirements review followed by a code-quality review after each task. If only one model/session is available, use `superpowers:executing-plans` and execute Tasks 1-7 sequentially with the listed checkpoints.

Do not broaden this plan into maker queues, latency, settlement, parameter sweeps, or market-specific fee curves until the conservative taker simulator passes every completion criterion above.
