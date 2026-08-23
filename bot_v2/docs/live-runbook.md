# Live Trading Runbook

This runbook defines the staged rollout from offline verification to capped live trading. Live submission stays unreachable until every earlier gate passes.

## Operator Commands

```bash
cd /Users/ghost/Projects/trader/bot_v2
source .venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m backtest.cli --snapshots backtest/example_orderbook_events.json --output /private/tmp/final-backtest.json
python -m app.main
python -m scripts.live_preflight --config-dir config
python -m scripts.healthcheck
```

`python -m app.main` uses `dry_run` until the final operator gate. The preflight command is read-only: it never submits, signs, or cancels orders.

## Non-Negotiable Rollout Gates

Live mode may only be enabled after every one of these gates is verified by the operator:

1. all tests and the five reviewed simulator regressions are green;
2. at least 24 continuous hours of subscribed dry-run data with a fresh heartbeat and no parser/reconnect errors;
3. authenticated read-only preflight green;
4. operator verification of jurisdiction, wallet, funder, pUSD balance, and allowances;
5. one explicitly approved minimal order and cancellation using a separately funded limited-risk wallet;
6. single-market live scope with `max_live_order_notional: 1`;
7. verified alerts, kill switch, and cancel-all procedure;
8. explicit user approval before changing the three live flags.

## Live Flags

Live trading requires all three of these configuration changes, and only after the gates above:

- `bot.mode: live`
- `execution.allow_live_trading: true`
- `execution.dry_run_force: false`

## Kill Switch And Cancel-All

A runtime HALT or operator shutdown cancels all known open orders before stopping further submission. Cancellation is bounded by `shutdown_timeout_seconds`; a timeout is logged as critical and persisted. A cancellation failure never clears the kill switch.

## Safety Properties

- Live remains default-off through `mode`, `allow_live_trading`, and `dry_run_force`.
- A blocked, malformed, or unavailable geoblock response prevents live startup.
- Missing L1/L2 credentials, funder address, balances, allowances, or authenticated account reads prevent live startup.
- Reconciliation failure prevents live startup.
- Every submission has a stable client order ID and is idempotent locally.
- Uncertain submit responses are reconciled before retry; the bot never blindly resubmits.
- Production order size is bounded by the hard live notional cap (`max_live_order_notional`).
- Secrets never appear in logs, snapshots, errors, or committed files.
