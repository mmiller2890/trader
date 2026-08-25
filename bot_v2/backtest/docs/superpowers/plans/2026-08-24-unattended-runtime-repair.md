# Unattended Runtime Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair unattended runtime supervision, durable incident safety, lease-authorized restart behavior, and qualification accounting.

**Architecture:** Keep `BotRuntime` as the owner of task supervision and safety ordering, while `DashboardController` owns the distinction between operator commands and process lifecycle. Reuse `OperationsRepository`, `LiveLeaseService`, `SnapshotStore`, and the existing durable event/outbox pipeline rather than adding another orchestration subsystem.

**Tech Stack:** Python 3.11+, asyncio, Pydantic 2, SQLite, FastAPI lifespan, pytest, pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-24-unattended-runtime-repair-design.md`

## Global Constraints

- No restart or command-line flag creates fresh live authorization.
- A safety halt persists its incident and revokes its lease before completion.
- Ordinary process shutdown preserves a valid lease.
- Operator stop, emergency halt, safety halt, and fatal supervision revoke the lease.
- Upstream exception messages and secrets are not persisted.
- Each production repair follows a red-green test cycle.

---

### Task 1: Correct loop and supervisor wiring

**Files:**
- Modify: `bot_v2/app/loops.py`
- Modify: `bot_v2/app/runtime.py`
- Test: `bot_v2/tests/test_runtime_loops.py`
- Test: `bot_v2/tests/test_runtime.py`

**Interfaces:**
- Consumes: `TaskSpec.factory(stop_event, heartbeat)`.
- Produces: default loop specs that forward the real heartbeat; a runtime-owned fatal monitor that terminates `wait()` with `FAILED`.

- [ ] Add a regression test asserting `_cycle` invokes its heartbeat exactly once without an unawaited coroutine.
- [ ] Run the targeted test and observe the current double-call/warning failure.
- [ ] Change `_cycle` to one `await heartbeat()` call.
- [ ] Add a regression test that starts every default loop spec with correctly typed arguments and observes a supervisor heartbeat.
- [ ] Run it and observe the health-loop `AttributeError` or unchanged heartbeat.
- [ ] Replace the mixed two-argument loop adapter with `TaskSpec` factories that forward `(services, stop_event, heartbeat, reporter)` correctly.
- [ ] Add a runtime regression test where a crashing critical loop exhausts its budget and `runtime.wait()` returns `FAILED`.
- [ ] Run it and observe the current timeout.
- [ ] Add a runtime-owned monitor for `supervisor.wait_fatal()` that applies fatal safety ordering, sets `FAILED`, and sets `_terminal_event`; cancel it during intentional shutdown.
- [ ] Run `pytest -q tests/test_runtime_loops.py tests/test_runtime.py tests/test_supervisor.py`.

### Task 2: Persist incidents and build real recovery context

**Files:**
- Modify: `bot_v2/app/runtime.py`
- Modify: `bot_v2/persistence/operations.py`
- Test: `bot_v2/tests/test_runtime.py`
- Test: `bot_v2/tests/test_operations_repository.py`

**Interfaces:**
- Consumes: `OperationalIncident`, `RecoveryContext`, `OperationsRepository`.
- Produces: `handle_incident()` that returns its real `RecoveryAction`, persists stable observations, and performs durable halt ordering.

- [ ] Add a regression test proving an accounting incident reaches `HALTED` without `NameError`, is persisted, snapshots the kill switch, revokes the lease, and is available to clear-halt readers.
- [ ] Run it and observe the current `RecoveryContext` failure.
- [ ] Import `RecoveryContext`; add runtime tracking for first observation and counts; obtain disk percentage from the configured data directory; pass supervisor crash count for fatal decisions.
- [ ] Add repository behavior that records repeated fingerprints under a stable unresolved incident and increments `consecutive_count` while preserving the first incident identity.
- [ ] Add a regression test for repeated incident persistence and run it red.
- [ ] Implement the transactional fingerprint upsert and run it green.
- [ ] Make `_make_reporter` and `_supervised_incident_handler` return the actual policy action.
- [ ] Persist and enqueue the incident before halt side effects; revoke the active live lease before latching/snapshotting/cancelling.
- [ ] Run `pytest -q tests/test_runtime.py tests/test_operations_repository.py tests/test_intervention_recovery.py tests/test_reliability_policy.py`.

### Task 3: Connect manual leases and automatic dashboard resume

**Files:**
- Modify: `bot_v2/dashboard/controller.py`
- Modify: `bot_v2/dashboard/app.py`
- Modify: `bot_v2/app/main.py`
- Test: `bot_v2/tests/test_dashboard_controller.py`
- Test: `bot_v2/tests/test_dashboard_api.py`
- Test: `bot_v2/tests/test_app_main.py`
- Test: `bot_v2/tests/test_live_lease.py`

**Interfaces:**
- Produces: `DashboardController.resume_on_startup()`, `DashboardController.shutdown_process()`, and lease-aware operator `start()`, `stop()`, and `halt()`.
- Consumes: `LiveLeaseService.validate_for_resume()`, `LiveLeaseService.issue()`, persisted snapshot kill-switch state, and runtime bootstrap safety checks.

- [ ] Add controller tests proving successful manual live start issues a lease and operator stop/halt revokes it.
- [ ] Run them red.
- [ ] Construct the controller lease service from the existing SQLite repository and issue/revoke leases at the operator boundaries.
- [ ] Add startup-resume tests for valid lease, missing lease, expired lease, fingerprint mismatch, latched snapshot, and runtime bootstrap failure.
- [ ] Run them red.
- [ ] Implement `resume_on_startup()` so only an already-authorized live configuration can start; rejection remains stopped and emits a sanitized durable event.
- [ ] Add a lifespan test proving startup calls resume and shutdown uses the lease-preserving process path.
- [ ] Update FastAPI lifespan to call `resume_on_startup()` and `shutdown_process()`.
- [ ] Add CLI tests proving `--resume-live` validates rather than authorizes and `--live` remains explicit fresh authorization.
- [ ] Thread resume intent into `app.main.run()` without allowing it to create a lease.
- [ ] Run `pytest -q tests/test_dashboard_controller.py tests/test_dashboard_api.py tests/test_app_main.py tests/test_live_lease.py`.

### Task 4: Exercise real qualification accounting

**Files:**
- Modify: `bot_v2/scripts/reliability_soak.py`
- Test: `bot_v2/tests/test_unattended_fault_injection.py`

**Interfaces:**
- Consumes: async `InMemoryStateStore.apply_confirmed_fill()`.
- Produces: accelerated cycles whose accounting counters correspond to completed state mutations.

- [ ] Add a regression assertion that one market cycle creates a closed lifecycle/realized P&L through actual state mutation.
- [ ] Run it and observe empty state plus unawaited-coroutine warnings.
- [ ] Await both fill applications and increment accounting counters only after successful completion.
- [ ] Run `pytest -q tests/test_unattended_fault_injection.py -W error::RuntimeWarning`.

### Task 5: Full verification and cleanup

**Files:**
- Modify only files required by failures found above.

**Interfaces:**
- Produces: warning-free verified branch state.

- [ ] Run `python -m compileall` over project source while excluding `.venv` and caches.
- [ ] Run `pytest -q -p no:cacheprovider -W error::RuntimeWarning`.
- [ ] Run `git diff --check` and inspect `git diff --stat` plus `git status --short`.
- [ ] Confirm no unrelated files or secrets were added.
