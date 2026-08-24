# Position Lifecycle and Exit Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make confirmed entry fills immediately and durably tradable, then close positions through strategy reversal, take profit, stop loss, maximum hold, or market-expiry exits without double-counting, overselling, or unsafe retries.

**Architecture:** Add idempotent cumulative-fill accounting to the state boundary, keep short-lived exchange/Data API disagreements behind an explicit grace period, and introduce a pure exit policy plus a reservation-aware coordinator. All orders continue through the existing router, risk engine, submitter, tracker, reconciliation, journal, and snapshot boundaries; entry orders remain FOK while managed exits use internal IOC mapped to Polymarket FAK.

**Tech Stack:** Python 3.11, asyncio, Decimal, Pydantic 2, FastAPI, PyYAML, py-clob-client-v2, pytest, pytest-asyncio, plain HTML/CSS/JavaScript.

**Spec:** `backtest/docs/superpowers/specs/2026-08-24-position-lifecycle-exit-management-design.md`

## Global Constraints

- Live and dry-run positions are long-only; offline backtest synthetic shorts remain unchanged.
- Only `FILLED`, `PARTIALLY_FILLED`, and dry-run `SIMULATED` cumulative fill deltas may change inventory.
- Use `Decimal` for every size, price, notional, and P&L calculation.
- Entry orders retain configured `FOK`; managed exits use internal `IOC`, mapped by the adapter to exchange `FAK`.
- A SELL may never exceed confirmed local quantity; replayed results must be idempotent.
- Never retry `UNKNOWN`; latch the kill switch and require reconciliation/operator review.
- Preserve the $1 minimum BUY and $1.01 live notional cap.
- Live snapshots never restore historical position quantities; active exchange positions remain startup authority.
- Never expose credentials, addresses, signatures, or raw upstream exception text.
- Follow red-green-refactor and commit only after each task's focused tests pass.

---

### Task 1: Configuration and Domain Types

**Files:** Modify `config/schema.py`, `config/bot.yaml`, `models/order.py`, `models/signal.py`, `models/position.py`; test `tests/test_config.py`; create `tests/test_position_models.py`.

**Interfaces:** Produces `PositionManagementConfig`, `ExitReason`, `FillCheckpoint`, `PositionLifecycle`, `FillApplication`, and exit-intent fields on `TradeSignal`.

- [ ] **Step 1: Write failing configuration tests**

```python
def test_production_position_management_defaults() -> None:
    config = load_config(PROJECT_ROOT / "config")
    policy = config.position_management
    assert policy.take_profit_bps == Decimal("300")
    assert policy.stop_loss_bps == Decimal("200")
    assert policy.max_hold_seconds == 180
    assert policy.exit_before_market_end_seconds == 60
    assert policy.exit_retry_interval_seconds == 2
    assert policy.max_exit_attempts == 3
    assert policy.position_confirmation_grace_seconds == 30
    assert policy.exit_time_in_force == TimeInForce.IOC
```

Parametrize invalid zero values for every positive duration/limit and assert `ValidationError`.

- [ ] **Step 2: Write failing strict-model tests**

```python
def test_position_exit_signal_carries_execution_intent() -> None:
    signal = TradeSignal(
        strategy_name="position_exit",
        signal_type=SignalType.POSITION_EXIT,
        market_id="m1", token_id="t1", side=SignalSide.SELL,
        reference_price=Decimal("0.50"), target_price=Decimal("0.55"),
        observed_move_bps=Decimal("1000"), reason="take_profit",
        requested_size=Decimal("2.5"), reduce_only=True,
        time_in_force=OrderTimeInForce.IOC,
    )
    assert signal.requested_size == Decimal("2.5")
    assert signal.reduce_only is True


def test_fill_checkpoint_rejects_negative_cumulative_values() -> None:
    with pytest.raises(ValidationError):
        FillCheckpoint(
            order_key="0xorder0001", market_id="m1", token_id="t1",
            side=OrderSide.BUY, accounted_filled_size=Decimal("-1"),
            accounted_fill_notional=Decimal("0"),
        )
```

- [ ] **Step 3: Verify red tests**

Run: `.venv/bin/pytest tests/test_config.py tests/test_position_models.py -q -p no:cacheprovider`

Expected: failures naming missing configuration and lifecycle fields.

- [ ] **Step 4: Implement exact configuration**

```python
class PositionManagementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    take_profit_bps: Decimal = Field(default=Decimal("300"), gt=0)
    stop_loss_bps: Decimal = Field(default=Decimal("200"), gt=0)
    max_hold_seconds: float = Field(default=180, gt=0)
    exit_before_market_end_seconds: float = Field(default=60, gt=0)
    exit_retry_interval_seconds: float = Field(default=2, gt=0)
    max_exit_attempts: int = Field(default=3, ge=1)
    position_confirmation_grace_seconds: float = Field(default=30, gt=0)
    exit_time_in_force: TimeInForce = TimeInForce.IOC
    exit_on_strategy_sell: bool = True
    liquidate_full_position: bool = True
```

