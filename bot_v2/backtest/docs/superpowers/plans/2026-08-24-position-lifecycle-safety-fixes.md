# Position Lifecycle Safety Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every validated live-safety and recovery defect found in the position lifecycle implementation review.

**Architecture:** Keep `InMemoryStateStore.apply_confirmed_fill` as the only inventory mutation boundary. Reconciliation will return confirmed terminal order updates through `OrderTracker`, adopted exchange positions will receive durable lifecycle metadata, and snapshot persistence will cover every state needed to fail closed across restarts. Risk will use a durable realized-P&L ledger and lifecycle-aware entry checks.

**Tech Stack:** Python 3.11, asyncio, Pydantic 2, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-24-position-lifecycle-exit-management-design.md`

## Global Constraints

- Preserve all existing uncommitted dashboard and live-path work.
- Never submit an exchange order or use live credentials during verification.
- Only confirmed cumulative fill deltas may mutate inventory.
- Unknown outcomes remain halted until explicit operator action; restart must not clear them.
- Every production behavior change starts with a regression test that fails for the reviewed defect.

---

### Task 1: Account delayed fills and adopt live positions safely

**Files:**
- Modify: `state/reconciliation.py`
- Modify: `state/store.py`
- Modify: `execution/tracker.py`
- Modify: `app/bootstrap.py`
- Test: `tests/test_reconciliation.py`
- Test: `tests/test_order_tracker.py`

**Interfaces:**
- `ReconciliationService` receives an optional confirmed-order callback compatible with `OrderTracker.handle_order_result`.
- `InMemoryStateStore.merge_authoritative_positions(..., adopted_at, market_end_lookup)` creates lifecycle metadata for remote-only positions.

- [ ] Write a failing test proving a locally submitted order that later reconciles as `FILLED` updates inventory through the accounting checkpoint exactly once.
- [ ] Run the targeted test and confirm inventory remains unchanged before the fix.
- [ ] Route confirmed `FILLED` and `PARTIALLY_FILLED` reconciliation results through the tracker; keep cancellation/rejection as status-only updates.
- [ ] Run the targeted reconciliation and tracker tests until green.
- [ ] Write a failing test proving startup and runtime remote-only positions receive a lifecycle and are visible to `PositionExitManager`.
- [ ] Run the test and confirm the current lifecycle is `None`.
- [ ] Create lifecycle metadata during authoritative adoption, attach the discovered market end when identifiable, and return `position_market_window_unknown` in live mode when sellable inventory cannot be mapped.
- [ ] Run reconciliation, exit-manager, bootstrap, and rotation tests until green.

### Task 2: Make safety state durable across restart

**Files:**
- Modify: `models/position.py`
- Modify: `state/store.py`
- Modify: `persistence/snapshots.py`
- Modify: `execution/router.py`
- Modify: `portfolio/exit_manager.py`
- Test: `tests/test_snapshots.py`
- Test: `tests/test_execution_router.py`

**Interfaces:**
- `StateSnapshot` persists kill state, active lifecycles, bounded closed lifecycles, and realized-P&L ledger entries.
- `ExecutionRouter` receives `SnapshotStore` and saves after successful reservation release.

- [ ] Write failing tests proving kill state, closed history, and a released reservation survive a save/restore cycle correctly.
- [ ] Run the tests and confirm kill state is cleared, history is empty, and the stale reservation returns.
- [ ] Restore kill state and reason, serialize/restore bounded closed lifecycle records, and expose explicit state-store restore methods.
- [ ] Save a snapshot after each successful release and after any persisted safety mutation.
- [ ] Run snapshot, router, exit-manager, and shutdown tests until green.

### Task 3: Enforce risk invariants for exits and losses

**Files:**
- Modify: `state/store.py`
- Modify: `risk/pretrade.py`
- Modify: `risk/runtime.py`
- Modify: `dashboard/read_model.py`
- Test: `tests/test_risk_pretrade.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_dashboard_read_model.py`

**Interfaces:**
- `InMemoryStateStore.get_realized_pnl_total()` returns durable closed realized P&L for the running session.
- Pretrade risk adds a lifecycle-aware `pending_exit` check that rejects BUY while an exit is reserved.

- [ ] Write a failing test proving BUY is rejected while the same token has a pending exit.
- [ ] Run it and confirm the current duplicate guard approves the BUY.
- [ ] Add the pending-exit check without changing reduce-only SELL behavior.
- [ ] Write a failing test proving an $8 closed loss breaches a $1 daily-loss limit.
- [ ] Run it and confirm the current check passes.
- [ ] Accumulate closed realized P&L durably and include it once—without double-counting active-position P&L—in runtime risk and dashboard totals.
- [ ] Run risk, accounting, snapshot, and dashboard tests until green.

### Task 4: Reset lifecycle state on re-entry and honor configuration

**Files:**
- Modify: `state/store.py`
- Modify: `portfolio/exit_manager.py`
- Modify: `portfolio/exit_policy.py`
- Modify: `config/risk.yaml`
- Test: `tests/test_position_accounting.py`
- Test: `tests/test_exit_manager.py`
- Test: `tests/test_config.py`

**Interfaces:**
- A BUY into a key whose previous lifecycle is closed creates a fresh active lifecycle while retaining the immutable closed record.
- `position_management.enabled=false` disables managed policy/strategy exits.
- `liquidate_full_position=false` preserves the configured fixed-size attempt, capped by inventory; `true` requests full remaining inventory.

- [ ] Write a failing close/re-entry test asserting new `opened_at`, cleared close/exit metadata, zero attempts, and retained closed history.
- [ ] Run it and confirm the closed fields remain on the reopened lifecycle.
- [ ] Create a fresh lifecycle on re-entry and keep the prior record only in closed history.
- [ ] Write failing tests for both position-management toggles.
- [ ] Wire `enabled` at the manager boundary and use `liquidate_full_position` when selecting requested exit size.
- [ ] Restore `max_data_staleness_seconds` to the approved 15-second default and update behavior-based config coverage.
- [ ] Run accounting, exit-policy, exit-manager, configuration, and end-to-end lifecycle tests until green.

### Task 5: Full verification and review

**Files:**
- Review all modified production and test files.

- [ ] Run every targeted test file changed above.
- [ ] Run `.venv/bin/python -m pytest -q` and require zero failures.
- [ ] Run compilation with `PYTHONPYCACHEPREFIX` directed to `/private/tmp`.
- [ ] Run `git diff --check` for both the committed feature range and current working tree.
- [ ] Inspect `git diff --stat`, `git status --short`, and the final patch for unrelated edits or credential leakage.
- [ ] Report the exact verification output and any remaining operational limitation without claiming exchange fills are guaranteed.
