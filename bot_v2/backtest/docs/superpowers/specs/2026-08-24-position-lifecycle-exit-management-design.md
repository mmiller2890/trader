# Position Lifecycle and Exit Management Design

**Status:** Approved by the operator's request for a detailed implementation plan after reviewing the proposed complete position-management scope.

## Goal

Turn confirmed entry fills into durable, immediately usable inventory and manage that inventory until it is completely sold or explicitly classified as unsellable dust. The bot must be able to exit because of a strategy reversal, take profit, stop loss, maximum holding time, or an approaching BTC 15-minute market deadline without double-counting fills, overselling, retrying an unknown order, or trusting a lagging account API over a just-confirmed exchange response.

## Non-Goals

- Guaranteeing an exchange fill or available counterparties.
- Short selling in live or dry-run modes.
- Multi-lot tax accounting or tax reporting.
- Cross-market portfolio optimization.
- Averaging down based on exit-policy state.
- Replacing the existing offline backtest ledger.

## Recommended Approach

Add a small position-lifecycle subsystem around the existing `Position`, `OrderResult`, state store, tracker, reconciliation service, and execution router. A confirmed cumulative fill is applied through one idempotent accounting function. A separate pure exit policy decides when a long position should be reduced, while a coordinator reserves a single exit attempt and emits a reduce-only SELL signal. The existing router remains the only path allowed to build, risk-check, submit, track, journal, and reconcile an order.

This approach is preferable to putting exits directly in `SpikeStrategy` because fill accounting, deadlines, retries, and recovery are portfolio responsibilities rather than price-signal responsibilities. It is also preferable to polling the Data API before every SELL because the Data API can lag a confirmed CLOB response.

## Initial Policy Values

Add a `position_management` configuration section with these checked-in dry/live defaults:

```yaml
position_management:
  enabled: true
  take_profit_bps: 300
  stop_loss_bps: 200
  max_hold_seconds: 180
  exit_before_market_end_seconds: 60
  exit_retry_interval_seconds: 2
  max_exit_attempts: 3
  position_confirmation_grace_seconds: 30
  exit_time_in_force: IOC
  exit_on_strategy_sell: true
  liquidate_full_position: true
```

`IOC` is the internal name already mapped by the CLOB adapter to Polymarket `FAK`. Entry orders remain `FOK`. Exit orders therefore accept an immediate partial fill and cancel the remainder; the coordinator can submit a later attempt only for the confirmed remaining inventory. These policy values are starting values for the existing one-dollar, BTC 15-minute profile and must be tuned with backtests and shadow runs rather than treated as profitable parameters.

## Domain Model

Extend `TradeSignal` with optional execution intent:

- `requested_size: Decimal | None`
- `reduce_only: bool`
- `time_in_force: OrderTimeInForce | None`

Add `SignalType.POSITION_EXIT`. A normal spike BUY keeps all defaults. A managed exit uses the current position quantity, `reduce_only=true`, and `IOC`.

Add these position lifecycle models:

- `ExitReason`: `strategy_signal`, `take_profit`, `stop_loss`, `max_hold`, `market_expiry`.
- `FillCheckpoint`: one record per exchange order identity containing cumulative accounted size and cumulative accounted notional. The cumulative notional is required to calculate an accurate delta when a later partial-fill update has a different average price.
- `PositionLifecycle`: first-open time, last-fill time, known market end, last exit reason, pending exit client ID, last exit attempt time, attempt count, confirmation deadline, and optional close time.
- `FillApplication`: the newly applied size/notional delta plus the resulting position; `duplicate=true` when the cumulative result was already accounted.

The order identity is `exchange_order_id` when present and otherwise the stable `client_order_id` for dry-run simulated fills. An `UNKNOWN`, `SUBMITTED`, `REJECTED`, `FAILED`, or `CANCELLED` result never changes inventory.

## Fill Accounting

