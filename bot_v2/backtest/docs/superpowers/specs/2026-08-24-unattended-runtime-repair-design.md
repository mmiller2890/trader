# Unattended Runtime Repair Design

## Goal

Make the unattended runtime fail safely, supervise its real tasks, preserve a
valid live authorization across ordinary process restarts, revoke that
authorization on operator or safety stops, and ensure qualification exercises
real accounting state.

## Scope

This repair covers the six integration defects found in commits
`origin/main..HEAD`:

1. incident policy evaluation fails or receives incomplete recovery context;
2. supervised task adapters misroute loop arguments and discard heartbeats;
3. fatal supervisor state never terminates `BotRuntime`;
4. the live lease service is not connected to activation or restart;
5. safety incidents and lease revocation are not persisted as one durable flow;
6. the accelerated qualification harness does not await accounting mutations.

Unrelated trading strategy, order construction, dashboard styling, and
backtest behavior remain out of scope.

## Runtime supervision

Default loop `TaskSpec` factories will receive the supervisor's real
`stop_event` and `heartbeat` callback. The health-report loop will use the same
factory interface as every other loop. `_cycle` will await one heartbeat per
successful cycle and will not create discarded coroutine objects.

`BotRuntime` will own a monitor task for `RuntimeSupervisor.wait_fatal()`.
When a restart budget or heartbeat policy becomes fatal, the monitor will run
the same safety ordering used by other halts, mark the runtime `FAILED`, and set
the terminal event so the headless runner exits nonzero. Intentional shutdown
will cancel this monitor without reporting another incident.

## Incident handling and policy context

Actionable incidents will be persisted to `OperationsRepository` before their
policy side effects. Urgent incident delivery will use the durable alert
outbox. Repeated observations will retain a stable incident identity and
increase their count so the dashboard can select the active halt incident.

`BotRuntime` will construct `RecoveryContext` from runtime evidence rather
than defaults: open-position state, repeated incident observations,
authoritative-state outage duration, task crash count supplied by the
supervisor, and current disk utilization. A forced fatal supervisor decision
will bypass ordinary retry policy while still using the centralized halt
ordering.

Safety ordering is:

1. persist the incident;
2. revoke an active live lease for halt/fatal actions;
3. latch and snapshot the kill switch;
4. enqueue the durable incident alert and emit the runtime event;
5. cancel live open orders with the configured timeout;
6. expose `HALTED` for recoverable intervention or `FAILED` for fatal process
   termination.

## Live lease lifecycle

Manual live activation remains guarded by a fresh dashboard preflight and the
exact confirmation phrase. After runtime bootstrap repeats preflight and
startup reconciliation successfully, the controller issues a new durable
lease and emits `LIVE_LEASE_ISSUED`.

Dashboard process startup will attempt automatic resume only when the loaded
configuration is live. It will validate all of the following before starting:

- an active, unexpired lease exists;
- the lease fingerprint matches the current safety-relevant configuration;
- the persisted snapshot does not contain a latched kill switch.

Runtime bootstrap then repeats exchange preflight and reconciliation. A failed
gate leaves trading stopped, records an `AUTO_RESUME_REJECTED` event, and does
not convert the restart into fresh authorization.

Operator stop, emergency halt, safety halt, and live-affecting configuration
changes revoke the lease. Ordinary host/process shutdown preserves a valid
lease. The dashboard lifespan therefore uses a dedicated process-shutdown
path, while the Stop API retains operator-stop semantics. The headless
`--resume-live` flag will perform the same lease validation; `--live` remains
fresh manual authorization.

## Qualification

The accelerated reliability harness will await both buy and sell fill
applications and derive accounting counts from completed mutations. Its tests
will assert that positions/lifecycles actually change, preventing a counter-
only false pass.

## Testing

Each repair begins with a regression test that fails against the current
implementation. Coverage will include:

- accounting incidents reach a durable halt without `NameError`;
- every default loop receives and advances its real heartbeat;
- the health loop starts with correctly typed arguments;
- exhausted restart budgets make `BotRuntime.wait()` return `FAILED`;
- manual live start issues a lease;
- valid restart authorization resumes live operation;
- missing, expired, mismatched, or kill-switched state rejects resume;
- operator/safety halt revokes the lease and persists a clearable incident;
- process shutdown preserves the lease;
- accelerated qualification applies real fills;
- the complete suite passes with no unawaited-coroutine warnings.

## Safety constraints

- No restart or command-line flag may create fresh live authorization.
- No halt is considered complete before its durable state is written.
- Failure to persist a required safety transition fails closed.
- Secrets and upstream exception messages are not stored in incidents or
  alerts.
