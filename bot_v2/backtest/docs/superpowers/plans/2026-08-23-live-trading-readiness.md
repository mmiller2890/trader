# Live Trading Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the five confirmed simulator defects and build a default-off, compliance-gated, observable Polymarket CLOB V2 live-trading path.

**Architecture:** Keep the existing strategy → risk → order-builder → submitter structure, but isolate production exchange behavior behind an explicit typed CLOB V2 adapter. Market discovery and WebSocket book reconstruction feed both dry-run and live modes; live startup additionally requires secrets, geographic compliance, authenticated account reads, balance/allowance checks, and reconciliation before order submission is reachable.

**Tech Stack:** Python 3.11+, asyncio, `Decimal`, Pydantic v2, `py-clob-client-v2==1.1.0`, websockets, httpx, pytest, pytest-asyncio.

**Spec:** `backtest/docs/superpowers/specs/2026-08-23-live-trading-readiness-design.md`

## Global Constraints

- Start from repository root `/Users/ghost/Projects/trader` and project root `/Users/ghost/Projects/trader/bot_v2` exactly as commands specify.
- Preserve the dirty worktree. At plan creation, `backtest/replay.py` contains an uncommitted `Decimal(str(...))` correction; do not overwrite it.
- Leave unrelated `../.DS_Store` and `backtest/results/.example-backtest.json.swp` untouched unless the user explicitly asks for cleanup.
- Do not commit unless the user explicitly authorizes commits. Commit commands below are optional checkpoints only.
- Follow TDD: write one focused regression, confirm it fails for the intended reason, implement the minimum fix, then run focused and full tests.
- Unit tests must not access the network. Inject fake SDK, HTTP, and WebSocket implementations.
- Keep `backtest` and `replay` fully offline and free of live SDK/client imports.
- Use `Decimal` for internal monetary values. Convert to SDK-required floats only at the final adapter boundary with `float(str(value))` after tick/size validation.
- Never log private keys, API secrets, passphrases, signed orders, or full authentication responses.
- Live trading stays unreachable until every preflight check succeeds. A timeout, malformed response, or unknown state fails closed.
- No implementation task authorizes funding a wallet, changing onchain allowances, or placing a production order.
- Pin the CLOB dependency exactly to `py-clob-client-v2==1.1.0`; do not retain legacy `py-clob-client`.
- Production defaults: CLOB host `https://clob.polymarket.com`, Data API host `https://data-api.polymarket.com`, Polygon chain ID `137`, WebSocket `wss://ws-subscriptions-clob.polymarket.com/ws/market`, geoblock URL `https://polymarket.com/api/geoblock`, signature type `3`, WebSocket application ping every `10` seconds.
- Run tests without generated cache files:

```bash
cd /Users/ghost/Projects/trader/bot_v2
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Baseline before implementation: `48 passed`.

## Five Confirmed Issues This Plan Must Close

1. **Cross-market short collateral is understated:** `PortfolioLedger.can_apply` projects only the position being changed, so an additional short can be accepted without reserving collateral for shorts already open in other markets.
2. **Executable liquidity includes unusable depth:** `OrderBookState.quote` counts levels outside the execution-price limit even though those levels cannot fill the order.
3. **A failed commit can consume part of the book:** `OrderBookState.commit` mutates early price levels before it discovers that a later fill is invalid.
4. **Risk sees the best quote instead of execution VWAP:** `BacktestEngine` can approve a multi-level fill whose actual average price violates the risk slippage limit.
5. **Legacy equal-time snapshots can be sequenced incorrectly:** sequence IDs are assigned before the final `(received_ts, source_ts)` ordering, so equal-receive-time inputs can replay out of sequence.

Tasks 1–3 add one direct regression for each item and must be completed before any live-trading work starts.

## Target File Map

- Modify `backtest/portfolio.py`: aggregate projected reserve across all positions.
- Modify `backtest/orderbook.py`: report only in-limit liquidity and make commits atomic.
- Modify `backtest/replay.py`: evaluate VWAP slippage and make legacy ordering consistent.
- Extend `backtest/test_portfolio.py`, `backtest/test_orderbook.py`, and `backtest/test_replay.py` with the five reviewed regressions.
- Create `.gitignore` and `.env.example`: protect and document secrets.
- Modify `pyproject.toml`: migrate to the pinned V2 SDK.
- Modify `config/schema.py` and `config/bot.yaml`: explicit exchange and live-safety settings.
- Rewrite `clients/clob_client.py`: typed V2 construction, account reads, submit, cancel, and normalization.
- Create `clients/data_api.py`: paginated current-position reads and normalization.
- Create `clients/geoblock.py`: fail-closed geographic compliance client.
- Create `clients/live_book.py`: production market-channel full-book/delta reconstruction.
- Modify `clients/ws_client.py` and `clients/market_data_client.py`: subscription, ping, V2 message parsing, and snapshot production.
- Modify `app/bootstrap.py` and `app/main.py`: preflight-gated live construction and cancel-on-halt.
- Modify `execution/submitter.py`, `execution/tracker.py`, and `execution/router.py`: hard live notional cap and uncertain-result reconciliation.
- Modify `state/reconciliation.py`: open orders, positions, and account truth.
- Create `scripts/live_preflight.py`: explicit read-only operator check.
- Extend tests under `tests/`; update `README.md` with shadow and go-live runbooks.

---

### Task 1: Fix Multi-Market Short Collateral Projection

**Files:**
- Modify: `backtest/portfolio.py:79-97,173-185`
- Test: `backtest/test_portfolio.py`

**Interfaces:**
- Preserves `PortfolioLedger.can_apply(report) -> tuple[bool, str]`.
- Replaces the override-only reserve helper with `_reserved_cash(positions: Iterable[Position]) -> Decimal`.

- [ ] **Step 1: Add the exact failing regression**

```python
def test_second_market_short_uses_combined_portfolio_reserve() -> None:
    ledger = PortfolioLedger(BacktestConfig(
        starting_cash="4",
        taker_fee_bps="0",
        allow_short_positions=True,
    ))
    first = filled_sell(size="5", price="0.50")
    assert ledger.can_apply(first) == (True, "funded")
    ledger.apply(first, NOW)

    second = filled_sell(size="5", price="0.50")
    second = second.model_copy(update={
        "order": second.order.model_copy(update={
            "market_id": "m2",
            "token_id": "t2",
        })
    })
    assert ledger.can_apply(second) == (
        False,
        "insufficient_short_collateral",
    )
    assert ledger.cash == Decimal("6.50")
    assert set(ledger.positions) == {("m1", "t1")}
```

- [ ] **Step 2: Confirm red**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  backtest/test_portfolio.py::test_second_market_short_uses_combined_portfolio_reserve
```

Expected: current code returns `(True, "funded")` for the second short.

- [ ] **Step 3: Project the complete position mapping**

In `can_apply`, copy the mapping and replace only the affected key:

```python
projected_positions = dict(self.positions)
projected_positions[key] = projected_position
reserved = self._reserved_cash(projected_positions.values())
```

Replace `_reserved_cash` with:

```python
def _reserved_cash(self, positions: Iterable[Position]) -> Decimal:
    return sum(
        (
            abs(min(position.quantity, Decimal("0")))
            * self.config.max_payout_per_share
            for position in positions
        ),
        start=Decimal("0"),
    )
```

Call it from `snapshot` as `_reserved_cash(self.positions.values())`. Import `Iterable` from `collections.abc`.

- [ ] **Step 4: Verify focused and full suites**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider backtest/test_portfolio.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

- [ ] **Step 5: Optional authorized commit**

```bash
git -C /Users/ghost/Projects/trader add bot_v2/backtest/portfolio.py bot_v2/backtest/test_portfolio.py
git -C /Users/ghost/Projects/trader commit -m "fix(backtest): aggregate short collateral across markets"
```

---

### Task 2: Fix Executable Liquidity and Atomic Book Commits

**Files:**
- Modify: `backtest/orderbook.py:112-115,162-182`
- Test: `backtest/test_orderbook.py`

**Interfaces:**
- Preserves `quote(...) -> ExecutionReport` and `commit(report) -> None`.
- `ExecutionReport.executable_liquidity` means depth eligible inside the computed execution limit.
- `commit` either applies every fill or changes no level.

- [ ] **Step 1: Add the exact executable-liquidity regression**

