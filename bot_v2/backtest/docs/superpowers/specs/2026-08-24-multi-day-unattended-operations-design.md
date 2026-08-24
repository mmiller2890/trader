# Multi-Day Unattended Operations Design

**Status:** Approved design, awaiting implementation-plan review
**Date:** 2026-08-24
**Scope:** Runtime reliability, safe automatic recovery, durable Telegram intervention alerts, bounded storage, deployment supervision, and endurance qualification for the Polymarket BTC 15-minute bot.

## Objective

The bot must run live for at least 72 continuous hours under ordinary network and exchange instability without routine operator intervention. It must recover automatically from explicitly classified transient faults, remain fail-closed for safety faults, and notify the operator through Telegram when human action is actually required.

This design does not promise uninterrupted trading through every failure. It promises that recoverable failures are handled automatically, unsafe states never resume automatically, and the operator receives a durable, actionable alert when intervention is required.

## Non-Goals

- Automatically clearing a kill switch after an accounting, authentication, compliance, position, or order-safety failure.
- Continuing to open positions when authoritative account state or market data is stale.
- Building a distributed orchestration platform or introducing an external message broker.
- Adding email in the first implementation. Telegram is the required primary channel; the notifier interface permits a later email fallback.
- Guaranteeing exchange availability, fills, profitable trading, or delivery by Telegram while Telegram itself is unavailable.
- Running live forever without renewed operator authorization. The initial live operating lease expires after 72 hours by default.

## Operating Contract

### Availability target

- Complete a continuous 72-hour dry run across approximately 288 BTC 15-minute markets before multi-day unattended live use.
- Recover from an ordinary process crash within 60 seconds when a valid live lease remains and all fresh safety checks pass.
- Recover from a transient WebSocket, Gamma API, Data API, CLOB API, or Telegram outage without operator action when the fault remains inside its recovery budget.
- Keep memory and disk consumption bounded after warm-up.

### Safety invariants

1. No BUY may be submitted while runtime state is `DEGRADED`, `HALTING`, `HALTED`, or `FAILED`.
2. Reduce-only exits may continue while degraded only when their market data, position quantity, balance, and exchange submission dependencies are independently fresh.
3. An automatic live resume requires an unexpired lease, an unchanged configuration fingerprint, an inactive persisted kill switch, and a fresh successful live preflight plus startup reconciliation.
4. A safety fault revokes the live lease before the process can be restarted.
5. A process supervisor may restart the process, but it may not override a revoked lease or latched kill switch.
6. Confirmed fills remain idempotent across retries and restarts.
7. A critical background task may not disappear while the runtime continues to report healthy or running.
8. Notification delivery failure may not block risk, reconciliation, exit, or shutdown behavior.
9. A new unattended live lease may not be issued until the configured Telegram destination has accepted a test alert within the preceding five minutes and the durable outbox is writable.

## Runtime States

Extend the runtime state model to use the following states:

- `STOPPED`: no bot services are active.
- `STARTING`: bootstrap, preflight, restoration, and reconciliation are in progress.
- `RUNNING`: every required task is healthy and entries are permitted subject to normal risk checks.
- `DEGRADED`: at least one recoverable dependency is unhealthy; entries are paused while recovery continues.
- `HALTING`: the kill switch is latched and cancellation/exit/persistence work is in progress.
- `HALTED`: safety actions completed or reached their timeout; operator intervention is required.
- `STOPPING`: operator-requested graceful shutdown is in progress.
- `FAILED`: the runtime cannot safely continue and should exit nonzero for the process supervisor.

Runtime state and trading permission are separate. `RUNNING` does not bypass pre-trade risk, and `DEGRADED` does not imply that reduce-only exits are automatically safe.

## Architecture

### 1. Supervised task isolation

Replace the monolithic housekeeping responsibility with separately named loops:

- `reconciliation-loop`
- `runtime-risk-loop`
- `position-exit-loop`
- `strategy-timer-loop`
- `snapshot-retention-loop`
- `market-rotation-loop`
- `notification-delivery-loop`

A `RuntimeSupervisor` owns every critical task. Each task specification declares whether it is restartable, its restart budget, and its heartbeat deadline. The supervisor consumes task-completion callbacks and heartbeat updates. An unexpected task exit is always converted into a typed incident; it can never leave the dashboard reporting `RUNNING` silently.

Restartable tasks may restart at most three times in a rolling ten-minute window. The fourth unexpected exit is a safety fault: revoke the lease, latch the kill switch, cancel open orders, persist diagnostics, enqueue an urgent alert, transition to `FAILED`, and request process termination with a nonzero status.

The WebSocket manager remains internally reconnecting, but exposes a typed health snapshot and completion future so the supervisor can distinguish a disconnected/retrying transport from a dead transport task.

### 2. Fault classification and recovery budgets