Add it to `AppConfig` and add the exact YAML block from the spec.

- [ ] **Step 5: Implement exact lifecycle models**

```python
class ExitReason(str, Enum):
    STRATEGY_SIGNAL = "strategy_signal"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    MAX_HOLD = "max_hold"
    MARKET_EXPIRY = "market_expiry"


class FillCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_key: str = Field(min_length=8)
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    side: OrderSide
    accounted_filled_size: Decimal = Field(ge=0)
    accounted_fill_notional: Decimal = Field(ge=0)
    confirmed_at: datetime = Field(default_factory=utc_now)


class PositionLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market_id: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    opened_at: datetime; last_fill_at: datetime
    market_end_at: datetime | None = None
    last_exit_reason: ExitReason | None = None
    pending_exit_client_order_id: str | None = None
    last_exit_attempt_at: datetime | None = None
    exit_attempt_count: int = Field(default=0, ge=0)
    confirmation_deadline: datetime | None = None
    closed_at: datetime | None = None
    closed_exit_price: Decimal | None = Field(default=None, gt=0, le=1)
    closed_realized_pnl: Decimal | None = None


class FillApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_key: str
    delta_size: Decimal = Field(ge=0)
    delta_notional: Decimal = Field(ge=0)
    duplicate: bool
    position: Position | None = None
```

Add `SignalType.POSITION_EXIT` plus `requested_size`, `reduce_only`, and `time_in_force` to `TradeSignal`. Validate that reduce-only requires SELL and position-exit signals require reduce-only.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest tests/test_config.py tests/test_position_models.py -q -p no:cacheprovider
git add config/schema.py config/bot.yaml models/order.py models/signal.py models/position.py tests/test_config.py tests/test_position_models.py
git commit -m "feat: define position lifecycle and exit policy models"
```

---

### Task 2: Atomic Idempotent Fill Accounting

**Files:** Modify `state/store.py`; create `tests/test_position_accounting.py`.

**Interfaces:** Consumes Task 1 types. Produces `PositionAccountingError`, `apply_confirmed_fill`, checkpoint/lifecycle accessors, and exit reservation methods.

- [ ] **Step 1: Write failing BUY and cumulative-partial tests**

```python
@pytest.mark.asyncio
async def test_confirmed_buy_creates_weighted_position_once() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    result = filled_result("0xorder0001", filled="2", price="0.40")
    applied = await state.apply_confirmed_fill(result, **APPLY_ARGS)
    replay = await state.apply_confirmed_fill(result, **APPLY_ARGS)
    position = await state.get_position("m1", "t1")
    assert applied.delta_size == Decimal("2")
    assert replay.duplicate is True
    assert position is not None and position.quantity == Decimal("2")


@pytest.mark.asyncio
async def test_cumulative_partial_applies_only_new_delta() -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    await state.apply_confirmed_fill(partial_result("1", "0.40"), **APPLY_ARGS)
    second = await state.apply_confirmed_fill(partial_result("3", "0.50"), **APPLY_ARGS)
    assert second.delta_size == Decimal("2")
    assert second.delta_notional == Decimal("1.10")
    assert (await state.get_position("m1", "t1")).average_entry_price == Decimal("0.50")
```

- [ ] **Step 2: Write failing SELL and invariant tests**

```python
@pytest.mark.asyncio
async def test_sell_reduces_inventory_and_realizes_pnl() -> None:
    state = state_with_position(quantity="3", average="0.40")
    applied = await state.apply_confirmed_fill(sell_result("2", "0.55"), **APPLY_ARGS)
    assert applied.position.quantity == Decimal("1")
    assert applied.position.realized_pnl == Decimal("0.30")


@pytest.mark.asyncio
async def test_sell_cannot_exceed_inventory() -> None:
    state = state_with_position(quantity="1", average="0.40")
    with pytest.raises(PositionAccountingError, match="sell_exceeds_inventory"):
        await state.apply_confirmed_fill(sell_result("2", "0.50"), **APPLY_ARGS)
```

Also cover close-to-zero, cumulative size/notional regression, missing identity/market/token/side/price, `UNKNOWN`, and live `SIMULATED`.

- [ ] **Step 3: Write failing exit-reservation test**

```python
first = await state.reserve_exit("m1", "t1", client_order_id="exit-order-0001", reason=ExitReason.TAKE_PROFIT, attempted_at=NOW)
second = await state.reserve_exit("m1", "t1", client_order_id="exit-order-0002", reason=ExitReason.TAKE_PROFIT, attempted_at=NOW)
assert first is True and second is False
await state.release_exit("m1", "t1", client_order_id="exit-order-0001")
```

- [ ] **Step 4: Verify red tests**

Run: `.venv/bin/pytest tests/test_position_accounting.py -q -p no:cacheprovider`

- [ ] **Step 5: Implement one lock-protected transaction**

```python
async def apply_confirmed_fill(
    self, result: OrderResult, *, market_end_at: datetime | None,
    confirmed_at: datetime, confirmation_grace_seconds: float,
) -> FillApplication: pass