```python
def test_executable_liquidity_excludes_levels_beyond_price_limit() -> None:
    book = book_with_asks([("0.50", "2"), ("0.51", "3"), ("0.55", "10")])
    report = book.quote(
        buy_order(price="0.50", size="1", tif="IOC"),
        max_slippage_bps=Decimal("300"),
        fee_bps=Decimal("0"),
    )
    assert report.executable_liquidity == Decimal("5")
```

Also change the existing assertion in `test_buy_quote_walks_asks_and_calculates_vwap_and_fees` from:

```python
assert report.executable_liquidity == Decimal("15")
```

to `Decimal("5")`.

The `0.55 × 10` level is outside the `0.515` buy limit and must not count.

- [ ] **Step 2: Add the atomic failure regression**

```python
def test_commit_validation_failure_leaves_every_level_unchanged() -> None:
    book = book_with_asks([("0.50", "2"), ("0.51", "2")])
    report = book.quote(
        buy_order(price="0.50", size="4", tif="IOC"),
        max_slippage_bps=Decimal("300"),
        fee_bps=Decimal("0"),
    )
    invalid = report.model_copy(update={
        "fills": [
            report.fills[0].model_copy(update={"size": Decimal("1")}),
            report.fills[1].model_copy(update={"size": Decimal("3")}),
        ]
    })
    before = book.asks.copy()
    with pytest.raises(ValueError, match="unavailable depth"):
        book.commit(invalid)
    assert book.asks == before
```

- [ ] **Step 3: Confirm both tests fail**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  backtest/test_orderbook.py -k "walks_asks or validation_failure"
```

- [ ] **Step 4: Sum eligible levels and commit through a copy**

```python
eligible_levels = list(eligible)
executable_liquidity = sum(
    (size for _price, size in eligible_levels),
    start=Decimal("0"),
)
```

In `commit`, select the live side, copy it, apply every validation/subtraction to the copy, then assign only after the loop:

```python
live_levels = self.asks if report.order.side == OrderSide.BUY else self.bids
candidate_levels = dict(live_levels)
for fill in report.fills:
    current = candidate_levels.get(fill.price)
    if current is None or current < fill.size:
        raise ValueError(f"report consumes unavailable depth at {fill.price}")
    remaining = current - fill.size
    if remaining > 0:
        candidate_levels[fill.price] = remaining
    else:
        candidate_levels.pop(fill.price)
if report.order.side == OrderSide.BUY:
    self.asks = candidate_levels
else:
    self.bids = candidate_levels
```

- [ ] **Step 5: Verify**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider backtest/test_orderbook.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

---

### Task 3: Fix VWAP Risk Evaluation and Legacy Ordering

**Files:**
- Modify: `backtest/replay.py:87-101,175-199`
- Test: `backtest/test_replay.py`

**Interfaces:**
- Preserves both public run methods.
- Risk consumes the candidate VWAP, not the best quote.
- Legacy sequence IDs are assigned after sorting by `(received_ts, source_ts)`.

- [ ] **Step 1: Add the VWAP slippage regression**

```python
@pytest.mark.asyncio
async def test_backtest_risk_rejects_depth_vwap_outside_risk_limit() -> None:
    config = AppConfig(
        bot={"mode": Mode.BACKTEST},
        execution={
            "default_order_size": "5",
            "max_slippage_bps": 1000,
            "time_in_force": "IOC",
        },
        risk={"min_top_of_book_liquidity": "1", "max_slippage_bps": 25},
        backtest={"starting_cash": "100", "taker_fee_bps": "0"},
    )
    engine = BacktestEngine(config=config)
    result = await engine.run_events(
        strategy=BuyOnceStrategy(),
        events=[snapshot_event(
            sequence=1,
            bids=[("0.49", "100")],
            asks=[("0.50", "1"), ("0.54", "4")],
        )],
    )
    report = result.execution_reports[0]
    assert report.status == ExecutionStatus.REJECTED
    assert report.average_fill_price is None
    assert report.reason.startswith("slippage_limit")
    assert engine._books[("m1", "t1")].asks[Decimal("0.50")] == Decimal("1")