`InMemoryStateStore.apply_confirmed_fill` is the single atomic mutation boundary. It accepts `FILLED` and `PARTIALLY_FILLED`; it also accepts `SIMULATED` only when the store mode is `dry_run`. It requires market, token, side, positive cumulative filled size, and average fill price.

For every result:

1. Calculate cumulative notional as `filled_size * avg_fill_price`.
2. Load the checkpoint for the order identity.
3. Reject cumulative size or notional that moves backward.
4. Compute only the unaccounted size and unaccounted notional.
5. Return a duplicate no-op if the size delta is zero.
6. For BUY, add quantity and update weighted-average entry price.
7. For SELL, require the delta not to exceed local quantity, subtract it, and add `(delta_price - average_entry_price) * delta_size` to realized P&L.
8. Preserve `opened_at` across additional BUY fills, update `last_fill_at`, and set a 30-second remote-confirmation deadline.
9. Reset the exit-attempt count after any confirmed position reduction.
10. Remove a zero-quantity position while retaining its close price and realized P&L in the closed lifecycle record for the dashboard and audit trail.

An impossible SELL fill, regressing cumulative fill, missing identity, or malformed confirmed result raises a typed `PositionAccountingError`. In live mode the router latches the kill switch and does not submit another order.

## Dry-Run Semantics

Dry run must exercise the same position lifecycle as live mode. `OrderSubmitter` returns `SIMULATED` with `filled_size=requested_size` and `avg_fill_price=order.price`. The tracker applies that simulated fill to state. This gives the dashboard real paper positions and allows a complete entry-to-exit cycle without an exchange write.

Offline backtests keep their existing deterministic ledger and synthetic-short behavior. The new live/dry ledger is not imported into `backtest/replay.py`.

## Reconciliation and Data-API Lag

A confirmed CLOB fill is newer evidence than a briefly stale Data API position response. Reconciliation therefore merges rather than blindly replaces positions:

- Keys with no pending confirmation use the Data API as authoritative.
- A key whose remote quantity matches local quantity clears its confirmation deadline and becomes authoritative.
- A mismatch inside the 30-second grace period preserves the local confirmed-fill quantity, records the key as deferred, and keeps reconciliation healthy.
- A mismatch after the grace period fails reconciliation with `position_confirmation_timeout:<market>:<token>` and latches the live kill switch.
- A remote read failure still fails live reconciliation.
- A confirmed SELL to zero matches an absent remote position.

The tracker requests immediate runtime reconciliation after every live confirmed fill. Housekeeping continues the existing 15-second reconciliation cycle until confirmation succeeds or expires. Unknown submission outcomes are never applied, never retried, keep their exit reservation, and immediately halt live trading pending reconciliation/operator review.

## Exit Decisions

`PositionExitPolicy` is a pure class evaluated from one position, its lifecycle, the latest snapshot, and the current UTC time. It uses executable SELL value (`best_bid`) rather than midpoint.

Exit priority is:

1. `market_expiry` when `now >= market_end_at - exit_before_market_end_seconds`;
2. `stop_loss` when return bps is at or below `-stop_loss_bps`;
3. `take_profit` when return bps is at or above `take_profit_bps`;
4. `max_hold` when age reaches `max_hold_seconds`;
5. `strategy_signal` when the spike strategy emits SELL and `exit_on_strategy_sell` is enabled.

The policy never emits an exit for zero quantity, an already reserved exit, missing/stale market data, or a quantity below `execution.min_order_size`. Sub-minimum residual inventory is marked as dust and surfaced on the dashboard; it is not rounded upward and oversold.

## Exit Coordination and Routing

`PositionExitManager` converts policy decisions and spike SELL signals into `POSITION_EXIT` signals. It reserves the position before emitting so concurrent snapshots cannot create duplicate exits. Entry BUY signals continue normally, except a BUY is rejected while that token has a pending exit.

The router uses `signal.requested_size` when present. `reduce_only` is passed into pre-trade risk and requires a SELL with enough current inventory. The order builder uses `signal.time_in_force` when present; managed exits use `IOC`/FAK while entries retain the configured FOK.