All loops report a typed `OperationalIncident` rather than directly making inconsistent retry/halt decisions. The incident contains component, category, severity, first/last occurrence, consecutive count, market/order context, sanitized reason, and recommended action.

The shared fault policy maps incidents to:

- `RETRY`: remain operational when safe and retry with jittered exponential backoff.
- `DEGRADE`: pause new entries, keep safe recovery and exit activity running, and alert if the degraded duration crosses its threshold.
- `HALT`: revoke authorization, latch the kill switch, cancel orders, persist, alert, and require an operator.

Default recovery rules:

| Fault | Initial action | Degraded threshold | Halt threshold |
|---|---|---|---|
| WebSocket disconnect | Retry at 1, 2, 4, 8, 16, then 30 seconds with ±20% jitter | 30 seconds; enable REST market-data fallback | No automatic halt while flat; halt when an open position cannot be priced or exited inside its exit safety window |
| HTTP timeout, 429, or 5xx | Retry with the same capped backoff | Two consecutive failed operating cycles | Five minutes without authoritative account state when positions or open orders exist |
| Gamma market discovery failure | Retry through the market boundary; stop entries near boundary | Immediate when no validated next market exists | Halt only when an open position reaches its market end without a safe exit path |
| Reconciliation transport failure | Pause entries and retry | First failed reconciliation cycle | Five minutes while exposed; remain degraded while flat |
| Confirmed account divergence | Use the existing confirmation grace | After the grace expires | Immediate after a second authoritative confirmation of the same unresolved divergence |
| Authentication, signature, geoblock, balance, or allowance failure | Stop entries | Immediate | Immediate for authentication/signature/geoblock; balance/allowance halts only if required for an outstanding safe exit, otherwise remains degraded |
| Accounting invariant or unknown confirmed fill | No retry that mutates accounting | Immediate | Immediate |
| Exit attempts exhausted or unprotected position | Cancel conflicting orders and attempt final reduce-only handling when permitted | Immediate | Immediate after the configured exit budget is exhausted |
| Critical task crash | Restart task | First crash | Fourth crash in ten minutes |
| Disk use | Continue with retention pass | 90%; pause entries | 95% or persistence write failure |
| Telegram unavailable | Persist and retry outbox | Oldest urgent message age exceeds two minutes | Never halt trading solely because Telegram is unavailable |

REST market-data fallback polls only the currently subscribed tokens, uses the same normalization and freshness rules as WebSocket data, and is used for safe exit/reconciliation decisions. It does not silently widen strategy eligibility while the primary stream is degraded.

### 3. Time-limited live operating lease

Manual `START LIVE` creates a durable `LiveOperatingLease` with:

- a random lease identifier;
- UTC `issued_at` and `expires_at` timestamps;
- the complete preflight-relevant configuration fingerprint;
- mode `live`;
- status `active`, `revoked`, or `expired`;
- revocation reason and timestamp when applicable.

The default lease duration is 72 hours and may be configured between 1 and 168 hours. The lease is stored atomically in the existing SQLite data file. It contains no private key, API secret, passphrase, bot token, or other credential.

Dashboard startup calls `attempt_auto_resume()`. Resume is allowed only when all of these checks pass:

1. lease exists, is active, and is not expired;
2. persisted kill switch is inactive;
3. current config fingerprint matches the lease fingerprint;
4. live mode remains fully armed;
5. fresh geoblock, credential, CLOB health, balance, allowance, and market-discovery checks pass;
6. snapshot restoration and startup reconciliation pass;
7. no unresolved notification or persistence failure is classified as safety-critical.

Automatic resume requires a writable durable outbox but does not require Telegram to be reachable at that exact moment; a temporary Telegram outage is recoverable and queued alerts must deliver after recovery. Manual lease issuance requires the recent successful test delivery described in the safety invariants.

If any check fails, the runtime remains stopped or halted, records the sanitized reason, and enqueues an urgent Telegram alert when possible. Operator stop, emergency halt, safety halt, configuration mutation, or explicit return to dry run revokes the lease. Normal process shutdown caused by a host restart does not revoke it.

The running bot warns at 24 hours and one hour before expiration. At expiration it stops new entries, attempts normal configured exits, cancels remaining open orders, persists state, sends an urgent alert, and halts. It does not extend its own lease.

### 4. Durable Telegram outbox

The event bus may continue distributing ordinary in-process events, but intervention alerts first write a durable outbox row in SQLite. The outbox schema includes alert ID, incident fingerprint, severity, payload, creation time, next-attempt time, attempt count, delivered time, and last sanitized error.

Delivery behavior:

- Use one long-lived `httpx.AsyncClient` owned by the notifier.
- Retry with capped exponential backoff and ±20% jitter.
- Mark delivered only after Telegram returns a successful HTTP response.
- Resume undelivered rows after process restart.
- Deduplicate identical active incidents for 15 minutes while incrementing an occurrence counter.
- Send a recovery message when a warning/degraded incident returns to healthy.
- Never include secrets, raw signed requests, private addresses configured as redacted, or exception strings that have not passed the sanitizer.

The dashboard provides an exact-confirmation `SEND TEST` action. It durably queues a test alert, attempts immediate delivery through the normal worker, and records the successful Telegram acceptance time. Live lease issuance requires this success to be no more than five minutes old.

Alert levels:

- `URGENT`: halted/failed runtime, kill switch, unprotected position, failed cancellation, accounting invariant, confirmed reconciliation divergence, authentication/compliance failure, automatic-resume rejection, or lease expiration.
- `WARNING`: degraded for two minutes, repeated reconnects, discovery delay, disk above 80%, notification backlog, or lease expiring within 24 hours.
- `INFO`: manual start, safe automatic resume, recovery completed, normal stop, and daily summary.

The daily summary reports uptime, current state, markets handled, submitted orders, fills, rejected orders, realized and unrealized P&L, recoveries, degraded duration, pending alert count, disk use, and remaining lease time.

### 5. Bounded state, journal, and disk use

Default retention policy:

- Keep order books and market snapshots for the current and immediately previous automatic market only.
- Keep at most 10,000 in-memory signals and prune signals older than 24 hours.
- Keep fill checkpoints for seven days after the related market closes; never prune checkpoints for an open order, active position, or unresolved reconciliation incident.
- Keep 90 UTC days of daily realized P&L in the hot snapshot; archive older entries in SQLite.
- Keep the most recent 100 closed lifecycle records in the snapshot and archive older records in SQLite.
- Retain delivered notification-outbox rows for 30 days and operational incidents for 90 days.
- Rotate event journals daily or at 50 MiB, whichever comes first; retain 14 days and cap total journal storage at 500 MiB.
- Run retention during startup and every hour.

Retention failures are incidents. Disk use above 80% sends a warning, 90% pauses entries, and 95% latches the kill switch because durable accounting and alerting can no longer be trusted.

### 6. Health and deployment supervision

Expose distinct health concepts:

- **Liveness:** the process event loop and supervisor are responsive.
- **Readiness:** required services are initialized and no critical task is dead.
- **Trading readiness:** state is `RUNNING`, lease is valid in live mode, data/account truth is fresh, and the kill switch is inactive.
- **Component health:** last heartbeat, consecutive failures, restart count, and degraded reason for every supervised task.

The dashboard state and health endpoint include runtime state, incident summary, task health, WebSocket/fallback source, current market, last reconciliation, outbox depth/age, disk use, lease expiry, and whether automatic resume is eligible. Health output never includes secrets.

The Docker image gains a liveness `HEALTHCHECK`. A checked-in Compose example and systemd unit use `restart: unless-stopped` or `Restart=on-failure`, bounded restart delays, persistent data volumes, environment-file injection, and log limits. The bot process exits nonzero only after a fatal supervised failure; a latched safety state prevents the restarted process from resuming live trading.

## Data Flow

### Recoverable outage

1. A component emits a typed incident.
2. The fault policy returns `RETRY` or `DEGRADE`.
3. The supervisor records the incident and pauses new entries when degraded.
4. The component retries with backoff; safe exits/reconciliation continue through healthy dependencies or REST fallback.
5. At two minutes degraded, a durable warning enters the outbox.
6. Recovery clears the degraded contribution, sends one recovery notice, and returns to `RUNNING` only after every critical component is healthy.

### Safety failure

1. The fault policy returns `HALT`.
2. The runtime atomically revokes the live lease and latches the kill switch.
3. It persists state and the incident before attempting network actions.
4. It cancels open orders with the configured timeout and performs only independently safe reduce-only handling.
5. It enqueues an urgent alert regardless of cancellation outcome.
6. It transitions to `HALTED` or `FAILED`; automatic process restart cannot clear the state.

### Ordinary process restart

1. Docker/systemd restarts the dashboard process.
2. The controller reads the lease and persisted snapshot.
3. It performs the complete auto-resume gate.
4. On success it starts live services and sends an informational resume message.
5. On failure it remains safe and sends an urgent rejection message when the outbox can deliver.

## Configuration

Add a dedicated `reliability` configuration section with these defaults:

```yaml
reliability:
  live_lease_hours: 72
  task_restart_limit: 3
  task_restart_window_seconds: 600
  degraded_alert_after_seconds: 120
  authoritative_state_halt_after_seconds: 300
  rest_fallback_after_seconds: 30
  retry_initial_seconds: 1
  retry_max_seconds: 30
  retry_jitter_ratio: 0.20
  disk_warning_percent: 80
  disk_degraded_percent: 90
  disk_halt_percent: 95
  retention_interval_seconds: 3600
  signal_retention_count: 10000
  signal_retention_hours: 24
  fill_checkpoint_retention_days: 7
  realized_pnl_hot_days: 90
  closed_lifecycle_hot_count: 100
  journal_rotation_mib: 50
  journal_retention_days: 14
  journal_total_limit_mib: 500
```