async def get_fill_checkpoints(self) -> list[FillCheckpoint]: pass
async def restore_fill_checkpoint(self, checkpoint: FillCheckpoint) -> None: pass
async def get_position_lifecycles(self) -> list[PositionLifecycle]: pass
async def get_position_lifecycle(self, market_id: str, token_id: str) -> PositionLifecycle | None: pass
async def restore_position_lifecycle(self, lifecycle: PositionLifecycle) -> None: pass
async def reserve_exit(self, market_id: str, token_id: str, *, client_order_id: str, reason: ExitReason, attempted_at: datetime) -> bool: pass
async def release_exit(self, market_id: str, token_id: str, *, client_order_id: str) -> bool: pass
```

Use cumulative notional subtraction to derive each delta price. Validate every invariant before mutation. Reset `exit_attempt_count` after a SELL delta reduces inventory. Remove zero-quantity positions, retain close price/realized P&L in 20 closed lifecycles, and keep all operations under the existing state lock.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest tests/test_position_accounting.py tests/test_state_store.py tests/test_risk_pretrade.py -q -p no:cacheprovider
git add state/store.py tests/test_position_accounting.py
git commit -m "feat: account confirmed fills idempotently"
```

---

### Task 3: Durable Lifecycle and Checkpoint Snapshots

**Files:** Modify `persistence/snapshots.py` and `tests/test_snapshots.py`.

**Interfaces:** Consumes Task 2 state accessors. Produces backward-compatible snapshot fields and atomic file replacement.

- [ ] **Step 1: Write failing round-trip and compatibility tests**

```python
@pytest.mark.asyncio
async def test_snapshot_round_trips_fill_checkpoints_and_lifecycle(tmp_path) -> None:
    original = accounted_state(mode=Mode.DRY_RUN)
    snapshots = SnapshotStore(tmp_path / "state.json")
    await snapshots.save_from_state(original)
    restored = InMemoryStateStore(mode=Mode.DRY_RUN)
    assert await snapshots.restore_into_state(restored) is True
    assert await restored.get_fill_checkpoints() == await original.get_fill_checkpoints()
    assert await restored.get_position_lifecycles() == await original.get_position_lifecycles()


@pytest.mark.asyncio
async def test_old_snapshot_without_new_fields_still_loads(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"mode": "dry_run"}), encoding="utf-8")
    loaded = await SnapshotStore(path).load()
    assert loaded.fill_checkpoints == []
    assert loaded.position_lifecycles == []
```

- [ ] **Step 2: Write atomic-write failure test**

Patch `Path.replace` to raise after a temporary file is written. Assert the prior target remains parseable and unchanged; the target may never be truncated before replacement.

- [ ] **Step 3: Verify red tests**

Run: `.venv/bin/pytest tests/test_snapshots.py -q -p no:cacheprovider`

- [ ] **Step 4: Implement fields and atomic replacement**

```python
class StateSnapshot(BaseModel):
    # keep existing fields
    fill_checkpoints: list[FillCheckpoint] = Field(default_factory=list)
    position_lifecycles: list[PositionLifecycle] = Field(default_factory=list)
```

Populate both in `save_from_state`; restore them even when `restore_positions=False`. Write JSON to `NamedTemporaryFile` in the target directory, flush and close it, then call `Path.replace`. Delete only the known temporary file after failure.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/test_snapshots.py tests/test_dashboard_read_model.py -q -p no:cacheprovider
git add persistence/snapshots.py tests/test_snapshots.py
git commit -m "feat: persist fill accounting and position lifecycle"
```

---

### Task 4: Tracker Integration and Real Dry-Run Fills

**Files:** Modify `execution/submitter.py`, `execution/tracker.py`, `execution/router.py`, `models/events.py`, `tests/test_submitter.py`, and `tests/test_execution_router.py`; create `tests/test_order_tracker.py`.

**Interfaces:** Consumes Tasks 2–3. Produces `TrackingOutcome` and immediate fill persistence.

- [ ] **Step 1: Write failing dry-run fill test**

```python
@pytest.mark.asyncio
async def test_dry_run_returns_simulated_confirmed_fill() -> None:
    result = await dry_submitter().submit(buy_request(size="2", price="0.45"))
    assert result.status == OrderStatus.SIMULATED
    assert result.accepted is True
    assert result.filled_size == Decimal("2")
    assert result.avg_fill_price == Decimal("0.45")