```

- [ ] **Step 2: Add the equal-receive-time legacy regression**

```python
@pytest.mark.asyncio
async def test_legacy_snapshots_assign_sequence_after_source_ordering() -> None:
    received = datetime(2025, 1, 1, tzinfo=UTC)
    later = snapshot(price="0.60", at=received).model_copy(
        update={"source_ts": received + timedelta(seconds=2)}
    )
    earlier = snapshot(price="0.50", at=received).model_copy(
        update={"source_ts": received + timedelta(seconds=1)}
    )
    engine = BacktestEngine(config=AppConfig(bot={"mode": Mode.BACKTEST}))
    result = await engine.run(
        strategy=FixedIdBuyOnceStrategy(),
        snapshots=[later, earlier],
    )
    assert len(result.portfolio_snapshots) == 2
    assert result.order_results[0].avg_fill_price == Decimal("0.51")
```

- [ ] **Step 3: Confirm red**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  backtest/test_replay.py -k "depth_vwap or sequence_after_source"
```

- [ ] **Step 4: Pass VWAP and align ordering**

Before risk evaluation, assert the filled candidate has a VWAP:

```python
assert candidate.average_fill_price is not None
decision = await self._risk.evaluate(
    signal=signal,
    snapshot=snapshot,
    proposed_size=candidate.filled_size,
    proposed_price=candidate.average_fill_price,
    executable_liquidity=candidate.executable_liquidity,
)
```

In `run`, sort before enumeration:

```python
ordered_snapshots = sorted(
    list(snapshots),
    key=lambda item: (item.received_ts, item.source_ts),
)
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
    for index, item in enumerate(ordered_snapshots)
]
```

Keep the current dirty-worktree correction:

```python
max_slippage_bps=Decimal(str(self._config.execution.max_slippage_bps))
```

- [ ] **Step 5: Verify all five reviewed defects**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  backtest/test_portfolio.py backtest/test_orderbook.py backtest/test_replay.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

Expected: at least `53 passed` after the five new regressions.

---

### Task 4: Secret Hygiene, V2 Dependency, and Exchange Configuration

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Modify: `pyproject.toml`
- Modify: `config/schema.py`
- Modify: `config/bot.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `ExchangeConfig` at `AppConfig.exchange`.
- Adds `ExecutionConfig.max_live_order_notional`.
- Pins the production SDK to `py-clob-client-v2==1.1.0`.

- [ ] **Step 1: Add configuration tests**

```python
def test_exchange_defaults_are_production_v2_and_live_stays_off() -> None:
    config = AppConfig()
    assert config.exchange.clob_host == "https://clob.polymarket.com"
    assert config.exchange.data_api_host == "https://data-api.polymarket.com"
    assert config.exchange.chain_id == 137
    assert config.exchange.signature_type == 3
    assert config.exchange.geoblock_url == "https://polymarket.com/api/geoblock"
    assert config.exchange.ws_ping_interval_seconds == 10
    assert config.execution.max_live_order_notional == Decimal("1")
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True


def test_live_notional_cap_must_not_exceed_large_order_threshold() -> None:
    with pytest.raises(ValidationError):
        AppConfig(execution={"max_live_order_notional": "101"})
```

- [ ] **Step 2: Add exact models and validation**

```python
class ExchangeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    chain_id: int = Field(default=137, gt=0)
    signature_type: int = Field(default=3, ge=0, le=3)
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    ws_ping_interval_seconds: float = Field(default=10, ge=5, le=60)
    compliance_timeout_seconds: float = Field(default=5, gt=0, le=30)
```

Add `exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)` to `AppConfig`, and add to `ExecutionConfig`:

```python
max_live_order_notional: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
```

In `AppConfig.validate_mode_guards`, reject `max_live_order_notional > notifications.large_order_threshold`.

- [ ] **Step 3: Protect secrets before documenting them**

Create `.gitignore`:

```gitignore
.env
.venv/
data/
*.swp
*.egg-info/
__pycache__/
.pytest_cache/
```

Create `.env.example` with empty values only:

```dotenv
PRIVATE_KEY=
POLYMARKET_PROXY_ADDRESS=
CLOB_API_KEY=
CLOB_SECRET=
CLOB_PASSPHRASE=
RPC_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

- [ ] **Step 4: Migrate dependency and YAML**

Replace `py-clob-client>=0.19.0` with:

```toml
"py-clob-client-v2==1.1.0",
```

Add the `exchange:` defaults and `execution.max_live_order_notional: 1` to `config/bot.yaml`.

