# Live Trading Readiness Design

**Status:** Approved from the 2026-08-23 review and live-readiness discussion.

## Goal

Make the bot safe and technically capable of trading against Polymarket's production CLOB V2 while retaining a default-off live path, a useful dry-run/shadow path, and a trustworthy offline simulator.

## Delivery Order

1. Repair the five confirmed backtest correctness defects.
2. Protect secrets and migrate the exchange dependency from the retired V1 SDK to CLOB V2.
3. Replace method guessing with a typed, testable V2 adapter.
4. Implement real market discovery, WebSocket subscription, book reconstruction, and heartbeats.
5. Add compliance and authenticated read-only preflight checks.
6. Complete order, fill, cancel, balance, and reconciliation behavior.
7. Add operational circuit breakers and a staged shadow-to-live runbook.

Live submission must remain unreachable until every earlier phase is verified.

## Confirmed Backtest Defects

1. `PortfolioLedger.can_apply` calculates reserve from only the position being changed and ignores shorts in other markets.
2. `OrderBookState.quote` reports all book depth as executable liquidity, including levels beyond the execution price limit.
3. `OrderBookState.commit` mutates earlier levels before validating later fills, so a failed commit can partially consume the book.
4. `BacktestEngine` sends the best quote rather than execution VWAP to the risk slippage check.
5. Legacy snapshots receive sequence IDs before the engine's final `(received_ts, source_ts, sequence_id)` ordering, allowing equal-receive-time inputs to reorder into an invalid sequence.

Each defect requires a regression test reproducing the exact reviewed failure.

## Runtime Architecture

The application keeps the existing strategy → risk → order-builder → submitter flow. External exchange behavior is isolated behind a typed `ClobClientAdapter`; production code calls explicit CLOB V2 methods, while unit tests inject a fake SDK client. Current-position reads come from a separate typed `DataApiClient`, because positions are a Data API resource rather than a CLOB SDK method. No SDK method-name guessing remains.

Market discovery yields explicit condition and outcome token IDs. On connection, the WebSocket sends a market-channel subscription for those token IDs, sends `PING` every ten seconds, and rebuilds books from full snapshots plus price changes. Strategies receive snapshots only when both sides exist.

Startup has three useful states:

- `backtest`: no network or live imports;
- `dry_run`: production market data and full decision pipeline, but simulated submission;
- `live`: authenticated V2 client, successful compliance/preflight/reconciliation, then real submission.

## Live Safety Properties

- Live remains default-off through `mode`, `allow_live_trading`, and `dry_run_force` guards.
- A geographic restriction response that is blocked, malformed, or unavailable prevents live startup.
- Missing L1/L2 credentials, funder address, balances, allowances, or authenticated account reads prevent live startup.
- Reconciliation failure prevents live startup.
- A kill switch cancels all known open orders before stopping further submission.
- Every submission has a stable client order ID and is idempotent locally.
- Uncertain submit responses are reconciled before retry; the bot never blindly resubmits.
- Production order size is bounded by a separate hard live notional cap.
- Secrets never appear in logs, snapshots, errors, or committed files.

## SDK and Protocol Baseline

- Pin `py-clob-client-v2==1.1.0` and remove legacy `py-clob-client`.
- Production host: `https://clob.polymarket.com`.
- Production positions host: `https://data-api.polymarket.com` using `GET /positions?user={funder}` with pagination.
- Polygon chain ID: `137`.
- Use the official L1 private-key and L2 API-credential flow.
- Use configured signature type and funder/deposit-wallet address rather than guessing wallet type.
- Subscribe to the public market WebSocket with `{"assets_ids": [...], "type": "market"}`.
- Support full `book`, `price_change`, `tick_size_change`, and `market_resolved` events needed for safe trading decisions.
- Treat pUSD as production collateral and verify available balance/allowance through the V2 SDK/API.

Primary references:

- https://docs.polymarket.com/v2-migration
- https://docs.polymarket.com/trading/overview
- https://docs.polymarket.com/trading/manage-orders
- https://docs.polymarket.com/api-reference/wss/market
- https://docs.polymarket.com/api-reference/geoblock
- https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user
- https://github.com/Polymarket/py-clob-client-v2

## Secrets and Configuration

Create `.gitignore` before `.env`. Provide `.env.example` containing names only. Retain the repository's current environment names for compatibility:

- `PRIVATE_KEY`
- `POLYMARKET_PROXY_ADDRESS`
- `CLOB_API_KEY`
- `CLOB_SECRET`
- `CLOB_PASSPHRASE`
- `RPC_URL`

Add non-secret exchange settings to YAML: CLOB host, Data API host, chain ID, signature type, geoblock URL, heartbeat interval, and hard live notional cap.

## Verification and Rollout

Unit tests use fake exchange/HTTP/WebSocket implementations and must not reach the network. A separately invoked preflight command performs real read-only checks. Rollout proceeds through offline tests, dry-run market ingestion, authenticated read-only preflight, extended shadow operation, one minimal production order plus cancellation, then tightly capped single-market operation.

No task in this design authorizes depositing funds, changing token allowances, submitting a production order, or enabling live mode. Those are explicit operator actions after the implementation and verification gates pass.