```

- [ ] **Step 2: Write failing tracker tests**

```python
@pytest.mark.asyncio
async def test_tracker_applies_fill_and_saves_immediately(tmp_path) -> None:
    state = InMemoryStateStore(mode=Mode.LIVE)
    snapshots = SnapshotStore(tmp_path / "state.json")
    tracker = OrderTracker(state_store=state, snapshots=snapshots, confirmation_grace_seconds=30)
    outcome = await tracker.handle_order_result(filled_result(), market_end_at=END_AT)
    assert outcome.fill_applied is True
    saved = await snapshots.load()
    assert saved.fill_checkpoints[0].accounted_filled_size == Decimal("2")


@pytest.mark.asyncio
async def test_tracker_never_applies_unknown() -> None:
    tracker, state = make_tracker(Mode.LIVE)
    outcome = await tracker.handle_order_result(unknown_result(), market_end_at=END_AT)
    assert outcome.unknown_outcome is True
    assert await state.get_positions() == []
```

Also cover duplicate no-op, dry-run `SIMULATED`, close-to-zero, and stable `PositionAccountingError` propagation.

- [ ] **Step 3: Verify red tests**

Run: `.venv/bin/pytest tests/test_submitter.py tests/test_order_tracker.py tests/test_execution_router.py -q -p no:cacheprovider`

- [ ] **Step 4: Implement tracking outcome and immediate persistence**

```python
class TrackingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fill_applied: bool = False
    position_closed: bool = False
    unknown_outcome: bool = False
    accounting_error: str | None = None
    fill_application: FillApplication | None = None
```

`handle_order_result(result, *, market_end_at)` always updates order state and execution heartbeat. It applies eligible fills, saves immediately on a changed checkpoint/lifecycle, and returns a typed outcome. Change dry-run submission to set full `filled_size` and `avg_fill_price` while retaining status `SIMULATED`.

- [ ] **Step 5: Halt exactly once on unknown/accounting error**

The router latches one kill-switch reason and one event for `unknown_order_outcome:<client_id>` or `position_accounting_error:<reason>`. It never releases an exit reservation for unknown outcomes. Add typed optional `quantity`, `price`, and `pnl` fields plus `POSITION_UPDATED` and `POSITION_CLOSED` event types.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest tests/test_submitter.py tests/test_order_tracker.py tests/test_execution_router.py tests/test_state_store.py -q -p no:cacheprovider
git add execution/submitter.py execution/tracker.py execution/router.py models/events.py tests/test_submitter.py tests/test_order_tracker.py tests/test_execution_router.py
git commit -m "feat: apply and persist confirmed live and paper fills"
```

---

### Task 5: Reconciliation Grace for Confirmed Fills

**Files:** Modify `state/reconciliation.py`, `models/position.py`, and `tests/test_reconciliation.py`.

**Interfaces:** Consumes confirmation deadlines from Task 2. Produces `PositionMergeResult` and `ReconciliationReport.deferred_positions`.

- [ ] **Step 1: Write failing matching, deferred, expired, and zero tests**

```python
@pytest.mark.asyncio
async def test_reconciliation_preserves_confirmed_local_fill_during_grace() -> None:
    state = state_with_pending_buy(quantity="2", deadline=NOW + timedelta(seconds=30))
    report = await service(state, remote_positions=[], now=lambda: NOW).reconcile_runtime()
    assert report.ok is True
    assert report.deferred_positions == ["m1:t1"]
    assert (await state.get_position("m1", "t1")).quantity == Decimal("2")


@pytest.mark.asyncio
async def test_reconciliation_fails_after_confirmation_grace() -> None:
    state = state_with_pending_buy(quantity="2", deadline=NOW - timedelta(seconds=1))
    report = await service(state, remote_positions=[], now=lambda: NOW).reconcile_runtime()
    assert report.ok is False
    assert report.errors == ["position_confirmation_timeout:m1:t1"]


@pytest.mark.asyncio
async def test_absent_remote_confirms_local_sell_to_zero() -> None:
    state = state_with_closed_pending_sell(deadline=NOW + timedelta(seconds=30))
    report = await service(state, remote_positions=[], now=lambda: NOW).reconcile_runtime()
    assert report.ok is True
    assert (await state.get_position_lifecycle("m1", "t1")).confirmation_deadline is None
```

- [ ] **Step 2: Verify red tests**

Run: `.venv/bin/pytest tests/test_reconciliation.py -q -p no:cacheprovider`

- [ ] **Step 3: Implement deterministic merging**

```python
class PositionMergeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deferred_keys: list[str] = Field(default_factory=list)
    expired_keys: list[str] = Field(default_factory=list)
```

Add `InMemoryStateStore.merge_authoritative_positions(remote, *, now)`. Compare every union key by exact quantity. Preserve local only while a confirmation deadline is active; clear the deadline on equality, including local zero versus remote absence; replace every non-pending key with remote truth. Return sorted stable keys.