- [ ] **Step 5: Reinstall and verify**

```bash
python -m pip install -e ".[dev]"
python -c "import importlib.metadata as m; assert m.version('py-clob-client-v2') == '1.1.0'"
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests/test_config.py
```

---

### Task 5: Replace SDK Guessing with a Typed CLOB V2 Adapter

**Files:**
- Rewrite: `clients/clob_client.py`
- Create: `clients/data_api.py`
- Modify: `clients/auth.py`
- Test: `tests/test_clob_client.py`
- Test: `tests/test_data_api.py`

**Interfaces:**
- Produces `ClobClientAdapter.from_v2(config, credentials, sdk_factory=ClobClient)`.
- Produces `healthcheck()`, `get_open_orders()`, `get_collateral_status()`, `submit_order()`, `cancel_order()`, and `cancel_all()`.
- Produces `DataApiClient.get_positions(user_address) -> list[Position]` using the documented Data API rather than inventing a CLOB SDK call.
- `sdk_factory` is injectable; tests use a fake and never import or call the network client.

- [ ] **Step 1: Define fake-SDK contract tests**

Create a `FakeV2Client` that records constructor kwargs and calls. Test that:

```python
def test_v2_factory_passes_l1_l2_signature_and_funder() -> None:
    adapter = ClobClientAdapter.from_v2(
        config=live_config(),
        credentials=complete_credentials(),
        sdk_factory=FakeV2Client,
    )
    assert adapter._client.kwargs == {
        "host": "https://clob.polymarket.com",
        "chain_id": 137,
        "key": "private-key",
        "creds": ApiCreds(
            api_key="api-key",
            api_secret="api-secret",
            api_passphrase="passphrase",
        ),
        "signature_type": 3,
        "funder": "0x1111111111111111111111111111111111111111",
    }
```

Also test missing private key, incomplete L2 credentials, and missing funder each raise `ClobAdapterError` before SDK construction.

- [ ] **Step 2: Implement exact V2 construction**

Import only from `py_clob_client_v2`:

```python
from py_clob_client_v2 import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    OrderArgs,
    OrderType,
    Side,
)
```

Delete `_call_first_available` and every alternate method-name tuple. Build `ApiCreds` from the repository's existing `ClobCredentials`, then call the injected factory with explicit keywords shown in the test.

- [ ] **Step 3: Test and implement explicit order submission**

Map internal values:

```python
side = Side.BUY if order.side == OrderSide.BUY else Side.SELL
order_type = {
    OrderTimeInForce.GTC: OrderType.GTC,
    # V2 names immediate-or-cancel semantics FAK: fill what is available,
    # then cancel the remainder.
    OrderTimeInForce.IOC: OrderType.FAK,
    OrderTimeInForce.FOK: OrderType.FOK,
}[order.time_in_force]
args = OrderArgs(
    token_id=order.token_id,
    price=float(str(order.price)),
    size=float(str(order.size)),
    side=side,
)
signed = self._client.create_order(args)
raw = self._client.post_order(signed, order_type=order_type)
```

Reject before signing when `order.price * order.size > config.execution.max_live_order_notional`. Normalize the real response into `OrderResult`; require a non-empty exchange order ID for `accepted=True`.

- [ ] **Step 4: Implement explicit reads and cancellation**

Call V2 methods by their actual names only: `get_ok`, `get_orders`, `get_balance_allowance`, `cancel`, and `cancel_all`. Call `get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))`. Normalize malformed responses to `ClobAdapterError`; do not silently return empty account state in live mode.

- [ ] **Step 5: Implement and test the Data API position client**

Use an injected `httpx.Client` and request `GET {data_api_host}/positions` with `user`, `sizeThreshold=0`, `limit=500`, and an incrementing `offset`. Continue until a page has fewer than 500 rows; reject HTTP errors, non-list payloads, malformed rows, and an offset above `10000` with `DataApiError`.

Normalize each row without binary float conversion:

```python
Position(
    market_id=str(row["conditionId"]),
    token_id=str(row["asset"]),
    quantity=Decimal(str(row["size"])),
    average_entry_price=Decimal(str(row["avgPrice"])),
    mark_price=Decimal(str(row["curPrice"])),
)
```

