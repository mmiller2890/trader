# Multi-Day Unattended Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot safely operate for at least 72 hours across recurring BTC 15-minute markets, automatically recover from classified transient failures, and durably notify the operator through Telegram when human intervention is required.

**Architecture:** Add a typed reliability layer around the existing runtime: separately supervised loops report structured incidents to one fault policy, runtime state pauses entries while degraded, safety faults revoke a time-limited live lease and latch the kill switch, and alerts flow through a SQLite outbox to Telegram. External process supervision handles fatal process failures, while bounded retention and explicit liveness/readiness signals prevent silent task death and unbounded resource growth.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, SQLite (`sqlite3` through `asyncio.to_thread`), httpx, FastAPI, pytest/pytest-asyncio, existing `py-clob-client-v2==1.1.0`, Docker/systemd.

**Spec:** `backtest/docs/superpowers/specs/2026-08-24-multi-day-unattended-operations-design.md`

## Global Constraints

- Preserve all existing live-safety guards: dry-run defaults, exact operator confirmations, kill-switch behavior, preflight, reconciliation, reduce-only exits, and idempotent fill accounting.
- Telegram is the required first notification channel; do not add SMTP/email in this implementation.
- Issuing a live operating lease requires a successful Telegram test alert within the preceding five minutes; auto-resume requires a writable outbox but tolerates temporary Telegram unavailability.
- Never automatically clear a kill switch or resume after a safety fault.
- Automatic live resume requires an active unexpired lease, unchanged configuration fingerprint, inactive persisted kill switch, and a completely fresh successful live preflight plus startup reconciliation.
- No BUY may be submitted in `DEGRADED`, `HALTING`, `HALTED`, or `FAILED` state.
- Notification delivery failure must never block risk, reconciliation, exit, persistence, or shutdown logic.
- Persist no credentials or secret values in leases, incidents, health files, journals, or outbox rows.
- Use TDD for every task. Run the specified focused test before and after implementation, then run the full suite before each commit.
- Do not run live orders as part of automated verification. The 24-hour and 72-hour qualification stages begin in dry-run mode.
- Work in an isolated worktree when executing this plan. The current source worktree is dirty and must not be overwritten or cleaned.

---


### Task 1: Reliability Configuration and Operational Domain Models

**Files:**
- Create: `models/operations.py`
- Modify: `config/schema.py:200-242`
- Modify: `config/bot.yaml:68-72`
- Test: `tests/test_config.py`
- Create: `tests/test_operations_models.py`

**Interfaces:**
- Produces: `ReliabilityConfig`, extended `NotificationsConfig`, `OperationalState`, `IncidentCategory`, `IncidentSeverity`, `RecoveryAction`, `LeaseStatus`, `OperationalIncident`, `TaskHealth`, `LiveOperatingLease`, and `OutboxAlert`.
- Consumes: existing Pydantic conventions from `config/schema.py` and UTC timestamp conventions from `models/events.py`.

- [ ] **Step 1: Add failing configuration-default and validation tests**

Add to `tests/test_config.py`:

```python
def test_reliability_defaults_match_unattended_operations_spec() -> None:
    config = AppConfig()
    reliability = config.reliability
    assert reliability.live_lease_hours == 72
    assert reliability.task_restart_limit == 3
    assert reliability.task_restart_window_seconds == 600
    assert reliability.degraded_alert_after_seconds == 120
    assert reliability.authoritative_state_halt_after_seconds == 300
    assert reliability.rest_fallback_after_seconds == 30
    assert reliability.retry_initial_seconds == 1
    assert reliability.retry_max_seconds == 30
    assert reliability.retry_jitter_ratio == 0.20
    assert reliability.disk_warning_percent == 80
    assert reliability.disk_degraded_percent == 90
    assert reliability.disk_halt_percent == 95
    assert config.notifications.durable_outbox_enabled is True
    assert config.notifications.telegram_deduplication_seconds == 900
    assert config.notifications.alert_retry_max_seconds == 300


@pytest.mark.parametrize(
    "payload",
    [
        {"live_lease_hours": 0},
        {"live_lease_hours": 169},
        {"retry_initial_seconds": 31, "retry_max_seconds": 30},
        {"retry_jitter_ratio": 1.01},
        {"disk_warning_percent": 91, "disk_degraded_percent": 90},
        {"disk_degraded_percent": 96, "disk_halt_percent": 95},
    ],
)
def test_reliability_rejects_unsafe_bounds(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AppConfig(reliability=payload)
```

- [ ] **Step 2: Add failing model tests**

Create `tests/test_operations_models.py` with timezone and secret-surface assertions:

```python
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from models.operations import (
    IncidentCategory,
    IncidentSeverity,
    LeaseStatus,
    LiveOperatingLease,
    OperationalIncident,
    OperationalState,
)


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def test_live_lease_is_typed_and_contains_no_secret_fields() -> None:
    lease = LiveOperatingLease(
        lease_id="lease-12345678",
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=72),
        config_fingerprint="a" * 64,
        status=LeaseStatus.ACTIVE,
    )
    assert lease.status == LeaseStatus.ACTIVE
    assert set(lease.model_dump()) == {
        "lease_id", "issued_at", "expires_at", "config_fingerprint",
        "status", "revoked_at", "revocation_reason",
    }


def test_incident_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        OperationalIncident(
            incident_id="incident-12345678",
            fingerprint="reconciliation:data_api_timeout",
            component="reconciliation",
            category=IncidentCategory.TRANSIENT_TRANSPORT,
            severity=IncidentSeverity.WARNING,
            reason="data_api_timeout",
            first_seen_at=datetime(2026, 8, 24),
            last_seen_at=NOW,
        )


def test_operational_state_has_required_non_running_states() -> None:
    assert tuple(state.value for state in OperationalState) == (
        "stopped", "starting", "running", "degraded", "halting",
        "halted", "stopping", "failed",
    )
```

- [ ] **Step 3: Run the new tests and confirm they fail**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_config.py::test_reliability_defaults_match_unattended_operations_spec \
  tests/test_config.py::test_reliability_rejects_unsafe_bounds \
  tests/test_operations_models.py
```

Expected: FAIL because `ReliabilityConfig` and `models.operations` do not exist.

- [ ] **Step 4: Implement the config and models**

Add `ReliabilityConfig` to `config/schema.py` with the exact defaults from the spec and an `after` validator enforcing:

```python
if self.retry_max_seconds < self.retry_initial_seconds:
    raise ValueError("retry_max_seconds must be >= retry_initial_seconds")
if not (
    self.disk_warning_percent
    < self.disk_degraded_percent
    < self.disk_halt_percent
):
    raise ValueError("disk thresholds must be strictly increasing")
```

Add `reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)` to `AppConfig`. Extend `NotificationsConfig` with the six approved defaults. Copy both YAML sections verbatim from the spec into `config/bot.yaml`.

Implement `models/operations.py` using `ConfigDict(extra="forbid")` on every model. Use string enums with these exact values:

```python
class OperationalState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    HALTING = "halting"
    HALTED = "halted"
    STOPPING = "stopping"
    FAILED = "failed"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    DEGRADE = "degrade"
    HALT = "halt"
```

Define categories for `transient_transport`, `market_discovery`, `authoritative_state`, `account_divergence`, `authentication`, `compliance`, `funding`, `accounting`, `exit_safety`, `task_crash`, `persistence`, `disk`, and `notification`. Add `INFO`, `WARNING`, and `URGENT` severities. Validate all datetimes as timezone-aware with a shared field validator.

Use these exact model fields so later tasks do not invent incompatible payloads:

```python
class OperationalIncident(BaseModel):
    incident_id: str
    fingerprint: str
    component: str
    category: IncidentCategory
    severity: IncidentSeverity
    reason: str
    first_seen_at: datetime
    last_seen_at: datetime
    consecutive_count: int = Field(default=1, ge=1)
    market_id: str | None = None
    token_id: str | None = None
    client_order_id: str | None = None
    resolved_at: datetime | None = None


class TaskHealth(BaseModel):
    name: str
    running: bool
    started_at: datetime | None = None
    last_heartbeat: datetime | None = None
    last_exit_at: datetime | None = None
    restart_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_error: str | None = None


class LiveOperatingLease(BaseModel):
    lease_id: str
    issued_at: datetime
    expires_at: datetime
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: LeaseStatus
    revoked_at: datetime | None = None
    revocation_reason: str | None = None


class OutboxAlert(BaseModel):
    alert_id: str
    incident_fingerprint: str
    severity: IncidentSeverity
    text: str
    created_at: datetime
    next_attempt_at: datetime
    attempt_count: int = Field(default=0, ge=0)
    occurrence_count: int = Field(default=1, ge=1)
    delivered_at: datetime | None = None
    last_error: str | None = None