- [ ] **Step 4: Integrate reports**

Use the merge result in runtime reconciliation. Add deferred keys to `ReconciliationReport` without failing it. Convert expired keys to `position_confirmation_timeout:<key>` errors. Startup remains authoritative before WebSocket/order processing starts.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/test_reconciliation.py tests/test_runtime.py tests/test_bootstrap.py -q -p no:cacheprovider
git add state/reconciliation.py models/position.py state/store.py tests/test_reconciliation.py
git commit -m "feat: reconcile confirmed fills through API lag"
```

---

### Task 6: Pure Position Exit Policy

**Files:** Create `portfolio/exit_policy.py` and `tests/test_exit_policy.py`.

**Interfaces:** Consumes config, position, lifecycle, snapshot, and exit-reason models. Produces `ExitDecision` and pure `PositionExitPolicy.evaluate`.

- [ ] **Step 1: Write failing trigger and priority tests**

```python
def test_take_profit_uses_executable_best_bid() -> None:
    decision = policy(take_profit_bps="300").evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(opened_at=NOW - timedelta(seconds=30)),
        snapshot=snapshot(best_bid="0.515", mid="0.52"), now=NOW,
    )
    assert decision.reason == ExitReason.TAKE_PROFIT
    assert decision.return_bps == Decimal("300")


def test_market_expiry_has_priority_over_take_profit() -> None:
    decision = policy().evaluate(
        position=position(quantity="2", average="0.50"),
        lifecycle=lifecycle(market_end_at=NOW + timedelta(seconds=60)),
        snapshot=snapshot(best_bid="0.60"), now=NOW,
    )
    assert decision.reason == ExitReason.MARKET_EXPIRY
```

Add separate boundary tests for stop loss at -200 bps, take profit at +300 bps, max hold at 180 seconds, one unit inside each threshold, pending exit, stale snapshot, entry price zero, and sub-minimum quantity returning dust.

- [ ] **Step 2: Verify red tests**

Run: `.venv/bin/pytest tests/test_exit_policy.py -q -p no:cacheprovider`

- [ ] **Step 3: Implement exact pure interface**

```python
class ExitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    should_exit: bool
    reason: ExitReason | None = None
    requested_size: Decimal = Decimal("0")
    return_bps: Decimal | None = None
    dust: bool = False
    explanation: str


class PositionExitPolicy:
    def __init__(self, config: PositionManagementConfig, *, min_order_size: Decimal, max_data_age_seconds: float) -> None: pass
    def evaluate(self, *, position: Position, lifecycle: PositionLifecycle, snapshot: MarketSnapshot | None, now: datetime) -> ExitDecision: pass
```

Calculate return as `((best_bid - average_entry_price) / average_entry_price) * Decimal("10000")`. Evaluate in exact priority: market expiry, stop loss, take profit, max hold. Return stable explanation codes.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/test_exit_policy.py -q -p no:cacheprovider
git add portfolio/exit_policy.py tests/test_exit_policy.py
git commit -m "feat: add deterministic position exit policy"
```

---

### Task 7: Exit Coordinator, Reduce-Only Risk, and Routing

**Files:** Create `portfolio/exit_manager.py` and `tests/test_exit_manager.py`; modify `risk/pretrade.py`, `execution/order_builder.py`, `execution/router.py`, `tests/test_risk_pretrade.py`, `tests/test_order_builder.py`, and `tests/test_execution_router.py`.

**Interfaces:** Consumes Tasks 1–6. Produces snapshot/timer/strategy exit conversion and reservation-aware routing.

- [ ] **Step 1: Write failing coordinator tests**

```python
@pytest.mark.asyncio
async def test_take_profit_emits_one_reserved_full_position_exit() -> None:
    state = state_with_position(quantity="2.5", average="0.40")
    manager = make_exit_manager(state=state, now=lambda: NOW)
    first = await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)
    second = await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)
    assert len(first) == 1 and second == []
    assert first[0].requested_size == Decimal("2.5")
    assert first[0].reduce_only is True
    assert first[0].time_in_force == OrderTimeInForce.IOC


@pytest.mark.asyncio
async def test_rejected_exit_waits_two_seconds_before_retry() -> None:
    state, manager = reserved_exit_manager(last_attempt_at=NOW)
    await state.release_exit("m1", "t1", client_order_id="exit-order-0001")
    assert await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT) == []
    manager.set_clock(lambda: NOW + timedelta(seconds=2))
    assert len(await manager.on_market_update(snapshot(best_bid="0.42"), market_end_at=END_AT)) == 1
```

Also test dust, three-attempt exhaustion, strategy SELL conversion only with inventory, and no duplicate reservation across concurrent `asyncio.gather` calls.

- [ ] **Step 2: Write failing risk and builder tests**