Tests must cover the exact first request params, a 500-row page followed by a second page, an empty account, HTTP failure, malformed JSON shape, and missing/invalid numeric fields.

- [ ] **Step 6: Verify adapter isolation**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_clob_client.py tests/test_data_api.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

---

### Task 6: Implement Market Subscription and Correct Live Book Reconstruction

**Files:**
- Create: `clients/live_book.py`
- Modify: `clients/ws_client.py`
- Modify: `clients/market_data_client.py`
- Test: `tests/test_market_data_client.py`
- Test: `tests/test_ws_client.py`

**Interfaces:**
- Produces `LiveBookState.apply_book(payload)`, `apply_price_change(change, timestamp)`, and `snapshot()`.
- `WebSocketManager` consumes `asset_ids: list[str]` and `ping_interval_seconds`.
- Strategies receive snapshots only from valid two-sided books.

- [ ] **Step 1: Add full-book ordering regression**

```python
@pytest.mark.asyncio
async def test_book_event_computes_true_best_levels_not_first_entries() -> None:
    await client.handle_ws_message({
        "event_type": "book",
        "market": "m1",
        "asset_id": "t1",
        "bids": [{"price": "0.48", "size": "10"}, {"price": "0.50", "size": "3"}],
        "asks": [{"price": "0.54", "size": "8"}, {"price": "0.52", "size": "4"}],
        "timestamp": "1757908892351",
    })
    snapshot = await state.get_market_snapshot("m1", "t1")
    assert snapshot is not None
    assert snapshot.best_bid == Decimal("0.50")
    assert snapshot.best_ask == Decimal("0.52")
```

- [ ] **Step 2: Add delta deletion/upsert regression**

Feed an initial `book`, then:

```python
{
    "event_type": "price_change",
    "market": "m1",
    "timestamp": "1757908892352",
    "price_changes": [
        {"asset_id": "t1", "price": "0.50", "size": "0", "side": "BUY"},
        {"asset_id": "t1", "price": "0.495", "size": "7", "side": "BUY"},
    ],
}
```

Assert zero deletes, non-zero upserts, and the next snapshot uses `0.495`.

- [ ] **Step 3: Implement `LiveBookState` atomically**

Use `dict[Decimal, Decimal]` bids/asks, copy-before-validate behavior, `max(bids)` and `min(asks)`, zero deletion, negative-size rejection, crossed-book rejection, and millisecond timestamps converted with:

```python
datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC)
```

- [ ] **Step 4: Add WebSocket subscription and ping tests**

With a fake socket, assert the first sent object is:

```python
{"assets_ids": ["t1", "t2"], "type": "market"}
```

Advance a fake clock ten seconds and assert the socket sends the string `PING`. On reconnect, assert subscription is sent again before frames are consumed.

- [ ] **Step 5: Wire `WebSocketManager`**

Add constructor fields `asset_ids` and `ping_interval_seconds`. Reject an empty asset list at startup. Use the existing `on_connect` boundary to send the JSON subscription, and run/cancel a per-connection ping task in `_consume_connection`.

- [ ] **Step 6: Handle lifecycle events safely**

- `tick_size_change`: store the new tick size for order validation.
- `market_resolved`: mark the market disabled and stop routing new signals.
- `last_trade_price`: update optional last-trade state without altering depth.
- unknown event types: structured debug log, no exception.
- malformed subscribed-asset events: structured warning and no state mutation.

- [ ] **Step 7: Verify**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_market_data_client.py tests/test_ws_client.py
```

---

### Task 7: Add Fail-Closed Compliance and Read-Only Live Preflight

**Files:**
- Create: `clients/geoblock.py`
- Create: `scripts/live_preflight.py`
- Modify: `app/bootstrap.py`
- Test: `tests/test_geoblock.py`
- Test: `tests/test_live_preflight.py`

**Interfaces:**
- Produces `GeoblockClient.check() -> GeoblockStatus`.
- Produces `run_preflight(config, adapter, positions_client, geoblock) -> LivePreflightReport`.
- The script never submits, signs, or cancels orders.

- [ ] **Step 1: Define fail-closed compliance tests**

Test four responses: `{"blocked": false}`, `{"blocked": true}`, timeout, and malformed JSON. Only the first returns an allowed status; all others return blocked/error and prevent live startup.

- [ ] **Step 2: Implement the client with injected HTTP transport**

```python
class GeoblockStatus(BaseModel):
    allowed: bool
    country: str | None = None
    region: str | None = None
    reason: str