```

Validate that lease expiration is after issuance, revocation fields are both present only for revoked leases, delivered alerts have a timestamp, and all free-form stored reasons/text have documented maximum lengths.

- [ ] **Step 5: Run focused and full tests**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_config.py tests/test_operations_models.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the task**

```bash
git add config/schema.py config/bot.yaml models/operations.py tests/test_config.py tests/test_operations_models.py
git commit -m "feat: define unattended operations reliability model"
```

---

### Task 2: Durable Operations Repository

**Files:**
- Create: `persistence/operations.py`
- Create: `tests/test_operations_repository.py`

**Interfaces:**
- Consumes: `LiveOperatingLease`, `OperationalIncident`, `OutboxAlert`, and `LeaseStatus` from Task 1.
- Produces: async `OperationsRepository` methods used by leases, incident history, notifications, dashboard health, and retention.

The public interface must be:

```python
class OperationsRepository:
    def __init__(self, path: str | Path) -> None: ...
    async def create_lease(self, lease: LiveOperatingLease) -> None: ...
    async def get_active_lease(self) -> LiveOperatingLease | None: ...
    async def revoke_active_lease(self, *, reason: str, revoked_at: datetime) -> LiveOperatingLease | None: ...
    async def record_incident(self, incident: OperationalIncident) -> None: ...
    async def recent_incidents(self, *, limit: int = 100) -> list[OperationalIncident]: ...
    async def resolve_incident(self, incident_id: str, *, resolved_at: datetime) -> OperationalIncident: ...
    async def enqueue_alert(self, alert: OutboxAlert, *, dedupe_after: datetime) -> OutboxAlert: ...
    async def due_alerts(self, *, now: datetime, limit: int = 20) -> list[OutboxAlert]: ...
    async def mark_alert_delivered(self, alert_id: str, *, delivered_at: datetime) -> None: ...
    async def reschedule_alert(self, alert_id: str, *, next_attempt_at: datetime, error: str) -> None: ...
    async def outbox_stats(self, *, now: datetime) -> tuple[int, float | None]: ...
    async def last_delivered_at(self, incident_fingerprint: str) -> datetime | None: ...
    async def prune(self, *, delivered_before: datetime, incidents_before: datetime) -> tuple[int, int]: ...
```

- [ ] **Step 1: Write restart-durability and deduplication tests**

Create `tests/test_operations_repository.py` with tests that instantiate a repository, write data, discard it, instantiate a second repository on the same file, and read the same typed records. Include:

```python
@pytest.mark.asyncio
async def test_active_lease_survives_repository_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.sqlite3"
    first = OperationsRepository(path)
    lease = active_lease()
    await first.create_lease(lease)

    restored = await OperationsRepository(path).get_active_lease()

    assert restored == lease