```python
@pytest.mark.asyncio
async def test_reduce_only_sell_requires_exact_inventory() -> None:
    decision = await engine_with_position("2").evaluate(
        signal=exit_signal(size="2"), snapshot=fresh_snapshot(),
        proposed_size=Decimal("2"), proposed_price=Decimal("0.50"),
    )
    assert decision.approved is True


def test_exit_signal_overrides_entry_fok_with_ioc() -> None:
    order = live_builder().build(signal=exit_signal(size="2"), snapshot=fresh_snapshot())
    assert order.time_in_force == OrderTimeInForce.IOC
```

Cover reduce-only BUY rejection, size above inventory, pending-exit BUY rejection, and cap-limited exits that never increase size.

- [ ] **Step 3: Verify red tests**

Run: `.venv/bin/pytest tests/test_exit_manager.py tests/test_risk_pretrade.py tests/test_order_builder.py tests/test_execution_router.py -q -p no:cacheprovider`

- [ ] **Step 4: Implement manager interface**

```python
class PositionExitManager:
    def __init__(self, *, config: AppConfig, state_store: InMemoryStateStore, snapshots: SnapshotStore, policy: PositionExitPolicy, now: Callable[[], datetime] = utc_now) -> None: pass
    async def on_market_update(self, snapshot: MarketSnapshot, *, market_end_at: datetime | None) -> list[TradeSignal]: pass
    async def from_strategy_signal(self, signal: TradeSignal, *, snapshot: MarketSnapshot, market_end_at: datetime | None) -> TradeSignal | None: pass
    async def on_timer(self, *, market_end_lookup: Callable[[str], datetime | None]) -> list[TradeSignal]: pass
```

Generate the signal ID before reserving and derive the exact future client order ID using the existing prefix/truncation rule. Store reason as `position_exit:<reason>`. Save a snapshot immediately after each reservation and release so a restart cannot forget an in-flight exit.

- [ ] **Step 5: Update routing contracts**

Extend `ExecutionRouter.route_signal` with keyword-only `market_end_at: datetime | None = None`; pass that value to `OrderTracker.handle_order_result`. Use `signal.requested_size or fixed_size(self._config.execution)`. Use `signal.time_in_force or config.execution.time_in_force`. Every live/dry SELL stays inventory-reducing. Release reservations after definite local/risk/exchange rejection or after a confirmed delta is durably saved. Keep unknown reservations and halt. Add `EXIT_TRIGGERED` and `POSITION_DUST` events. Halt after three attempts without quantity reduction using `exit_attempts_exhausted:<market>:<token>`.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest tests/test_exit_manager.py tests/test_risk_pretrade.py tests/test_order_builder.py tests/test_execution_router.py tests/test_order_tracker.py -q -p no:cacheprovider
git add portfolio/exit_manager.py risk/pretrade.py execution/order_builder.py execution/router.py tests/test_exit_manager.py tests/test_risk_pretrade.py tests/test_order_builder.py tests/test_execution_router.py
git commit -m "feat: route reserved reduce-only position exits"
```

---

### Task 8: Runtime, Immediate Reconciliation, and Rotation Wiring

**Files:** Modify `app/bootstrap.py`, `app/runtime.py`, `clients/market_rotation.py`, `app/shutdown.py`, `tests/test_bootstrap.py`, `tests/test_runtime.py`, and `tests/test_market_rotation.py`.

**Interfaces:** Consumes prior task services. Produces exit-first snapshot handling, post-fill reconciliation, timer exits, and safe rotation gating.

- [ ] **Step 1: Write failing bootstrap-order tests**

```python
@pytest.mark.asyncio
async def test_snapshot_routes_exit_before_entry_strategy() -> None:
    services, calls = await boot_with_recorders()
    await services.market_data_client._on_snapshot(fresh_snapshot())
    assert calls[:2] == ["exit_manager", "strategy"]


@pytest.mark.asyncio
async def test_confirmed_live_fill_reconciles_immediately() -> None:
    services, reconciliation = await boot_with_filled_submitter()
    await services.router.route_signal(buy_signal(), snapshot=fresh_snapshot())
    assert reconciliation.runtime_calls == 1