```

Use `httpx.Client(timeout=config.exchange.compliance_timeout_seconds)` by default, but accept an injected client in tests. Never include the returned IP in logs or persisted reports.

- [ ] **Step 3: Define preflight checks**

`LivePreflightReport` contains named checks for:

1. configuration live guards;
2. complete L1/L2 credentials and funder;
3. geoblock allowed;
4. CLOB health/version reachable;
5. authenticated open-order read;
6. Data API positions read for the configured funder address;
7. pUSD balance and allowance sufficient for `max_live_order_notional`;
8. at least one subscribed token ID;
9. startup reconciliation successful.

Every check has `name`, `passed`, and redacted `reason`. `report.ok` is true only when every check passes.

- [ ] **Step 4: Implement the read-only command**

```bash
python -m scripts.live_preflight --config-dir config
```

Exit `0` only for a fully passing report, `2` otherwise. Print JSON with no secrets. The command must not call `submit_order`, `post_order`, `cancel`, or allowance mutation.

- [ ] **Step 5: Replace unconditional live bootstrap failure with preflight gating**

Construct the V2 adapter and Data API client only in live mode. Run preflight before building `OrderSubmitter`; raise `RuntimeError("live startup blocked by preflight: ...")` when it fails. Keep dry-run using `ClobClientAdapter.disabled()` so credentials are unnecessary.

- [ ] **Step 6: Verify**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_geoblock.py tests/test_live_preflight.py
```

---

### Task 8: Complete Order Lifecycle, Reconciliation, and Kill-Switch Cancellation

**Files:**
- Modify: `execution/submitter.py`
- Modify: `execution/tracker.py`
- Modify: `execution/router.py`
- Modify: `state/reconciliation.py`
- Modify: `app/main.py`
- Test: `tests/test_submitter.py`
- Test: `tests/test_reconciliation.py`
- Test: `tests/test_live_kill_switch.py`

**Interfaces:**
- Produces `OrderSubmitter.cancel_all_open_orders() -> list[str]`.
- Produces reconciliation across CLOB open orders and Data API positions.
- Makes uncertain submission outcomes non-retriable until reconciled.

- [ ] **Step 1: Add hard-cap and uncertain-submit tests**

Prove a live order above `max_live_order_notional` is rejected before the adapter is called. Simulate a timeout after adapter submission and assert the client order ID enters an `UNKNOWN`/non-retriable state; a second submit with the same ID must not call the adapter again.

- [ ] **Step 2: Add reconciliation tests**

Test:

- remote order missing locally is imported;
- local open order missing remotely blocks live startup until terminal status is resolved;
- remote position mismatch blocks live startup;
- adapter read error blocks live startup;
- dry-run reconciliation remains non-blocking.

- [ ] **Step 3: Add kill-switch cancellation test**

With two fake open orders, trigger runtime risk failure and assert:

1. state kill switch becomes active;
2. adapter `cancel_all` is called once;
3. cancellation results are journaled;
4. future router calls cannot submit;
5. cancellation failure remains a visible critical error and does not clear the kill switch.

- [ ] **Step 4: Implement lifecycle state transitions**

Use only valid transitions:

```text
pending -> submitted -> partially_filled -> filled
pending -> rejected
submitted -> cancelled
submitted -> failed
submitted -> unknown -> reconciled terminal state
```

Do not infer filled size from requested size. Populate fills only from exchange/trade responses.

- [ ] **Step 5: Wire cancel-on-halt and graceful shutdown**

In live mode, a runtime HALT or operator shutdown calls `cancel_all_open_orders` before closing clients. Bound cancellation by `shutdown_timeout_seconds`; timeout is logged as critical and persisted.

- [ ] **Step 6: Verify**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider \
  tests/test_submitter.py tests/test_reconciliation.py tests/test_live_kill_switch.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