Exit sizing starts with the full current quantity. Existing visible-liquidity, maximum order size, and live-notional caps may reduce one attempt. A partial or cap-limited fill releases the reservation only after accounting the confirmed delta, allowing the next eligible attempt to target the remaining quantity. Definite rejection releases the reservation after the two-second retry interval. Three failed attempts without any position reduction latch `exit_attempts_exhausted`. Unknown outcomes retain the reservation and halt immediately.

The exit manager runs before entry strategy routing on every market snapshot and from housekeeping timers. This gives deadline and risk exits priority and allows time-based exits even during a quiet book.

## Market Deadline

For the automatic BTC market, the exit manager reads `Btc15mMarketRotator.status().current_market.end_at` and stores it in the lifecycle when a position is first created or adopted from reconciliation. A live position whose market does not match the discovered current market and has no durable end time fails closed with `position_market_window_unknown`; the bot must not silently carry an unmanaged position across rotation.

Market rotation must not replace the subscription while a non-dust position in the ending market lacks a confirmed exit. At the existing refresh lead, the rotator asks a callback whether rotation is safe. A remaining position causes a degraded `position_exit_pending` status and short retries until the market ends. If the end is reached with a sellable position, runtime risk latches `position_open_at_market_end`.

## Persistence and Recovery

Snapshots add `fill_checkpoints` and `position_lifecycles`, both with empty defaults for backward compatibility. Snapshot writes become atomic temporary-file replacements. The tracker saves a snapshot immediately after every applied fill or exit-reservation change instead of waiting for the periodic 30-second snapshot.

Live startup continues to skip historical position quantities, fetches active positions from the Data API, then merges restored lifecycle/checkpoint metadata onto matching active keys. Closed or resolved historical positions cannot reappear as exposure. Dry run restores positions, lifecycles, and checkpoints so a paper cycle can resume.

## Dashboard and Events

Add event types `POSITION_UPDATED`, `EXIT_TRIGGERED`, `POSITION_CLOSED`, `POSITION_DUST`, and `POSITION_CONFIRMATION_DEFERRED`. Events include typed optional quantity, price, and P&L fields; they never include credentials or signed payloads.

The dashboard position table adds entry price, executable mark, return, held time, market deadline, exit state/reason, and remaining quantity. The control/status area warns when an exit is pending, confirmation is deferred, dust remains, or an exit halt is active. Closed lifecycle records are bounded to the most recent 20 for display and snapshots.

## Safety Invariants

- Only confirmed cumulative fill deltas mutate inventory.
- Replaying the same result is idempotent.
- A SELL cannot exceed known inventory.
- No new BUY is submitted while an exit for that token is pending.
- Unknown outcomes are never retried.
- Exit retries target only confirmed remaining inventory.
- Exchange/Data API disagreement is tolerated only for the configured grace period.
- Live startup never trusts resolved snapshot positions.
- Position and checkpoint changes are durably snapshotted before another exit attempt.
- An open sellable position at market end halts live trading.

## Acceptance Criteria

1. A dry-run BUY creates a paper position immediately; a later take-profit/stop-loss/strategy/deadline condition creates a SELL and reduces or closes it.
2. Replaying a filled or partially filled result does not change quantity twice.
3. A sequence of cumulative partial fills applies only each delta and never oversells.
4. A confirmed live fill remains locally available for an immediate SELL while the Data API is inside its lag grace period.
5. A position mismatch after grace, unknown order outcome, accounting invariant violation, exhausted exit attempts, or open sellable position at market end latches a visible reason.
6. Snapshots recover lifecycle/checkpoint state without restoring resolved live positions.
7. The dashboard exposes exit state without secrets.
8. The full test suite, compile check, diff check, read-only preflight, one full BTC rollover dry run, and a deterministic paper entry/exit scenario pass before live enablement.