Extend notification configuration with these defaults:

```yaml
notifications:
  durable_outbox_enabled: true
  telegram_deduplication_seconds: 900
  alert_retry_initial_seconds: 2
  alert_retry_max_seconds: 300
  delivered_outbox_retention_days: 30
  daily_summary_hour_utc: 0
```

Undelivered alerts retry indefinitely at the capped interval until delivered or explicitly acknowledged by an operator. Secrets remain environment-only.

## Testing Strategy

### Unit tests

Use fake clocks, deterministic jitter sources, fake disk probes, and fake transports to test:

- every fault-policy mapping and threshold boundary;
- rolling task-restart budgets;
- state transitions and entry/exit permissions;
- lease creation, revocation, expiration, fingerprint mismatch, and safe resume gates;
- outbox persistence, retry, deduplication, recovery messages, and secret redaction;
- retention without pruning active accounting identities;
- liveness/readiness/trading-readiness separation.

### Integration and fault-injection tests

Exercise WebSocket disconnects, REST fallback, HTTP 429/5xx/timeouts, malformed responses, delayed reconciliation, Telegram downtime, snapshot write failure, disk thresholds, process interruption, task crashes, and market-boundary discovery failures. Assert that entries pause, safe exits remain possible, confirmed fills remain idempotent, alerts persist, and unsafe auto-resume never occurs.

### Soak qualification

1. Accelerated deterministic run across at least 500 market rotations with periodic injected failures.
2. Continuous 24-hour dry run.
3. Continuous 72-hour dry run covering approximately 288 real BTC market rotations.
4. During the 72-hour run, perform at least one process restart, one five-minute WebSocket outage, one temporary Data API outage, one Gamma discovery delay, and one Telegram outage.
5. Confirm no duplicate orders, no missing fill accounting, no orphaned open orders, bounded memory after warm-up, bounded disk use, and correct incident/daily-summary delivery.

### Live rollout

1. Run the complete existing and new automated suite.
2. Pass the accelerated and 72-hour dry-run gates.
3. Run a low-notional supervised live canary for one market.
4. Run a supervised live session across four consecutive markets.
5. Run a 24-hour live lease with active operator monitoring.
6. Enable a 72-hour unattended lease only after all prior gates pass without unresolved safety findings.

## Operator Runbooks

Document one exact playbook for each urgent category:

- authentication or compliance failure;
- confirmed reconciliation/accounting divergence;
- unprotected position or exhausted exits;
- cancellation failure;
- critical task crash budget exhausted;
- persistence/disk failure;
- automatic-resume rejection;
- lease expiration.

Each playbook identifies the authoritative exchange checks, dashboard evidence, safe cancellation procedure, snapshot/journal artifacts to preserve, conditions for clearing the kill switch, required preflight, and exact procedure for issuing a new lease. No runbook instructs an operator to clear a kill switch before the underlying condition is verified resolved.

The dashboard provides a guarded intervention recovery action. It requires a fresh passing preflight, successful authoritative reconciliation, writable persistence/outbox, disk below the warning threshold, no unsafe open-order or position condition, and exact `CLEAR HALT <incident-suffix>` confirmation. It marks the selected incident resolved and clears the persisted kill switch, but it never starts trading or restores the revoked lease. The operator must separately send a Telegram test and issue a new live lease.

## Acceptance Criteria

The feature is complete only when:

- the runtime cannot silently lose a critical task;
- recoverable faults enter `DEGRADED` and recover without weakening entry safeguards;
- safety faults persistently halt and revoke auto-resume authorization;
- a valid lease can safely resume after an ordinary process restart;
- Telegram intervention alerts survive process and Telegram outages;
- unattended lease issuance is blocked until a recent Telegram test alert succeeds;
- health output accurately distinguishes process, service, and trading readiness;
- state, journal, and disk growth are bounded;
- the full existing test suite and all new reliability tests pass;
- the accelerated 500-rotation soak passes;
- the documented 72-hour dry-run qualification passes before unattended live use.
- a halted operator can clear the latch only through the guarded recovery action and must separately authorize a new lease.

## Compatibility and Migration

Existing configurations without a `reliability` section receive the safe defaults above. Existing snapshots without lease, incident, or archival metadata restore normally but are not eligible for automatic live resume. Existing direct Telegram configuration is migrated to the durable notifier without changing environment variable names. Dry-run startup remains manual by default, and live startup remains manual unless a valid durable lease passes every resume gate.