```

---

### Task 9: Shadow Burn-In, Operator Runbook, and Final Verification

**Files:**
- Modify: `scripts/healthcheck.py`
- Modify: `README.md`
- Create: `docs/live-runbook.md`
- Test: `tests/test_healthcheck.py`

**Interfaces:**
- Healthcheck fails when market-data heartbeat is missing or stale.
- Runbook defines explicit no-money, read-only, shadow, minimal-order, and capped-live gates.

- [ ] **Step 1: Fix healthcheck false-positive behavior**

Add tests proving a fresh state snapshot with no `market_data` heartbeat returns exit `1`, while a fresh snapshot and heartbeat returns `0`. Implement the missing-heartbeat failure.

- [ ] **Step 2: Document exact operator commands**

Include:

```bash
cd /Users/ghost/Projects/trader/bot_v2
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m backtest.cli --snapshots backtest/example_orderbook_events.json --output /private/tmp/final-backtest.json
python -m app.main
python -m scripts.live_preflight --config-dir config
python -m scripts.healthcheck
```

State explicitly that `python -m app.main` uses `dry_run` until the final operator gate.

- [ ] **Step 3: Define non-negotiable rollout gates**

The runbook must require:

1. all tests and five reviewed regressions green;
2. at least 24 continuous hours of subscribed dry-run data with fresh heartbeat and no parser/reconnect errors;
3. authenticated read-only preflight green;
4. operator verification of jurisdiction, wallet, funder, pUSD balance, and allowances;
5. one explicitly approved minimal order and cancellation using a separately funded limited-risk wallet;
6. single-market live scope with `max_live_order_notional: 1`;
7. verified alerts, kill switch, and cancel-all procedure;
8. explicit user approval before changing the three live flags.

- [ ] **Step 4: Final static and dynamic verification**

```bash
cd /Users/ghost/Projects/trader/bot_v2
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
PYTHONPYCACHEPREFIX=/private/tmp/bot_v2_live_verify python -m compileall -q .
git -C /Users/ghost/Projects/trader diff --check
rg -n "py-clob-client(?!-v2)|_call_first_available" pyproject.toml clients --pcre2
rg -n "PRIVATE_KEY=.+|CLOB_SECRET=.+|CLOB_PASSPHRASE=.+" . --hidden -g '!*.md' -g '!.git/**'
```

Expected: full tests pass, compile succeeds, whitespace check succeeds, legacy SDK/method guessing has no matches, and secret-value scan has no matches.

- [ ] **Step 5: Verify offline isolation**

```bash
python -c "import sys; from backtest.replay import BacktestEngine; assert not any(name.startswith(('clients.clob_client', 'httpx', 'websockets', 'py_clob_client_v2')) for name in sys.modules); print('backtest-offline-ok')"
```

- [ ] **Step 6: Final read-only review**

Request review focused on credential redaction, compliance failure behavior, SDK method correctness, WebSocket resubscription, book ordering, idempotency, unknown submit outcomes, reconciliation, hard notional cap, cancel-on-halt, and all five original simulator findings.

## Completion Criteria

Implementation is complete only when current evidence proves:

- all five reviewed simulator defects have direct passing regressions;
- the legacy CLOB SDK is removed and V2 `1.1.0` is installed;
- `.env` is ignored and `.env.example` contains no values;
- backtest imports no live/network modules;
- dry-run subscribes to configured asset IDs and maintains a fresh two-sided book;
- WebSocket reconnect resubscribes and application ping runs every ten seconds;
- compliance, credential, CLOB health, balance/allowance, subscription, and reconciliation failures all block live startup;
- the adapter uses explicit V2 methods and maps order types correctly;
- live hard notional cap is enforced before SDK invocation;
- uncertain submissions cannot be blindly retried;
- runtime halt and shutdown cancel open live orders;
- missing market heartbeat makes healthcheck fail;
- the documented 24-hour shadow gate and explicit operator approval remain required before live flags change;
- no production order, deposit, or allowance change was performed during implementation.

## Execution Handoff

Recommended mode: `superpowers:subagent-driven-development`, one fresh implementer per task with requirements and code-quality review after each. If only one model is available, use `superpowers:executing-plans` and execute Tasks 1–9 sequentially.

The first implementation prompt should be:

> Read the design and implementation plan completely. Preserve the dirty worktree and the existing `Decimal(str(...))` correction. Begin Task 1 with its failing regression. Do not enable live mode, use real credentials, fund a wallet, change allowances, place an order, or commit without explicit authorization.
