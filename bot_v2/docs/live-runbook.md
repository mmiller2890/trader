# Live Trading Runbook

This runbook defines the staged rollout from offline verification to capped live trading. Live submission stays unreachable until every earlier gate passes.

## Operator Commands

```bash
cd /Users/ghost/Projects/trader/bot_v2
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m backtest.cli --snapshots backtest/example_orderbook_events.json --output /private/tmp/final-backtest.json
python -m app.main                 # dry run; refuses an armed live overlay
python -m app.main --live          # explicit CLI live authorization
python -m dashboard.main
python -m scripts.live_preflight --config-dir config
python -m scripts.healthcheck
```

`python -m app.main` refuses an armed live configuration unless the operator also supplies `--live`. Run the preflight command while the safe defaults are still enabled. It constructs a read-only exchange adapter and never submits, signs, or cancels orders; live startup still requires all three live flags after the operator gates pass.

For multi-day unattended operation (72-hour live lease, durable Telegram
alerts, auto-resume gates, guarded `CLEAR HALT <suffix>` recovery), see
[unattended-operations-runbook.md](unattended-operations-runbook.md). The
healthcheck command now defaults to liveness over the runtime health file;
use `python -m scripts.healthcheck --kind trading` for the legacy
market-data freshness checks with exit codes 0/2.

`python -m dashboard.main` opens the loopback-only operator console at `http://127.0.0.1:8000`. It starts/stops dry-run and live runtimes, inspects the active BTC 15-minute market, and shows every read-only preflight check. With automatic market discovery enabled, the bot owns both rotating outcome token IDs and the manual token editor stays locked. Live activation requires a passing preflight from the last five minutes and the exact `ENABLE LIVE` phrase; live start requires `START LIVE` and repeats full preflight in runtime bootstrap. `Return to dry run` atomically restores the safe mode bundle. Emergency halt and cancel-all retain their exact confirmations.

The checked-in dry-run profile discovers market metadata through the public Gamma API before opening the market WebSocket. No private key or CLOB credentials are needed for this path. If discovery cannot validate the current active, open, order-book-enabled market, startup fails closed instead of subscribing to stale IDs.

Do not reuse credentials that have appeared in chat, screenshots, terminal history, or committed files. Revoke and rotate them first, store replacements only in the local ignored `.env`, and verify that the replacement wallet is limited-risk before continuing.

## Non-Negotiable Rollout Gates

Live mode may only be enabled after every one of these gates is verified by the operator:

1. all tests and the five reviewed simulator regressions are green;
2. at least 24 continuous hours of subscribed dry-run data with a fresh heartbeat and no parser/reconnect errors;
3. authenticated read-only preflight green;
4. operator verification of jurisdiction, wallet, funder, pUSD balance, and allowances;
5. one explicitly approved minimal order and cancellation using a separately funded limited-risk wallet;
6. single-market live scope with `min_live_buy_notional: 1` and `max_live_order_notional: 1.01`;
7. verified alerts, kill switch, and cancel-all procedure;
8. explicit user approval before changing the three live flags.

## Live Flags

Live trading requires all three of these configuration changes, and only after the gates above:

- `bot.mode: live`
- `execution.allow_live_trading: true`
- `execution.dry_run_force: false`

Keep the first-order execution controls at their checked-in values:

- `execution.default_order_size: 1`
- `execution.time_in_force: FOK`
- `execution.min_live_buy_notional: 1`
- `execution.max_live_order_notional: 1.01`
- `risk.max_data_staleness_seconds: 1`

The router reduces a live order to the smallest of the configured size, visible top-level liquidity, and the live notional cap. If that amount is below `min_order_size`, it rejects the execution plan. FOK responses are treated as filled only when the exchange immediately reports a complete `matched` amount; ambiguous or partial responses remain unresolved for reconciliation.

## Kill Switch And Cancel-All

A runtime HALT or operator shutdown cancels all known open orders before stopping further submission. Cancellation is bounded by `shutdown_timeout_seconds`; a timeout is logged as critical and persisted. A cancellation failure never clears the kill switch.

## Position Exits And Recovery

Entry orders use `FOK`; managed exits use internal `IOC` mapped to exchange `FAK`. Exit priority is market expiry, stop loss, take profit, max hold, then strategy SELL. Initial thresholds are `take_profit_bps: 300`, `stop_loss_bps: 200`, `max_hold_seconds: 180`, and `exit_before_market_end_seconds: 60`.

Every halt reason below requires returning to dry run and repeating the read-only preflight before live trading resumes:

- `unknown_order_outcome:<client_id>` — a submission outcome is unknown; the exit reservation is retained and the order is never retried.
- `position_accounting_error:<reason>` — a confirmed fill violated an accounting invariant.
- `position_confirmation_timeout:<market>:<token>` — the Data API still disagrees with a confirmed local fill after the 30-second grace period.
- `exit_attempts_exhausted:<market>:<token>` — three exit attempts produced no confirmed reduction.
- `position_open_at_market_end` — a sellable position remained when its market ended.
- `post_fill_reconciliation_failed` — immediate post-fill reconciliation failed.

Partial exits target only confirmed remaining inventory; sub-minimum residual inventory is marked as dust and never oversold. Neither FOK nor FAK guarantees counterparties.

## Safety Properties

- Live remains default-off through `mode`, `allow_live_trading`, and `dry_run_force`.
- A blocked, malformed, or unavailable geoblock response prevents live startup.
- Missing L1/L2 credentials, effective funder address, balances, allowances, or authenticated account reads prevent live startup. Signature type `0` derives the EOA funder from `PRIVATE_KEY`; contract/proxy types require `POLYMARKET_PROXY_ADDRESS`.
- Reconciliation failure prevents live startup.
- Restored orders are matched by exchange ID, terminal fills/cancellations are polled, and live positions are refreshed during housekeeping.
- Every submission has a stable client order ID and is idempotent locally.
- Uncertain submit responses are reconciled before retry; the bot never blindly resubmits.
- Production order size is bounded by the hard live notional cap (`max_live_order_notional`).
- First live orders use FOK, so they either receive a confirmed complete match or are treated as unfilled/unresolved; this improves execution certainty but cannot create market liquidity.
- Secrets never appear in logs, snapshots, errors, or committed files.