```

- [ ] **Step 2: Write failing timer/end tests**

Assert one housekeeping iteration routes `position_exit:max_hold`. Assert sellable inventory at `market_end_at` latches `position_open_at_market_end:m1:t1`. Assert deferred post-fill reconciliation emits `POSITION_CONFIRMATION_DEFERRED` without halting; a failed report latches `post_fill_reconciliation_failed`.

- [ ] **Step 3: Write failing rotation-gate tests**

Inject `can_rotate(current_market)`. `False` keeps old asset IDs and sets `DEGRADED/position_exit_pending`; later `True` rotates once; reaching `end_at` while false raises `RuntimeError("position_open_at_market_end")`.

- [ ] **Step 4: Verify red tests**

Run: `.venv/bin/pytest tests/test_bootstrap.py tests/test_runtime.py tests/test_market_rotation.py -q -p no:cacheprovider`

- [ ] **Step 5: Reorder bootstrap without callback cycles**

Construct reconciliation, tracker, exit policy, exit manager, router, market callback, WebSocket, then rotator. Inject `reconciliation.reconcile_runtime` into the router as `post_fill_reconcile: Callable[[], Awaitable[ReconciliationReport]]`. Add exit manager to `AppServices`. The snapshot callback obtains the matching current market `end_at`, passes it to every `route_signal` call, routes automatic exits first, then converts strategy SELL through `from_strategy_signal`, then routes entry BUY.

- [ ] **Step 6: Wire timer and rotation safety**

Housekeeping evaluates timer exits before routine reconciliation/risk. Rotation refuses a non-dust ending-market position. Emit `POSITION_CONFIRMATION_DEFERRED` for grace-period reports. Shutdown writes a final snapshot after tasks stop and before clients close.

- [ ] **Step 7: Verify and commit**

```bash
.venv/bin/pytest tests/test_bootstrap.py tests/test_runtime.py tests/test_market_rotation.py tests/test_live_kill_switch.py -q -p no:cacheprovider
git add app/bootstrap.py app/runtime.py clients/market_rotation.py app/shutdown.py tests/test_bootstrap.py tests/test_runtime.py tests/test_market_rotation.py
git commit -m "feat: supervise exits through runtime and rollover"
```

---

### Task 9: Dashboard Exit and Lifecycle Telemetry

**Files:** Modify `dashboard/models.py`, `dashboard/read_model.py`, `dashboard/templates/index.html`, `dashboard/static/dashboard.js`, `dashboard/static/dashboard.css`, `tests/test_dashboard_read_model.py`, `tests/test_dashboard_ui.py`, and `tests/test_dashboard_api.py`.

**Interfaces:** Consumes active positions, lifecycle records, and typed lifecycle events. Produces `ManagedPositionView` and bounded warnings.

- [ ] **Step 1: Write failing read-model tests**

```python
@pytest.mark.asyncio
async def test_dashboard_exposes_managed_exit_state() -> None:
    state = await read_model_for_position(
        quantity="2", average="0.40", mark="0.44",
        opened_at=NOW - timedelta(seconds=30), end_at=NOW + timedelta(minutes=5),
        pending_exit="exit-order-0001", reason=ExitReason.TAKE_PROFIT,
    ).build()
    managed = state.managed_positions[0]
    assert managed.return_bps == Decimal("1000")
    assert managed.held_seconds == 30
    assert managed.exit_pending is True


@pytest.mark.asyncio
async def test_dashboard_warns_about_dust() -> None:
    state = await read_model_for_position(quantity="0.5", minimum="1").build()
    assert "position_dust:m1:t1:0.5" in state.warnings
```

- [ ] **Step 2: Write failing UI tests**

Assert table IDs/headers exist for return, held time, deadline, and exit status. Assert JavaScript uses `textContent`/`replaceChildren`, shows `Monitoring`, `Exit pending`, `Awaiting account confirmation`, `Dust`, and `Closed`, and introduces no `innerHTML`.

- [ ] **Step 3: Verify red tests**

Run: `.venv/bin/pytest tests/test_dashboard_read_model.py tests/test_dashboard_ui.py tests/test_dashboard_api.py -q -p no:cacheprovider`

- [ ] **Step 4: Implement secret-free view model**

```python
class ManagedPositionView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Position
    opened_at: datetime | None = None
    held_seconds: float | None = Field(default=None, ge=0)
    market_end_at: datetime | None = None
    return_bps: Decimal | None = None
    exit_pending: bool = False
    exit_reason: ExitReason | None = None
    exit_attempt_count: int = Field(default=0, ge=0)
    confirmation_deferred: bool = False
    dust: bool = False
```

Add active managed positions and the newest 20 closed lifecycle records to `DashboardState`. Compute return from the current executable mark. Keep all account and credential identifiers out of JSON.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/test_dashboard_read_model.py tests/test_dashboard_ui.py tests/test_dashboard_api.py -q -p no:cacheprovider
git add dashboard/models.py dashboard/read_model.py dashboard/templates/index.html dashboard/static/dashboard.js dashboard/static/dashboard.css tests/test_dashboard_read_model.py tests/test_dashboard_ui.py tests/test_dashboard_api.py
git commit -m "feat: show position exits and lifecycle in dashboard"
```

---

### Task 10: End-to-End Lifecycle and Release Gates

**Files:** Create `tests/test_position_lifecycle_e2e.py`; modify `README.md` and `docs/live-runbook.md`.

**Interfaces:** Consumes the complete implementation. Produces deterministic entry/exit evidence and the operator recovery runbook.