@pytest.mark.asyncio
async def test_enqueue_deduplicates_pending_incident(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    first = await repository.enqueue_alert(alert("alert-1"), dedupe_after=NOW - timedelta(minutes=15))
    second = await repository.enqueue_alert(alert("alert-2"), dedupe_after=NOW - timedelta(minutes=15))
    assert second.alert_id == first.alert_id
    assert second.occurrence_count == 2
    assert len(await repository.due_alerts(now=NOW, limit=20)) == 1
```

Also test lease revocation is atomic and keeps the first reason, delivered alerts are excluded from `due_alerts`, retry errors are sanitized/truncated before storage, and pruning never deletes undelivered alerts.

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_operations_repository.py
```

Expected: FAIL because `OperationsRepository` does not exist.

- [ ] **Step 3: Implement schema initialization and typed serialization**

Create SQLite tables `live_leases`, `operational_incidents`, and `notification_outbox`. Store Pydantic models as canonical JSON plus indexed lifecycle columns. Use `BEGIN IMMEDIATE` for lease replacement/revocation and alert deduplication. Enable `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, `PRAGMA foreign_keys=ON`, and `busy_timeout=5000` for every connection.

Every public async method must wrap one complete synchronous transaction with `await asyncio.to_thread(...)`; never hold a SQLite connection across an `await`. Normalize datetimes to UTC ISO-8601 strings. Cap stored `last_error` at 256 sanitized characters.

- [ ] **Step 4: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_operations_repository.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the task**

```bash
git add persistence/operations.py tests/test_operations_repository.py
git commit -m "feat: persist leases incidents and alert outbox"
```

---

### Task 3: Deterministic Backoff and Shared Fault Policy

**Files:**
- Create: `reliability/__init__.py`
- Create: `reliability/backoff.py`
- Create: `reliability/incidents.py`
- Create: `reliability/policy.py`
- Create: `tests/test_reliability_policy.py`

**Interfaces:**
- Consumes: Task 1 enums/models and `ReliabilityConfig`.
- Produces: `BackoffSchedule.delay(attempt: int) -> float`, `IncidentFactory`, `RecoveryContext`, and `FaultPolicy.decide(incident, context) -> RecoveryAction`.

Define the context exactly as:

```python
@dataclass(frozen=True, slots=True)
class RecoveryContext:
    flat: bool
    authoritative_unavailable_seconds: float = 0
    open_position_exit_window_seconds: float | None = None
    repeated_authoritative_confirmations: int = 0
    task_crashes_in_window: int = 0
    disk_percent: float = 0
    required_for_safe_exit: bool = False
```

- [ ] **Step 1: Write the full policy-matrix tests**

Use parametrized cases for every row in the approved spec. At minimum assert:

```python
@pytest.mark.parametrize(
    ("category", "context", "expected"),
    [
        (IncidentCategory.TRANSIENT_TRANSPORT, RecoveryContext(flat=True), RecoveryAction.RETRY),
        (IncidentCategory.AUTHENTICATION, RecoveryContext(flat=True), RecoveryAction.HALT),
        (IncidentCategory.COMPLIANCE, RecoveryContext(flat=True), RecoveryAction.HALT),
        (IncidentCategory.ACCOUNTING, RecoveryContext(flat=True), RecoveryAction.HALT),
        (IncidentCategory.EXIT_SAFETY, RecoveryContext(flat=False), RecoveryAction.HALT),
        (IncidentCategory.TASK_CRASH, RecoveryContext(flat=True, task_crashes_in_window=3), RecoveryAction.RETRY),
        (IncidentCategory.TASK_CRASH, RecoveryContext(flat=True, task_crashes_in_window=4), RecoveryAction.HALT),
        (IncidentCategory.DISK, RecoveryContext(flat=True, disk_percent=89), RecoveryAction.RETRY),
        (IncidentCategory.DISK, RecoveryContext(flat=True, disk_percent=90), RecoveryAction.DEGRADE),
        (IncidentCategory.DISK, RecoveryContext(flat=True, disk_percent=95), RecoveryAction.HALT),
    ],
)
def test_fault_policy(category, context, expected) -> None:
    assert policy().decide(incident(category), context) == expected
```

Test that an authoritative-state outage remains `DEGRADE` while flat, but becomes `HALT` after 300 seconds when exposed. Test account divergence degrades after the first confirmation and halts after the second. Test funding failure halts only when `required_for_safe_exit=True`.

For backoff, inject a deterministic random source and assert base delays `1, 2, 4, 8, 16, 30, 30` and jitter stays within ±20%.

Test `IncidentFactory` mappings for reconciliation transport failure, repeated authoritative divergence, runtime daily loss, stale heartbeat, circuit breaker, `position_open_at_market_end`, authentication/signature/compliance, persistence writes, disk thresholds, and unknown exceptions. Unknown exception output must contain the exception type but never `str(exc)`.

- [ ] **Step 2: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_reliability_policy.py
```

Expected: FAIL because the reliability package does not exist.

- [ ] **Step 3: Implement policy without side effects**

`FaultPolicy` must be a pure decision object: it does not sleep, persist, cancel orders, mutate runtime state, or send notifications. Put threshold precedence in explicit branches, checking immediate `HALT` categories before duration-based rules. `BackoffSchedule` owns jitter and must reject negative attempts.

`IncidentFactory` is the only place that converts current reconciliation reports, risk decisions, known client exceptions, rotation errors, and persistence errors into categories/reasons. It assigns stable fingerprints from component/category/sanitized reason and increments are handled later by repository/supervisor state. Do not repeat string matching in runtime loops.

- [ ] **Step 4: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_reliability_policy.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the task**

```bash
git add reliability tests/test_reliability_policy.py
git commit -m "feat: classify operational faults and recovery budgets"
```

---

### Task 4: Durable Telegram Alert Pipeline

**Files:**
- Create: `notifications/outbox.py`
- Modify: `notifications/telegram.py:15-86`
- Modify: `notifications/events.py:29-35`
- Modify: `models/events.py:25-40`
- Modify: `app/bootstrap.py:98-123,317-324,439-461`
- Create: `tests/test_notification_outbox.py`
- Create: `tests/test_telegram.py`
- Modify: `tests/test_bootstrap.py`

**Interfaces:**
- Consumes: `OperationsRepository`, `BackoffSchedule`, `OperationalIncident`, `OutboxAlert`, and notification config.
- Produces: `AlertService.enqueue_incident(...)`, `AlertService.enqueue_event(...)`, `TelegramTransport.send(alert)`, and `NotificationWorker.run(stop_event, heartbeat)`.

Use these signatures:

```python
class AlertService:
    async def enqueue_incident(self, incident: OperationalIncident) -> OutboxAlert: ...
    async def enqueue_event(self, event: BotEvent) -> OutboxAlert | None: ...
    async def enqueue_test(self, *, now: datetime) -> OutboxAlert: ...


class TelegramTransport:
    async def send(self, alert: OutboxAlert) -> None: ...
    async def close(self) -> None: ...


class NotificationWorker:
    async def deliver_due_once(self) -> int: ...
    async def deliver_alert_now(self, alert_id: str) -> bool: ...
    async def run(
        self,
        stop_event: asyncio.Event,
        heartbeat: Callable[[], Awaitable[None]],
    ) -> None: ...
```

- [ ] **Step 1: Write failing durability, retry, and redaction tests**

Tests must prove:

1. `enqueue_incident` returns only after the SQLite row exists.
2. A failed Telegram call reschedules the same alert with incremented attempts.
3. A second repository/worker instance delivers an alert queued before restart.
4. Identical incidents inside 15 minutes create one alert with an occurrence count.
5. Telegram failure does not raise out of `AlertService.enqueue_event` after the durable write.
6. Messages never contain configured private key, CLOB credentials, Telegram token, or funder address.
7. A recovered warning generates one recovery alert.
8. `deliver_alert_now` records a successful `telegram:test` delivery timestamp, and a failed test remains in the durable retry queue.

Representative delivery test:

```python
@pytest.mark.asyncio
async def test_failed_delivery_is_retried_after_restart(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    await AlertService(repository, config()).enqueue_incident(warning_incident())
    failing = FakeTelegram([RuntimeError("token=secret" )])
    first = NotificationWorker(repository, failing, config(), now=lambda: NOW)
    assert await first.deliver_due_once() == 0

    succeeding = FakeTelegram([None])
    restored = NotificationWorker(
        OperationsRepository(tmp_path / "bot.sqlite3"),
        succeeding,
        config(),
        now=lambda: NOW + timedelta(minutes=10),
    )
    assert await restored.deliver_due_once() == 1
    assert succeeding.sent[0].incident_fingerprint == warning_incident().fingerprint
```

- [ ] **Step 2: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_notification_outbox.py tests/test_telegram.py
```

Expected: FAIL because the outbox worker and transport boundary do not exist.

- [ ] **Step 3: Refactor Telegram and add the worker**

Move HTTP delivery out of event filtering. `TelegramTransport` owns one long-lived `httpx.AsyncClient(timeout=10.0)` and raises a sanitized `NotificationDeliveryError` on failure. `NotificationWorker` owns retries and persists every reschedule. Use `BackoffSchedule` with notification initial/max values.

`AlertService.enqueue_event` maps `KILL_SWITCH_TRIPPED`, `REPEATED_FAILURES`, fatal task failures, lease events, and daily summaries to durable rows. Preserve the existing large simulated-order behavior. Add event types `RUNTIME_DEGRADED`, `RUNTIME_RECOVERED`, `RUNTIME_FAILED`, `LIVE_LEASE_ISSUED`, `LIVE_LEASE_EXPIRING`, `LIVE_LEASE_EXPIRED`, `AUTO_RESUME_REJECTED`, and `DAILY_SUMMARY`.

Change `EventBus.publish` to gather handlers with `return_exceptions=True` and log sanitized handler failures so an auxiliary subscriber cannot break core runtime work. Safety code must call `AlertService.enqueue_incident` directly before relying on the event bus.

Wire `OperationsRepository`, `AlertService`, `TelegramTransport`, and `NotificationWorker` into `AppServices`. Remove direct `event_bus.subscribe(telegram.notify_event)` delivery; subscribe `alert_service.enqueue_event` instead.

- [ ] **Step 4: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_notification_outbox.py tests/test_telegram.py tests/test_bootstrap.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the task**

```bash
git add notifications models/events.py app/bootstrap.py tests/test_notification_outbox.py tests/test_telegram.py tests/test_bootstrap.py
git commit -m "feat: deliver intervention alerts through durable telegram outbox"
```

---
### Task 5: Critical Task Supervisor

**Files:**
- Create: `app/supervisor.py`
- Create: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `BackoffSchedule`, `OperationalIncident`, `IncidentCategory`, `IncidentSeverity`, `TaskHealth`, and reliability config.
- Produces: `TaskSpec` and `RuntimeSupervisor`, consumed by the runtime refactor in Task 6.

Use these exact public types:

```python
Heartbeat = Callable[[], Awaitable[None]]
TaskFactory = Callable[[asyncio.Event, Heartbeat], Awaitable[None]]
IncidentHandler = Callable[[OperationalIncident], Awaitable[RecoveryAction]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    factory: TaskFactory
    restartable: bool = True
    heartbeat_timeout_seconds: float = 60.0


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        config: ReliabilityConfig,
        incident_handler: IncidentHandler,
        backoff: BackoffSchedule,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None: ...
    async def start(self, specs: list[TaskSpec]) -> None: ...
    async def stop(self) -> None: ...
    async def wait_fatal(self) -> OperationalIncident: ...
    async def health(self) -> list[TaskHealth]: ...
```

- [ ] **Step 1: Write failing task-death and restart-budget tests**

Cover clean stop, unexpected normal return, exception, heartbeat timeout, restart success, rolling-window reset, and exhausted restart budget. The central regression is:

```python
@pytest.mark.asyncio
async def test_fourth_task_crash_becomes_fatal_and_cannot_be_silent() -> None:
    crashes = 0

    async def crashing(_stop: asyncio.Event, heartbeat: Heartbeat) -> None:
        nonlocal crashes
        crashes += 1
        await heartbeat()
        raise RuntimeError("secret remote message")

    supervisor = supervisor_with_zero_backoff()
    await supervisor.start([TaskSpec(name="reconciliation-loop", factory=crashing)])
    fatal = await asyncio.wait_for(supervisor.wait_fatal(), timeout=1)

    assert crashes == 4
    assert fatal.category == IncidentCategory.TASK_CRASH
    assert fatal.reason == "task_crash:RuntimeError"
    health = {item.name: item for item in await supervisor.health()}
    assert health["reconciliation-loop"].running is False
    assert health["reconciliation-loop"].restart_count == 3
```

Test that calling `stop()` prevents intentional cancellation/return from being counted as a crash.

- [ ] **Step 2: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_supervisor.py
```

Expected: FAIL because `app.supervisor` does not exist.

- [ ] **Step 3: Implement supervisor ownership and health**

Create one wrapper task per `TaskSpec`. The wrapper:

1. records start time and heartbeat;
2. awaits the factory;
3. treats return before `stop_event` as `task_returned_unexpectedly`;
4. sanitizes exceptions to type-only reasons;
5. reports a `TASK_CRASH` incident;
6. restarts only when the incident handler returns `RETRY` and the rolling budget permits it;
7. sets the fatal future exactly once when policy returns `HALT` or the restart budget is exhausted.

Run a watchdog task at `min(5.0, heartbeat_timeout / 2)` that emits a task-crash incident when a running task exceeds its heartbeat deadline. Ensure only the supervisor mutates restart counters. `stop()` sets the shared stop event, cancels all owned tasks, and gathers them with `return_exceptions=True`.

- [ ] **Step 4: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_supervisor.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the task**

```bash
git add app/supervisor.py tests/test_supervisor.py
git commit -m "feat: supervise critical runtime tasks"
```

---

### Task 6: Isolate Periodic Loops and Enforce Degraded/Halt State

**Files:**
- Create: `app/loops.py`
- Modify: `app/runtime.py:26-42,61-195,205-395`
- Modify: `app/main.py:8-45`
- Modify: `app/shutdown.py:28-80`
- Modify: `app/bootstrap.py:98-123,439-461`
- Modify: `state/store.py:39-138`
- Modify: `risk/pretrade.py:43-95`
- Modify: `dashboard/models.py:120-160`
- Modify: `dashboard/read_model.py:89-150`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_risk_pretrade.py`
- Create: `tests/test_runtime_loops.py`

**Interfaces:**
- Consumes: `RuntimeSupervisor`, `FaultPolicy`, `RecoveryContext`, `AlertService`, `OperationsRepository`, and Task 1 operational models.
- Produces: supervised loop factories, `BotRuntime.handle_incident`, accurate degraded/halted/failed runtime state, and an entry-permission gate in `InMemoryStateStore`.

Add this state-store boundary:

```python
async def set_operational_state(self, state: OperationalState, *, reason: str | None = None) -> None: ...
async def get_operational_state(self) -> tuple[OperationalState, str | None]: ...
async def entries_permitted(self) -> bool: ...
```

- [ ] **Step 1: Write failing entry-gate and silent-loop-death tests**

Add to `tests/test_risk_pretrade.py`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        OperationalState.DEGRADED,
        OperationalState.HALTING,
        OperationalState.HALTED,
        OperationalState.FAILED,
    ],
)
async def test_buy_is_rejected_outside_running_state(state: OperationalState) -> None:
    store = ready_state_store()
    await store.set_operational_state(state, reason="test_incident")
    decision = await risk_engine(store).evaluate(
        signal=buy_signal(), snapshot=fresh_snapshot(),
        proposed_size=Decimal("1"), proposed_price=Decimal("0.5"),
        executable_liquidity=Decimal("100"),
    )
    assert decision.approved is False
    assert decision.reason == f"entries_paused:{state.value}:test_incident"
```

Add to `tests/test_runtime.py` a regression where the reconciliation loop raises four times. Assert the runtime leaves `RUNNING`, persists the kill switch, cancels live orders, enqueues an urgent incident, and `wait()` returns rather than waiting forever on `_stop_event`.

- [ ] **Step 2: Add failing isolated-loop tests**

Create `tests/test_runtime_loops.py`. Test each loop invokes only its responsibility and sends a heartbeat after a successful cycle. Use an immediately set stop event after one cycle. Assert:

- reconciliation failures report an authoritative-state incident;
- runtime-risk rejection reports the exact typed safety category;
- the exit loop routes only exit signals;
- the strategy timer routes timer signals;
- the snapshot loop writes snapshots without running reconciliation or strategy;
- notification delivery has its own loop and cannot terminate other loops.

- [ ] **Step 3: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_loops.py tests/test_runtime.py tests/test_risk_pretrade.py
```

Expected: FAIL because operational state and isolated loops are missing.

- [ ] **Step 4: Extract loops without changing successful-cycle behavior**

Move the current housekeeping responsibilities into `app/loops.py` functions with this shape:

```python
async def reconciliation_loop(
    services: AppServices,
    stop_event: asyncio.Event,
    heartbeat: Heartbeat,
    report: Callable[[OperationalIncident], Awaitable[RecoveryAction]],
) -> None: ...
```

Create the same signature for runtime risk, position exit, strategy timer, snapshots, and notifications. Expected dependency failures are converted to incidents and the loop continues according to the returned action. Unexpected programming errors must bubble to `RuntimeSupervisor` instead of being swallowed.

Wrap market rotation as a supervised loop too. Convert the known `position_open_at_market_end` error through `IncidentFactory` to `EXIT_SAFETY/HALT`; convert `MarketDiscoveryError` to the retry/degrade discovery path; allow unknown programming errors to bubble as task crashes. Remove the current direct kill-switch mutation from `market_rotation_loop` so all halt ordering is centralized in `BotRuntime.handle_incident`.

Do not duplicate timer work. Remove `housekeeping_loop`, remove its import from `app/main.py`, remove the injectable `housekeeping` constructor argument from `BotRuntime`, and update every affected runtime test to inject loop factories through the supervisor seam instead.

- [ ] **Step 5: Integrate the supervisor into `BotRuntime`**

Replace the duplicate runtime enum with `RuntimePhase = OperationalState` in `app/runtime.py` so existing imports remain compatible while one enum owns all serialized values. Register one `TaskSpec` per loop plus market rotation and REST fallback when configured.

Implement `BotRuntime.handle_incident` in this order:

1. persist the incident;
2. build `RecoveryContext` from positions, open orders, deadlines, task history, and disk state;
3. call the pure fault policy;
4. for `RETRY`, retain current state and return;
5. for `DEGRADE`, set runtime and state-store operational state to degraded, enqueue a warning only after the threshold, and return;
6. for `HALT`, transition to `HALTING`, latch and snapshot the kill switch, cancel open orders with timeout, enqueue an urgent alert, transition to `HALTED` or `FAILED`, and set a runtime terminal event.

If incident persistence, kill-state snapshotting, or outbox enqueue fails, treat it as a persistence safety failure: keep the in-memory kill switch latched, attempt cancellation, write a sanitized critical log to stderr, transition to `FAILED`, and request nonzero process exit. Telegram transport failure after a successful durable enqueue is not a halt condition.

`BotRuntime.wait()` must await either an operator stop or the supervisor fatal/terminal event and return the terminal `RuntimeStatus`. It may never depend solely on `_stop_event`. Headless `app.main.run` raises `FatalRuntimeError` after cleanup when that status is `FAILED`, producing a nonzero process exit for systemd/Docker. `RuntimeStatus` must include `operational_reason` and `degraded_since`.

Add `_operational_state_check` before ordinary exposure checks in `PreTradeRiskEngine`. SELL/reduce-only signals still pass this check, but remain subject to all data, inventory, liquidity, and slippage checks.

- [ ] **Step 6: Make shutdown idempotent with supervisor ownership**

`shutdown_app` must stop the supervisor first, then cancel open live orders, persist a final snapshot, stop the market rotator/discovery client, close notification transport, and stop the WebSocket. Preserve current cleanup aggregation and secret-safe reasons.

- [ ] **Step 7: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_loops.py tests/test_runtime.py tests/test_risk_pretrade.py \
  tests/test_dashboard_read_model.py tests/test_position_lifecycle_e2e.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS and no test expects a monolithic `housekeeping` task.

- [ ] **Step 8: Commit the task**

```bash
git add app/loops.py app/runtime.py app/main.py app/shutdown.py app/bootstrap.py state/store.py \
  risk/pretrade.py dashboard/models.py dashboard/read_model.py \
  tests/test_runtime.py tests/test_runtime_loops.py tests/test_risk_pretrade.py \
  tests/test_dashboard_read_model.py tests/test_position_lifecycle_e2e.py
git commit -m "feat: isolate and supervise runtime safety loops"
```

---

### Task 7: WebSocket Health and REST Market-Data Fallback

**Files:**
- Modify: `clients/ws_client.py:25-153`
- Modify: `clients/clob_client.py:118-205`
- Modify: `clients/market_data_client.py:25-208`
- Create: `clients/rest_market_data.py`
- Modify: `app/bootstrap.py:412-461`
- Modify: `app/runtime.py`
- Modify: `tests/test_ws_client.py`
- Modify: `tests/test_clob_client.py`
- Modify: `tests/test_market_data_client.py`
- Create: `tests/test_rest_market_data.py`

**Interfaces:**
- Consumes: supervised-task heartbeat/report callbacks and operational state from Task 6.
- Produces: `WebSocketHealth`, `ClobClientAdapter.get_market_snapshot`, and `RestMarketDataFallback.run`.

Define:

```python
class WebSocketHealth(BaseModel):
    connected: bool
    task_running: bool
    last_heartbeat: datetime | None
    disconnected_since: datetime | None
    connection_attempts: int
    last_error: str | None


class RestMarketDataFallback:
    async def run(
        self,
        stop_event: asyncio.Event,
        heartbeat: Heartbeat,
    ) -> None: ...
```

- [ ] **Step 1: Write failing WebSocket-health tests**

Extend `tests/test_ws_client.py` to assert `health()` distinguishes connected, retrying, stopped, and unexpectedly dead states. Exception reasons must be type-only. Expose `wait_closed()` so the supervisor can await the internal task without reaching into `_task`.

- [ ] **Step 2: Write failing CLOB normalization tests**

In `tests/test_clob_client.py`, fake `get_order_book(token_id)` to return V2 SDK book objects/dicts with bids, asks, market/asset identifiers, and timestamp. Assert `get_market_snapshot(market_id, token_id)` returns a validated `MarketSnapshot` with best bid/ask, sizes, UTC timestamps, and rejects empty/crossed books as `ClobAdapterError`.

- [ ] **Step 3: Write failing fallback behavior tests**

Create `tests/test_rest_market_data.py` proving:

- no REST poll occurs before 30 seconds disconnected;
- both current automatic-market token IDs are polled after the threshold;
- fallback snapshots update state and may invoke exit processing;
- fallback snapshots never invoke spike-strategy entry generation;
- WebSocket recovery disables fallback and requires a successful reconciliation before entries resume;
- REST failures report typed transient incidents and obey backoff;
- an exposed position without fresh WebSocket or REST data inside its exit window becomes `HALT`.

- [ ] **Step 4: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_ws_client.py tests/test_clob_client.py tests/test_market_data_client.py \
  tests/test_rest_market_data.py
```

Expected: FAIL because health/wait and REST fallback interfaces are absent.

- [ ] **Step 5: Implement health and fallback**

`WebSocketManager._run` records `disconnected_since` and sanitized `last_error`, continues existing reconnect behavior, and exposes immutable health. Do not let `replace_asset_ids` race with fallback token lookup; both return copied asset lists.

Add `ClobClientAdapter.get_market_snapshot` around the installed V2 SDK's `get_order_book(token_id)` method. Keep SDK interaction inside the adapter and call it with `asyncio.to_thread` from async fallback code.

Add `MarketDataClient.ingest_fallback_snapshot(snapshot)` that updates the store and fallback heartbeat but calls a dedicated exit-only callback. Do not reuse the primary `on_snapshot` callback because it currently evaluates both exits and entry strategy.

The fallback task polls active tokens every two seconds only after the configured disconnect threshold. It exits fallback mode after a fresh WebSocket heartbeat, then asks runtime coordination for one successful reconciliation before clearing the degraded state.

- [ ] **Step 6: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_ws_client.py tests/test_clob_client.py tests/test_market_data_client.py \
  tests/test_rest_market_data.py tests/test_runtime.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the task**

```bash
git add clients/ws_client.py clients/clob_client.py clients/market_data_client.py \
  clients/rest_market_data.py app/bootstrap.py app/runtime.py tests/test_ws_client.py \
  tests/test_clob_client.py tests/test_market_data_client.py tests/test_rest_market_data.py
git commit -m "feat: recover market data through supervised rest fallback"
```

---

### Task 8: Time-Limited Live Lease and Safe Automatic Resume

**Files:**
- Create: `app/process_services.py`
- Create: `config/fingerprint.py`
- Create: `reliability/lease.py`
- Modify: `dashboard/controller.py:41-58,61-252`
- Modify: `dashboard/app.py:46-72`
- Modify: `dashboard/main.py:28-53`
- Modify: `app/runtime.py:248-395`
- Modify: `app/main.py:11-45`
- Modify: `app/bootstrap.py`
- Modify: `dashboard/models.py`
- Modify: `dashboard/read_model.py`
- Modify: `tests/test_dashboard_controller.py`
- Modify: `tests/test_dashboard_api.py`
- Modify: `tests/test_app_main.py`
- Modify: `tests/test_runtime.py`
- Create: `tests/test_live_lease.py`

**Interfaces:**
- Consumes: `OperationsRepository`, `AlertService`, operational state, full bootstrap preflight/reconciliation, and existing dashboard confirmations.
- Produces: `config_fingerprint(config)`, `LiveLeaseService`, `DashboardController.attempt_auto_resume`, and distinct explicit-stop versus process-shutdown paths.

Define:

```python
class LiveLeaseService:
    async def issue(self, config: AppConfig, *, now: datetime) -> LiveOperatingLease: ...
    async def validate_for_resume(self, config: AppConfig, *, now: datetime) -> LiveOperatingLease: ...
    async def revoke(self, reason: str, *, now: datetime) -> LiveOperatingLease | None: ...
    async def expiration_state(self, *, now: datetime) -> Literal["valid", "warn_24h", "warn_1h", "expired", "missing"]: ...
```

- [ ] **Step 1: Extract and test the configuration fingerprint**

Move the dashboard's current `_config_fingerprint` logic into `config/fingerprint.py`. Test that mode/live-arm fields are normalized as today, a secret change changes the fingerprint, ordinary serialization order does not, and the returned value is exactly 64 lowercase hex characters. The hash is persisted; its source payload is never logged or stored.

- [ ] **Step 2: Write failing lease lifecycle tests**

Create `tests/test_live_lease.py` for issue, max/min duration validation, active lookup after restart, expiration, first-reason revocation, config mismatch, kill-switch rejection, and lease warning thresholds. Include:

```python
@pytest.mark.asyncio
async def test_safety_halt_revokes_lease_before_process_restart(tmp_path: Path) -> None:
    repository = OperationsRepository(tmp_path / "bot.sqlite3")
    leases = LiveLeaseService(repository, reliability_config())
    await leases.issue(live_config(), now=NOW)
    await leases.revoke("accounting_invariant", now=NOW + timedelta(minutes=1))
    with pytest.raises(LiveResumeRejected, match="lease_missing_or_revoked"):
        await leases.validate_for_resume(live_config(), now=NOW + timedelta(minutes=2))
```

- [ ] **Step 3: Write failing dashboard/process restart tests**

Prove all of these cases:

1. Manual dashboard `START LIVE` issues a lease only after runtime reaches `RUNNING`.
2. Explicit Stop, Halt, config mutation, or Return to dry run revokes it.
3. FastAPI lifespan shutdown calls `controller.shutdown_process()` and preserves an active lease.
4. Lifespan startup calls `attempt_auto_resume()`.
5. Auto-resume reruns full bootstrap preflight and startup reconciliation.
6. A valid lease resumes; expired lease, config mismatch, kill switch, failed preflight, or failed reconciliation does not.
7. Rejection writes an urgent outbox alert even when the bot service graph never starts.
8. Lease expiry pauses entries, exits positions, cancels orders, persists, alerts, and halts.
9. Lease issuance fails with `recent_telegram_test_required` unless `telegram:test` was delivered within five minutes.
10. A fatal supervised-task result makes the dashboard server shut down and return exit code 1; an ordinary safety `HALTED` state leaves the dashboard running for intervention.
11. Restarting the lease monitor cannot duplicate 24-hour or one-hour warnings, and expiration follows centralized halt ordering exactly once.
12. Failure to persist a newly issued lease immediately halts the just-started runtime and cancels its open orders.

- [ ] **Step 4: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_live_lease.py tests/test_dashboard_controller.py tests/test_dashboard_api.py \
  tests/test_app_main.py tests/test_runtime.py
```

Expected: FAIL because lease and auto-resume behavior do not exist.

- [ ] **Step 5: Implement lease behavior and safe lifecycle separation**

Manual start flow:

1. require fresh dashboard preflight, a `telegram:test` delivery no older than five minutes, and exact `START LIVE` confirmation;
2. call `BotRuntime.start(..., allow_live=True)`; bootstrap repeats full live preflight and reconciliation;
3. issue the lease only after status is `RUNNING`;
4. enqueue `LIVE_LEASE_ISSUED`.

Auto-resume flow:

1. load config without changing it;
2. validate lease and persisted snapshot kill state;
3. call the same `BotRuntime.start(..., allow_live=True)` path, never a reduced preflight path;
4. on success enqueue an informational resume event;
5. on failure remain stopped/halted and durably enqueue `AUTO_RESUME_REJECTED` with a sanitized check name.

Add `DashboardController.shutdown_process()` and `BotRuntime.shutdown_process()` for host/process shutdown without lease revocation. Keep explicit `/api/control/stop`, halt, configuration changes, and return-to-dry-run revoking. Fatal incident handling from Task 6 must revoke before snapshot/cancel/alert.

Refactor `dashboard.main` to run `uvicorn.Server.serve()` and `controller.wait_runtime_failed()` concurrently. If the runtime-failed waiter finishes first, set `server.should_exit = True`, await graceful server shutdown, and return exit code 1. Do not terminate the server for `HALTED`; the operator must retain dashboard access to diagnose and recover.

`app.main` gains `--resume-live`, mutually exclusive with `--live`. `--resume-live` validates a persisted lease; it never treats the command-line flag as new authorization.

Construct `OperationsRepository`, `LiveLeaseService`, `AlertService`, and `TelegramTransport` at the process/controller boundary from `BOT_DATA_DIR`, not only inside a successfully bootstrapped trading service graph. This is required so an auto-resume failure can still write and attempt delivery of `AUTO_RESUME_REJECTED`. Inject the same repository/services into `BotRuntime`/`bootstrap_app`; do not start two notification workers against the same outbox in one process. The dashboard lifespan owns the single notification worker and keeps it running while the trading runtime is stopped or halted; headless `app.main` owns one worker for its process.

Use one explicit container:

```python
@dataclass(slots=True)
class ProcessReliabilityServices:
    repository: OperationsRepository
    leases: LiveLeaseService
    alerts: AlertService
    telegram: TelegramTransport
    notification_worker: NotificationWorker


def build_process_reliability_services(
    *, config: AppConfig, data_dir: Path
) -> ProcessReliabilityServices: ...
```

`dashboard.main`, headless `app.main`, `DashboardController`, `BotRuntime`, and `bootstrap_app` receive this same instance. Trading-service shutdown must not close process-owned notification transport; process lifespan shutdown closes it exactly once.

Add `DashboardController.send_telegram_test("SEND TEST")` and `POST /api/notifications/test` behind the existing operator token/origin guard. The method durably queues `telegram:test`, calls `deliver_alert_now`, and returns failure without deleting the queued alert when Telegram is unreachable.

Register a supervised `lease-monitor-loop` in live mode. It emits each 24-hour and one-hour warning once using repository idempotency keys. At expiration it reports a lease-expired safety incident; centralized halt handling pauses entries, runs configured exits, cancels remaining orders, snapshots, alerts, and leaves the lease expired/revoked.

If runtime reaches `RUNNING` but lease persistence fails, do not leave it trading without authorization: immediately invoke centralized safety halt, cancel orders, persist the failure where possible, and return a failed start result.

- [ ] **Step 6: Add lease state to dashboard read models**

Expose lease ID suffix (last eight characters only), state, expiry, remaining seconds, auto-resume eligibility, and rejection reason. Never expose the fingerprint. Add warnings at 24 hours and one hour.

- [ ] **Step 7: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_live_lease.py tests/test_dashboard_controller.py tests/test_dashboard_api.py \
  tests/test_dashboard_read_model.py tests/test_app_main.py tests/test_runtime.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 8: Commit the task**

```bash
git add app/process_services.py config/fingerprint.py reliability/lease.py dashboard \
  app/main.py app/runtime.py \
  app/bootstrap.py tests/test_live_lease.py tests/test_dashboard_controller.py \
  tests/test_dashboard_api.py tests/test_dashboard_read_model.py tests/test_app_main.py \
  tests/test_runtime.py
git commit -m "feat: authorize safe live resume with durable leases"
```

---

### Task 9: Bounded Retention, Journal Rotation, and Disk Safety

**Files:**
- Create: `persistence/retention.py`
- Modify: `persistence/journal.py:12-34`
- Modify: `persistence/snapshots.py:26-164`
- Modify: `persistence/operations.py`
- Modify: `state/store.py:39-84,489-560`
- Modify: `app/loops.py`
- Modify: `app/bootstrap.py`
- Create: `tests/test_retention.py`
- Create: `tests/test_journal.py`
- Modify: `tests/test_snapshots.py`
- Modify: `tests/test_state_store.py`

**Interfaces:**
- Consumes: `OperationsRepository.prune`, supervised snapshot loop, incident reporting, current market identity, open orders, active positions, and reliability retention config.
- Produces: `RetentionManager.run_once(...) -> RetentionReport`, rotating `JsonlJournal`, disk incidents, and bounded state-store pruning.

Define:

```python
class RetentionReport(BaseModel):
    signals_removed: int = 0
    market_books_removed: int = 0
    market_snapshots_removed: int = 0
    fill_checkpoints_removed: int = 0
    pnl_days_archived: int = 0
    closed_lifecycles_archived: int = 0
    outbox_rows_removed: int = 0
    incidents_removed: int = 0
    journals_removed: int = 0
    disk_percent: float


class RetentionManager:
    async def run_once(
        self,
        *,
        state_store: InMemoryStateStore,
        active_market_keys: set[tuple[str, str]],
        now: datetime,
    ) -> RetentionReport: ...
```

- [ ] **Step 1: Write failing state-retention safety tests**

Create `tests/test_retention.py` with an over-cap collection containing active and old markets. Assert:

- current and immediately previous automatic-market books/snapshots remain;
- old market data is removed;
- at most 10,000 newest signals remain and none older than 24 hours remain;
- a seven-day-old fill checkpoint remains if its order is open, its position is active, or its incident is unresolved;
- an otherwise unreferenced old checkpoint is archived then removed;
- only 90 UTC days of P&L and 100 closed lifecycles remain hot after archive writes succeed;
- no hot state is deleted when the SQLite archive transaction fails.

- [ ] **Step 2: Write failing journal-rotation tests**

Use a fake clock and small byte limit:

```python
@pytest.mark.asyncio
async def test_journal_rotates_by_size_and_preserves_complete_json_lines(tmp_path: Path) -> None:
    journal = JsonlJournal(
        tmp_path / "journal" / "events.jsonl",
        rotate_bytes=180,
        retention_days=14,
        total_limit_bytes=1_000,
        now=lambda: NOW,
    )
    for index in range(20):
        await journal.append(event(index))
    await journal.maintain(now=NOW)
    rows = []
    for path in sorted((tmp_path / "journal").glob("*.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text().splitlines())
    assert len(rows) == 20
```

Also test daily rotation, 14-day removal, 500 MiB total-cap ordering, and that the active file is never deleted.

- [ ] **Step 3: Write failing disk-threshold tests**

Inject `disk_usage(path)` returning 79%, 80%, 90%, and 95%. Assert warning at 80, degraded at 90, and halt at 95. Simulate snapshot and archive write failures and assert they produce persistence incidents rather than pruning data.

- [ ] **Step 4: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_retention.py tests/test_journal.py tests/test_snapshots.py tests/test_state_store.py
```

Expected: FAIL because retention/rotation interfaces do not exist.

- [ ] **Step 5: Implement archive-before-prune retention**

Add state-store snapshot methods that copy candidate collections under the lock and mutation methods that remove only named identities after archive success. Never perform SQLite or filesystem I/O while holding the state-store lock.

`RetentionManager.run_once` follows this order:

1. obtain disk usage;
2. report disk warning/degraded/halt incidents before any large write;
3. calculate immutable prune/archive candidates;
4. archive P&L, lifecycles, and eligible checkpoints in one repository transaction;
5. remove only successfully archived identities from hot state;
6. rotate/prune journals;
7. prune delivered outbox rows older than 30 days and incidents older than 90 days;
8. return counts and final disk percentage.

Update snapshot collection to use the already bounded hot state. Schedule retention at startup and every configured 3,600 seconds in the snapshot/retention supervised loop.

- [ ] **Step 6: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_retention.py tests/test_journal.py tests/test_snapshots.py \
  tests/test_state_store.py tests/test_runtime_loops.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the task**

```bash
git add persistence/retention.py persistence/journal.py persistence/snapshots.py \
  persistence/operations.py state/store.py app/loops.py app/bootstrap.py \
  tests/test_retention.py tests/test_journal.py tests/test_snapshots.py \
  tests/test_state_store.py tests/test_runtime_loops.py
git commit -m "feat: bound runtime state journal and disk growth"
```

---

### Task 10: Health Surfaces and Dashboard Operations View

**Files:**
- Create: `persistence/health.py`
- Modify: `dashboard/models.py:90-160`
- Modify: `dashboard/read_model.py:75-292`
- Modify: `dashboard/app.py:97-160`
- Modify: `dashboard/templates/index.html`
- Modify: `dashboard/static/dashboard.js`
- Modify: `dashboard/static/dashboard.css`
- Modify: `scripts/healthcheck.py`
- Modify: `app/loops.py`
- Modify: `app/bootstrap.py`
- Modify: `tests/test_dashboard_read_model.py`
- Modify: `tests/test_dashboard_api.py`
- Modify: `tests/test_dashboard_ui.py`
- Modify: `tests/test_healthcheck.py`

**Interfaces:**
- Consumes: supervisor health, operational incidents, outbox stats, lease status, disk report, reconciliation timestamps, market rotation, and current trading state.
- Produces: atomic `RuntimeHealthSnapshot`, `/api/health/live`, `/api/health/ready`, `/api/health/trading`, and dashboard operational-health models.

Define separate response models:

```python
class HealthAnswer(BaseModel):
    ok: bool
    state: OperationalState
    reason: str
    generated_at: datetime


class RuntimeHealthSnapshot(BaseModel):
    process_live: bool
    service_ready: bool
    trading_ready: bool
    state: OperationalState
    reason: str | None
    tasks: list[TaskHealth]
    websocket: WebSocketHealth
    market_data_source: Literal["websocket", "rest_fallback", "unavailable"]
    last_reconciliation_at: datetime | None
    outbox_pending: int
    oldest_outbox_age_seconds: float | None
    disk_percent: float
    lease_expires_at: datetime | None
    updated_at: datetime
```

- [ ] **Step 1: Write failing liveness/readiness/trading-readiness tests**

Assert these distinctions:

- a degraded but supervised runtime is live and service-ready but not trading-ready;
- a dead supervisor is not live;
- startup before reconciliation is live but not service/trading-ready;
- a halted dashboard process can be live and service-ready while trading-ready is false;
- stale health snapshots fail the CLI liveness check;
- no response contains config fingerprints, credentials, tokens, private keys, or funder addresses.

- [ ] **Step 2: Write failing dashboard state/UI tests**

Extend dashboard models with task health, incident summary, data source, last reconciliation, outbox depth/age, disk percent, lease expiry/remaining duration, and auto-resume eligibility. UI tests should assert stable element IDs and copy for `Running`, `Degraded`, `Halted`, `Telegram backlog`, `REST fallback`, and lease warnings. Keep the existing loopback and mutation-token protections unchanged.

- [ ] **Step 3: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_dashboard_read_model.py tests/test_dashboard_api.py tests/test_dashboard_ui.py \
  tests/test_healthcheck.py
```

Expected: FAIL because the new health models and endpoints are absent.

- [ ] **Step 4: Implement atomic health snapshots and endpoints**

`HealthSnapshotStore` writes `data/health/runtime.json` using temp-file + flush + `fsync` + replace, mirroring `SnapshotStore`. The supervisor updates it every five seconds and immediately on state transitions. Historical dashboard reads use this file when the runtime is not in process.

Endpoints are read-only and return 200 with `ok=false` for dashboard display. `scripts.healthcheck` uses exit 0/2 based on the selected `--kind liveness|readiness|trading`, defaulting to liveness for deployment supervisors. Preserve its existing market-data snapshot checks as the trading-readiness branch.

- [ ] **Step 5: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_dashboard_read_model.py tests/test_dashboard_api.py tests/test_dashboard_ui.py \
  tests/test_healthcheck.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the task**

```bash
git add persistence/health.py dashboard scripts/healthcheck.py app/loops.py app/bootstrap.py \
  tests/test_dashboard_read_model.py tests/test_dashboard_api.py \
  tests/test_dashboard_ui.py tests/test_healthcheck.py
git commit -m "feat: expose unattended operations health"
```

---

### Task 11: Guarded Human-Intervention Recovery

**Files:**
- Create: `reliability/recovery.py`
- Modify: `persistence/operations.py`
- Modify: `dashboard/controller.py`
- Modify: `dashboard/app.py`
- Modify: `dashboard/models.py`
- Modify: `dashboard/templates/index.html`
- Modify: `dashboard/static/dashboard.js`
- Modify: `dashboard/static/dashboard.css`
- Create: `tests/test_intervention_recovery.py`
- Modify: `tests/test_dashboard_controller.py`
- Modify: `tests/test_dashboard_api.py`
- Modify: `tests/test_dashboard_ui.py`

**Interfaces:**
- Consumes: fresh preflight, authoritative reconciliation, disk/health state, snapshot store, operations repository, revoked lease state, and exact operator confirmation.
- Produces: `InterventionRecoveryService.clear_halt(...) -> RecoveryResult` and guarded `POST /api/control/clear-halt`.

- [ ] **Step 1: Write failing intervention-recovery tests**

Require confirmation text `CLEAR HALT <last-eight-characters-of-incident-id>`. Assert recovery is rejected unless all of these are true: the selected incident is the active halt, a fresh dashboard preflight passed, authoritative reconciliation passed, persistence and outbox writes succeed, disk is below 80%, no unsafe open order exists, and every remaining position has a known lifecycle/deadline and safe exit path. Test both an in-process halted state and a restarted process reading a historical snapshot.

The successful test must prove:

```python
before = await snapshot_store.load()
assert before is not None and before.kill_switch_active is True
result = await recovery.clear_halt(
    incident_id=incident.incident_id,
    confirmation=f"CLEAR HALT {incident.incident_id[-8:]}",
)
assert result.cleared is True
after = await snapshot_store.load()
assert after is not None and after.kill_switch_active is False
assert (await repository.get_active_lease()) is None
assert (await repository.recent_incidents(limit=1))[0].resolved_at == NOW
assert runtime.status().phase != OperationalState.RUNNING
```

Also assert that clearing the latch does not start runtime, issue a lease, clear a different unresolved urgent incident, or reuse the preflight that authorized clearing.

- [ ] **Step 2: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_intervention_recovery.py tests/test_dashboard_controller.py \
  tests/test_dashboard_api.py tests/test_dashboard_ui.py
```

Expected: FAIL because guarded recovery does not exist.

- [ ] **Step 3: Implement the recovery boundary**

Define:

```python
class RecoveryResult(BaseModel):
    cleared: bool
    incident_id: str
    checks: list[RiskCheckResult]
    reason: str


class InterventionRecoveryService:
    async def clear_halt(
        self,
        *,
        incident_id: str,
        confirmation: str,
    ) -> RecoveryResult: ...
```

Consume injected callbacks for fresh preflight, authoritative reconciliation, disk probe, open-order/position verification, snapshot persistence, incident resolution, and lease revocation. Keep category-specific evidence in named checks returned to the dashboard. Never import exchange SDK code directly.

Add `POST /api/control/clear-halt` behind the existing operator guard. On success, clear and save the in-process state or atomically rewrite the historical snapshot, resolve only the selected incident, leave the lease revoked, invalidate the prior preflight, and require a new Telegram test plus preflight before `START LIVE`.

- [ ] **Step 4: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_intervention_recovery.py tests/test_dashboard_controller.py \
  tests/test_dashboard_api.py tests/test_dashboard_ui.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the task**

```bash
git add reliability/recovery.py persistence/operations.py dashboard \
  tests/test_intervention_recovery.py tests/test_dashboard_controller.py \
  tests/test_dashboard_api.py tests/test_dashboard_ui.py
git commit -m "feat: require verified intervention to clear safety halt"
```

---

### Task 12: Restart-Safe Operational Metrics and Daily Summary

**Files:**
- Create: `reliability/metrics.py`
- Modify: `persistence/operations.py`
- Modify: `app/bootstrap.py`
- Modify: `app/loops.py`
- Create: `tests/test_daily_summary.py`
- Create: `tests/test_operational_metrics.py`

**Interfaces:**
- Consumes: event bus, rotation/recovery callbacks, state P&L, health snapshot, lease state, disk and outbox statistics.
- Produces: idempotent `OperationalMetrics` and one durable daily Telegram summary per UTC day.

Add `daily_operational_metrics` and `metric_idempotency_keys` tables through `OperationsRepository`. Expose:

```python
class OperationalMetrics:
    async def record_event(self, event: BotEvent) -> None: ...
    async def record_market_rotation(self, market_id: str, *, at: datetime) -> None: ...
    async def record_recovery(self, incident_fingerprint: str, degraded_seconds: float, *, at: datetime) -> None: ...
    async def summary(self, day: date) -> DailyOperationalSummary: ...
```

Use event IDs and market IDs as idempotency keys so replay after restart cannot double-count orders, fills, rejects, markets, or recoveries. Obtain realized/unrealized P&L from authoritative state/snapshot at summary creation rather than incrementing P&L from events.

- [ ] **Step 1: Write failing metric-idempotency tests**

Record the same event and market rotation before and after repository restart. Assert each counter increments once. Test UTC day boundaries, recovery duration accumulation, and that rejected orders are distinct from submission/fill counts.

- [ ] **Step 2: Write failing daily-summary tests**

Use a fake clock at the configured UTC hour. Assert one summary per UTC day, restart-safe outbox deduplication, and fields for uptime, state, markets, orders, fills, rejects, realized/unrealized P&L, recovery count, degraded seconds, pending alerts, disk, and remaining lease time.

- [ ] **Step 3: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_operational_metrics.py tests/test_daily_summary.py
```

Expected: FAIL because durable operational metrics do not exist.

- [ ] **Step 4: Implement metrics and summary scheduling**

Subscribe `OperationalMetrics.record_event` to the event bus and call explicit market/recovery methods from rotation/runtime coordination. Add one daily-summary cycle to the notification loop. Use a repository idempotency key for the UTC summary date so restart around midnight cannot duplicate it. Enqueue through `AlertService`; never call Telegram directly.

- [ ] **Step 5: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_operational_metrics.py tests/test_daily_summary.py tests/test_notification_outbox.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the task**

```bash
git add reliability/metrics.py persistence/operations.py app/bootstrap.py app/loops.py \
  tests/test_operational_metrics.py tests/test_daily_summary.py
git commit -m "feat: send restart-safe daily operations summary"
```

---

### Task 13: Process Supervision, Deployment Examples, and Intervention Runbooks

**Files:**
- Modify: `Dockerfile`
- Create: `docker-compose.example.yml`
- Create: `deploy/polymarket-bot.service`
- Create: `deploy/polymarket-bot.env.example`
- Modify: `README.md:529-564`
- Modify: `docs/live-runbook.md`
- Create: `docs/unattended-operations-runbook.md`
- Create: `tests/test_deployment_files.py`

**Interfaces:**
- Consumes: `dashboard.main`, auto-resume lease behavior, `scripts.healthcheck --kind liveness`, persistent `BOT_DATA_DIR`, and nonzero fatal exit behavior.
- Produces: restartable deployment examples and exact operator procedures for every urgent alert category.

- [ ] **Step 1: Write failing deployment-file tests**

Create `tests/test_deployment_files.py` to parse/assert:

- Docker has a liveness `HEALTHCHECK` invoking `python -m scripts.healthcheck --kind liveness`;
- Compose uses `restart: unless-stopped`, a persistent `/data` volume, `BOT_DATA_DIR=/data`, an env file, and a log-size limit;
- systemd uses `Restart=on-failure`, `RestartSec=5`, a dedicated environment file, a non-root `User`, persistent working/data paths, and a start-limit burst that avoids a tight crash loop;
- neither deployment file contains credentials or placeholder real secrets;
- service commands run the dashboard/runtime path that performs lease-based auto-resume rather than passing `--live` as fresh authorization.

- [ ] **Step 2: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_deployment_files.py
```

Expected: FAIL because the deployment examples and health check are missing.

- [ ] **Step 3: Implement deployment supervision**

Update the Docker command to run the operator dashboard process. Keep its loopback-only binding; document Linux `network_mode: host`/SSH access rather than weakening the host validation. Mount config read-only and data read-write. The health check reads the atomic health file, so it does not need dashboard network access.

The systemd unit must restart only after nonzero fatal exit. A safe `HALTED` dashboard stays alive for operator inspection and does not enter a restart loop. Use `TimeoutStopSec` greater than the bot shutdown timeout so final cancel/snapshot work can complete.

- [ ] **Step 4: Write exact intervention runbooks**

For each category below, document: Telegram message fields, authoritative exchange checks, dashboard evidence, cancellation steps, snapshot/journal files to preserve, condition that proves resolution, preflight requirement, kill-switch clearing procedure, and lease reissue procedure.

- authentication/signature/compliance;
- confirmed reconciliation or accounting divergence;
- unprotected position/exhausted exits;
- cancellation failure;
- critical task restart budget exhausted;
- persistence/disk failure;
- auto-resume rejection;
- lease expiration;
- Telegram backlog.

Every playbook must explicitly say not to clear the kill switch until the named authoritative check passes. Add startup, ordinary host-restart, emergency halt, and rollback commands using the checked-in service names and paths.

- [ ] **Step 5: Run focused and full tests**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_deployment_files.py tests/test_healthcheck.py
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the task**

```bash
git add Dockerfile docker-compose.example.yml deploy README.md docs/live-runbook.md \
  docs/unattended-operations-runbook.md tests/test_deployment_files.py
git commit -m "ops: supervise and document multi-day bot operation"
```

---

### Task 14: Fault-Injection Harness, Soak Qualification, and Final Release Gate

**Files:**
- Create: `reliability/qualification.py`
- Create: `scripts/reliability_soak.py`
- Create: `tests/test_reliability_qualification.py`
- Create: `tests/test_unattended_fault_injection.py`
- Modify: `README.md`
- Modify: `docs/unattended-operations-runbook.md`

**Interfaces:**
- Consumes: complete supervised runtime, fake clocks/transports, automatic market rotation, durable operations repository, health snapshots, retention report, and notification outbox.
- Produces: deterministic accelerated fault-injection tests, a resumable dry-run soak command, and machine-readable JSON qualification artifacts.

Define a machine-readable result:

```python
class QualificationReport(BaseModel):
    run_id: str
    mode: Literal["accelerated", "wall_clock"]
    started_at: datetime
    completed_at: datetime
    markets_completed: int
    orders_submitted: int
    fills_accounted: int
    duplicate_orders: int
    orphan_open_orders: int
    accounting_errors: int
    injected_faults: dict[str, int]
    recovered_faults: dict[str, int]
    urgent_alerts_expected: int
    urgent_alerts_delivered: int
    max_memory_mib: float
    final_memory_mib: float
    max_disk_mib: float
    passed: bool
    failures: list[str]
```

- [ ] **Step 1: Write failing accelerated fault-injection tests**

Create `tests/test_unattended_fault_injection.py` using fake transports and a fake clock. Run enough virtual time for 500 market rotations while injecting:

- WebSocket disconnect/recovery;
- five-minute REST fallback period;
- CLOB/Data API 429, timeout, and 5xx sequences;
- Gamma discovery delay across a boundary while flat;
- Telegram outage spanning a process restart;
- one ordinary process restart with valid lease;
- one unexpected task crash followed by successful restart;
- snapshot/archive transient write failure;
- disk warning and degraded thresholds below halt.

Assert no duplicate order identities, no missing/double fill deltas, no orphan open orders, bounded collection sizes, recovered alerts, and final `RUNNING` state. Add separate short tests proving accounting failure, fourth task crash, exposed authoritative-state outage, and 95% disk each halt and cannot auto-resume.

- [ ] **Step 2: Write failing qualification-evaluator tests**

Test that `QualificationReport.passed` is false for any duplicate order, orphan order, accounting error, missing expected urgent alert, unbounded-memory threshold breach, or incomplete required fault injection. An accelerated run passes only at 500 or more completed rotations. A wall-clock release report passes only at 72 or more hours and 288 or more markets.

- [ ] **Step 3: Run tests and confirm failure**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_reliability_qualification.py tests/test_unattended_fault_injection.py
```

Expected: FAIL because qualification modules do not exist.

- [ ] **Step 4: Implement a non-live soak runner**

`scripts/reliability_soak.py` must refuse `bot.mode=live`. Support:

```text
--mode accelerated|wall-clock
--markets 500
--duration-hours 72
--inject-faults
--resume-run-id <id>
--output-dir data/qualification
```

Write progress atomically after each market so a host restart can resume a wall-clock dry run without erasing evidence. Record process RSS through `resource.getrusage` and normalize macOS bytes versus Linux KiB before reporting MiB; record disk use through `shutil.disk_usage`. Do not claim leak-free operation from RSS alone; evaluate bounded hot collection counts and a configured post-warm-up RSS growth ceiling together.

- [ ] **Step 5: Run automated verification and accelerated qualification**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/polymarket-bot-pyc \
  .venv/bin/python -m compileall -q app clients config dashboard execution models \
  notifications persistence portfolio reliability risk scripts state strategies tests

.venv/bin/python -m pytest -q -p no:cacheprovider

.venv/bin/python -m scripts.reliability_soak \
  --mode accelerated --markets 500 --inject-faults \
  --output-dir data/qualification

git diff --check
```

Expected: compilation succeeds, the entire test suite passes, the accelerated report has `passed=true`, and `git diff --check` prints nothing.

- [ ] **Step 6: Commit automated qualification support**

```bash
git add reliability/qualification.py scripts/reliability_soak.py \
  tests/test_reliability_qualification.py tests/test_unattended_fault_injection.py \
  README.md docs/unattended-operations-runbook.md
git commit -m "test: qualify unattended operation under injected faults"
```

- [ ] **Step 7: Execute the 24-hour dry-run gate**

Run outside an agent test timeout in a supervised terminal/service:

```bash
.venv/bin/python -m scripts.reliability_soak \
  --mode wall-clock --duration-hours 24 --inject-faults \
  --output-dir data/qualification
```

Expected: the report records at least 96 markets, every scheduled recoverable fault recovers, no duplicate/orphan/accounting errors exist, Telegram backlog drains after the injected outage, and resource bounds hold. Run safety-halt fault drills as separate short qualification runs because an intentional safety halt must not be auto-cleared to continue a 24-hour run. Preserve every JSON report; do not commit runtime data.

- [ ] **Step 8: Execute the 72-hour dry-run release gate**

After the 24-hour report passes, run:

```bash
.venv/bin/python -m scripts.reliability_soak \
  --mode wall-clock --duration-hours 72 --inject-faults \
  --output-dir data/qualification
```

During this run perform the spec-required ordinary process restart, WebSocket outage, temporary Data API outage, Gamma delay, and Telegram outage. Expected: at least 288 markets and `passed=true`. If any acceptance field fails, unattended live use remains blocked; fix the cause and restart the 72-hour qualification from a new run ID.

- [ ] **Step 9: Perform supervised live canary gates**

Only after both dry-run reports pass:

1. run current live preflight;
2. issue the shortest allowed live lease;
3. supervise one low-notional market;
4. inspect authoritative exchange orders/positions/fills and local reconciliation;
5. supervise four consecutive markets;
6. run a monitored 24-hour live lease;
7. authorize a 72-hour unattended lease only when all stages have no unresolved safety findings.

These are operator-controlled live steps. An agent must not submit, enable, or extend live trading merely because the automated suite passes.

---

## Final Review Checklist

Before presenting this branch for merge or PR, verify all of the following from fresh output:

- [ ] Full pytest suite passes.
- [ ] `compileall` passes.
- [ ] `git diff --check` passes.
- [ ] Every critical task appears in supervisor health and cannot die silently.
- [ ] BUY entry is blocked in every non-running operational state.
- [ ] Every safety halt revokes the live lease before process restart.
- [ ] Ordinary restart with a valid lease repeats full preflight and reconciliation.
- [ ] Outbox alerts survive repository/process restart and redact all secrets.
- [ ] REST fallback is exit/reconciliation-only and never generates entries.
- [ ] Retention cannot delete accounting state before durable archive.
- [ ] Liveness, readiness, and trading readiness report different concepts correctly.
- [ ] Deployment supervisor cannot override a kill switch or revoked lease.
- [ ] Accelerated 500-market qualification report passes.
- [ ] 24-hour and 72-hour dry-run reports are preserved and pass before unattended live authorization.
- [ ] No live credentials, wallet material, or runtime qualification data are committed.

## Spec-to-Task Coverage

| Approved design requirement | Implementation task |
|---|---|
| Reliability configuration and typed operating states | Task 1 |
| Durable leases, incidents, outbox, archives, and metrics | Tasks 2, 9, 12 |
| Shared backoff, incident mapping, and recovery decisions | Task 3 |
| Durable Telegram delivery, deduplication, retry, redaction, and test alert | Task 4 |
| Critical-task supervision and restart budgets | Task 5 |
| Isolated loops, degraded entry pause, centralized halt ordering, and fatal exit | Task 6 |
| WebSocket health and exit-only REST fallback | Task 7 |
| 72-hour live lease, safe auto-resume, warnings, expiry, and process ownership | Task 8 |
| Bounded memory, archive-before-prune, journal rotation, and disk thresholds | Task 9 |
| Liveness, service readiness, trading readiness, and dashboard visibility | Task 10 |
| Verified human recovery without automatic restart or lease restoration | Task 11 |
| Restart-safe counters and daily Telegram summary | Task 12 |
| Docker/systemd supervision and exact intervention runbooks | Task 13 |
| Fault injection, 500-market acceleration, 24/72-hour dry runs, and live canaries | Task 14 |