- [ ] **Step 1: Write deterministic paper entry-to-exit test**

```python
@pytest.mark.asyncio
async def test_dry_run_buy_then_take_profit_closes_position() -> None:
    harness = await DryRunLifecycleHarness.start(take_profit_bps=Decimal("300"))
    await harness.publish(snapshot(mid="0.50", bid="0.49", ask="0.50"))
    await harness.route(buy_signal())
    opened = await harness.state.get_position("m1", "t1")
    assert opened is not None and opened.quantity > 0

    await harness.publish(snapshot(mid="0.52", bid="0.515", ask="0.52"))
    assert await harness.state.get_position("m1", "t1") is None
    lifecycle_events = [event.event_type for event in harness.journal.events if event.event_type in {
        EventType.POSITION_UPDATED, EventType.EXIT_TRIGGERED, EventType.POSITION_CLOSED,
    }]
    assert lifecycle_events == [
        EventType.POSITION_UPDATED, EventType.EXIT_TRIGGERED, EventType.POSITION_CLOSED,
    ]
```

- [ ] **Step 2: Write partial and unknown integration tests**

The partial test returns cumulative FAK fills of 1.00 then 2.00 for a 2.00 position and asserts the second attempt requests exactly the remaining 1.00. The unknown test asserts one submission, no inventory mutation, retained reservation, kill switch active, and no retry across later snapshots/timers.

- [ ] **Step 3: Update documentation with exact operations**

Document entry FOK versus exit IOC/FAK; five exit reasons and priority; initial thresholds; partial and dust behavior; confirmation grace; every new halt reason; and the fact that neither order type guarantees counterparties. Require returning to dry run and repeating preflight after unknown/accounting halts.

- [ ] **Step 4: Run all focused lifecycle tests**

```bash
.venv/bin/pytest \
  tests/test_position_models.py tests/test_position_accounting.py \
  tests/test_snapshots.py tests/test_submitter.py tests/test_order_tracker.py \
  tests/test_reconciliation.py tests/test_exit_policy.py tests/test_exit_manager.py \
  tests/test_risk_pretrade.py tests/test_order_builder.py tests/test_execution_router.py \
  tests/test_bootstrap.py tests/test_runtime.py tests/test_market_rotation.py \
  tests/test_dashboard_read_model.py tests/test_dashboard_ui.py tests/test_dashboard_api.py \
  tests/test_position_lifecycle_e2e.py -q -p no:cacheprovider
```

Expected: every named test passes.

- [ ] **Step 5: Run repository-wide gates**

```bash
git diff --check
PYTHONPYCACHEPREFIX=/private/tmp/bot-v2-position-lifecycle-pycache \
  .venv/bin/python -m compileall -q \
  app clients config dashboard execution models notifications persistence portfolio risk state strategies scripts backtest tests
.venv/bin/pytest -q -p no:cacheprovider
```

Expected: no diff errors, compile exit 0, and full suite pass.

- [ ] **Step 6: Run external release gates without live writes**

```bash
.venv/bin/python -m scripts.live_preflight --config-dir config
.venv/bin/python -m app.main
```

Keep effective mode `dry_run`, `allow_live_trading=false`, and `dry_run_force=true`. Cross one complete BTC 15-minute rollover. Required evidence:

- all nine read-only preflight checks pass;
- WebSocket transport stays fresh through rotation;
- a paper BUY creates inventory;
- a deterministic exit trigger submits simulated IOC and closes/reduces it;
- no unknown, accounting, confirmation, duplicate-exit, oversell, or market-end halt occurs;
- dashboard displays opening, exit reason, and closure.

Do not enable live flags or place a real order during verification.

- [ ] **Step 7: Commit integration coverage and runbook**

```bash
git add README.md docs/live-runbook.md tests/test_position_lifecycle_e2e.py
git commit -m "docs: add position lifecycle release runbook"
```

---

## Final Reviewer Checklist

- [ ] Cumulative fills are idempotent and Decimal-only.
- [ ] Dry run creates and closes paper positions through production routing/tracking.
- [ ] Confirmed live inventory is usable immediately.
- [ ] Data API lag is tolerated only inside 30 seconds.
- [ ] Exit sizing never exceeds confirmed remaining inventory.
- [ ] Entry FOK and exit IOC/FAK are independently selected.
- [ ] Definite failures retry only after two seconds and at most three times.
- [ ] Unknown outcomes never retry and visibly halt.
- [ ] Market expiry blocks unsafe rotation and halts if sellable inventory remains.
- [ ] Live startup still excludes resolved snapshot positions.
- [ ] Snapshot writes are atomic and fill/lifecycle changes persist immediately.
- [ ] Dashboard and events contain no secrets or raw upstream payloads.
- [ ] Full tests, compile, diff, preflight, rollover, and paper entry/exit evidence exist before live enablement.
