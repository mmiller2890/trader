# Polymarket BTC 15-Minute Trading Bot

A dry-run-first Python trading bot for Polymarket's recurring Bitcoin Up/Down 15-minute markets. It includes automatic market discovery, live order routing behind explicit safety gates, position lifecycle and exit management, reconciliation, snapshots, backtesting, and a local operator dashboard.

> [!WARNING]
> This is experimental trading software, not financial advice. Live orders can lose money, fail, partially fill, or remain exposed during exchange, network, or process failures. Start in dry-run mode, use a dedicated low-balance account, and independently verify exchange positions and orders.

## Current status

Implemented today:

- automatic discovery and rotation of active BTC 15-minute markets;
- WebSocket order-book ingestion with reconnect backoff;
- spike-strategy signals with liquidity, exposure, slippage, and duplicate guards;
- simulated dry-run execution and guarded Polymarket CLOB V2 live execution;
- idempotent confirmed-fill accounting and exchange reconciliation;
- managed take-profit, stop-loss, maximum-hold, strategy, and pre-expiry exits;
- persisted positions, fill checkpoints, realized P&L, lifecycle history, and kill-switch state;
- a loopback-only dashboard for configuration, preflight, start, stop, halt, and cancel-all controls;
- deterministic historical backtesting.

Still planned, not yet implemented:

- supervised recovery from every critical background-task failure;
- safe automatic live resumption after a process restart;
- durable Telegram alert delivery and bounded notification retries;
- bounded long-term memory/journal retention and deployment health checks;
- formal 24-hour and 72-hour unattended qualification.

See the [multi-day unattended operations design](backtest/docs/superpowers/specs/2026-08-24-multi-day-unattended-operations-design.md) and [implementation plan](backtest/docs/superpowers/plans/2026-08-24-multi-day-unattended-operations.md) for the remaining reliability work. Until that plan is complete and qualified, treat the bot as supervised software.

## How it works

```text
Gamma market discovery
        │
        ▼
Polymarket market WebSocket ──► normalized order book and snapshots
        │                                      │
        │                                      ▼
        │                               spike strategy
        │                                      │
        │                                      ▼
        │                              pre-trade risk checks
        │                                      │
        ▼                                      ▼
position exit policy ─────────────────► execution router
                                               │
                              ┌────────────────┴───────────────┐
                              ▼                                ▼
                         dry-run fill                    live CLOB order
                              │                                │
                              └──────────────┬─────────────────┘
                                             ▼
                                  accounting + reconciliation
                                             │
                                             ▼
                                snapshots, journal, dashboard
```

All order intents go through the same risk and accounting boundaries. Live mode adds credentials and exchange submission; it does not bypass risk checks.

## Requirements

- Python 3.11 or newer
- macOS or Linux
- Internet access for current-market discovery and live market data
- Polymarket wallet/CLOB credentials only for live trading

## Quick start

From the `bot_v2` directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
cp .env.example .env
python3 -m pytest -q -p no:cacheprovider
python3 -m dashboard.main
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The dashboard binds only to loopback by design.

The checked-in configuration starts in `dry_run`. No wallet or CLOB credentials are required for public market discovery and simulated execution.

## Run without the dashboard

Start the bot in the configured mode:

```bash
python3 -m app.main
```

Press `Ctrl+C` for a graceful shutdown. An armed live configuration is refused unless the process also receives the explicit live flag:

```bash
python3 -m app.main --live
```

Do not use `--live` until every item in the live checklist below passes.

## Operator dashboard

Start it with:

```bash
python3 -m dashboard.main
```

Optional arguments:

```bash
python3 -m dashboard.main \
  --host 127.0.0.1 \
  --port 8000 \
  --config-dir config
```

The dashboard provides:

- runtime, market rotation, WebSocket, credential, and readiness status;
- open orders, balances, positions, managed exits, closed positions, and P&L;
- recent journal events and safety warnings;
- editable operator-safe settings;
- read-only preflight checks;
- explicit Start, Stop, Emergency Halt, and Cancel All controls.

Destructive or live actions require exact confirmation phrases. The API also requires a per-process operator token and a trusted loopback browser origin.

## Configuration

Configuration is validated with Pydantic and assembled from:

| File | Purpose |
|---|---|
| `config/bot.yaml` | Runtime, market data, exchange, execution, position management, and notifications |
| `config/risk.yaml` | Exposure, loss, liquidity, staleness, and circuit-breaker limits |
| `config/strategies/spike.yaml` | Spike strategy behavior |
| `config/operator.yaml` | Dashboard-managed mode and token scope; ignored by Git |
| `.env` | Credentials and secrets; ignored by Git |

The safe mode bundle is:

```yaml
bot:
  mode: dry_run
execution:
  allow_live_trading: false
  dry_run_force: true
```

`config/operator.example.yaml` contains a safe operator overlay example.

## Live trading checklist

Live mode requires all three guards:

```yaml
bot:
  mode: live
execution:
  allow_live_trading: true
  dry_run_force: false
```

It also requires environment-backed credentials. Copy `.env.example` to `.env` and populate only your local file:

| Variable | Purpose |
|---|---|
| `PRIVATE_KEY` | Wallet signing key |
| `POLYMARKET_PROXY_ADDRESS` | Effective funded account for proxy/contract signature types |
| `CLOB_API_KEY` | Polymarket CLOB API key |
| `CLOB_SECRET` | Polymarket CLOB API secret |
| `CLOB_PASSPHRASE` | Polymarket CLOB API passphrase |
| `RPC_URL` | Polygon RPC endpoint when required |
| `TELEGRAM_BOT_TOKEN` | Optional Telegram notifier token |
| `TELEGRAM_CHAT_ID` | Optional Telegram destination |

Before enabling live mode:

1. Keep the safe dry-run flags enabled.
2. Use a dedicated account with only the amount you are prepared to risk.
3. Verify the configured signature type and effective funder address.
4. Verify USDC balance and exchange allowance.
5. Run the read-only preflight:

   ```bash
   python3 -m scripts.live_preflight --config-dir config
   ```

6. Require every preflight check to pass. Exit code `0` means pass; `2` means blocked.
7. Use the dashboard to enable live mode with the exact confirmation.
8. Start live trading with the exact `START LIVE` confirmation.
9. Independently confirm the first order, fill, and resulting position on Polymarket.

Runtime bootstrap repeats the full preflight and startup reconciliation before it permits live trading.

> [!IMPORTANT]
> Credentials previously pasted into chat, logs, screenshots, issues, or source code should be treated as compromised. Revoke and rotate them before funding or running the account.

For detailed operating procedures, see the [live runbook](docs/live-runbook.md).

## Automatic BTC market rotation

With `market_data.automatic_market.enabled: true`, the bot:

1. discovers the active `btc-updown-15m` market through the public Gamma API;
2. validates that the market is active, open, and order-book enabled;
3. subscribes to both outcome-token books;
4. starts exit handling before the current window ends;
5. rotates the WebSocket subscription to the next market only when the ending market is safe to leave.

If sellable inventory remains at market end, rotation fails closed and activates the kill switch instead of silently abandoning the position.

## Position lifecycle and selling

A confirmed BUY creates or extends a managed position. The exit manager can generate reduce-only SELL orders for:

- strategy sell signals;
- take-profit thresholds;
- stop-loss thresholds;
- maximum holding time;
- approaching market expiration.

Exit reservations prevent duplicate concurrent sells. Failed exits retry according to configuration, and exhausting the exit-attempt budget activates the persisted kill switch. Confirmed partial and full SELL fills reduce inventory and update realized P&L idempotently.

No software can guarantee a fill. IOC/FOK behavior, price, available liquidity, order size, balance, allowance, exchange validation, and network conditions all affect execution.

## Safety model

Important protections include:

- dry-run mode by default;
- explicit live-arm flags plus an explicit process/dashboard confirmation;
- geoblock, credential, balance, allowance, market, and authenticated-account preflight checks;
- maximum order, position, exposure, open-order, daily-loss, liquidity, slippage, and data-age limits;
- duplicate-signal and pending-exit entry guards;
- startup and periodic exchange reconciliation;
- cumulative fill checkpoints that prevent double accounting;
- persisted exit reservations and kill-switch state;
- cancel-all on runtime safety halt and graceful live shutdown;
- sanitized external error reporting to reduce secret leakage.

Safety failures intentionally require investigation. Do not clear or work around the kill switch until the exchange orders, positions, balances, allowance, and local snapshot agree.

## Testing

Run the complete suite:

```bash
python3 -m pytest -q -p no:cacheprovider
```

Compile every package without writing bytecode into the repository:

```bash
PYTHONPYCACHEPREFIX=/tmp/polymarket-bot-pyc \
python3 -m compileall -q \
  app backtest clients config dashboard execution models notifications \
  persistence portfolio risk scripts state strategies tests
```

Useful focused suites:

```bash
python3 -m pytest -q tests/test_live_preflight.py
python3 -m pytest -q tests/test_reconciliation.py tests/test_position_accounting.py
python3 -m pytest -q tests/test_exit_manager.py tests/test_rotation_gate.py
python3 -m pytest -q tests/test_dashboard_controller.py tests/test_dashboard_api.py
```

## Backtesting

Run the included example:

```bash
python3 -m backtest.cli \
  --snapshots backtest/example_orderbook_events.json \
  --output backtest/results/example-backtest.json
```

The backtester supports normalized order-book events and legacy `MarketSnapshot` arrays. It models fees, liquidity consumption, sequence gaps, position/equity changes, and configured risk behavior. Backtest results are simulations, not evidence of future performance or live fill quality.

## Runtime data

Runtime state defaults to `data/` and can be moved with `BOT_DATA_DIR`:

```text
data/
├── bot.sqlite3
├── journal/events.jsonl
└── snapshots/state.json
```

These files may contain order, position, balance, and operational metadata. They are ignored by Git and should be backed up and protected like account records.

## Project layout

```text
bot_v2/
├── app/              # Bootstrap, runtime lifecycle, and shutdown
├── backtest/         # Historical replay, paper exchange, metrics, and plans/specs
├── clients/          # Gamma, WebSocket, CLOB, Data API, geoblock, and book adapters
├── config/           # Typed configuration and safe checked-in defaults
├── dashboard/        # Local FastAPI operator console
├── execution/        # Risk routing, order building, submission, and tracking
├── models/           # Typed domain models
├── notifications/    # Event bus and Telegram boundary
├── persistence/      # Journal, SQLite metadata, and snapshots
├── portfolio/        # Exposure, sizing, and position exits
├── risk/             # Pre-trade and runtime risk policies
├── scripts/          # Health and live-preflight commands
├── state/            # In-memory state and exchange reconciliation
├── strategies/       # Trading strategy implementations
└── tests/            # Runtime, exchange-boundary, dashboard, and safety tests
```

## Documentation

- [Live trading runbook](docs/live-runbook.md)
- [Dashboard design](backtest/docs/superpowers/specs/2026-08-24-dashboard-live-control-design.md)
- [Position lifecycle and exit design](backtest/docs/superpowers/specs/2026-08-24-position-lifecycle-exit-management-design.md)
- [Lifecycle safety fixes plan](backtest/docs/superpowers/plans/2026-08-24-position-lifecycle-safety-fixes.md)
- [Multi-day unattended operations design](backtest/docs/superpowers/specs/2026-08-24-multi-day-unattended-operations-design.md)
- [Multi-day unattended operations implementation plan](backtest/docs/superpowers/plans/2026-08-24-multi-day-unattended-operations.md)

## Troubleshooting

### The dashboard says a fresh preflight is required

Preflight results expire after five minutes and are invalidated by configuration changes or failed live startup. Return to safe dry-run flags, run preflight again, resolve every failed check, then re-enable live mode.

### The bot is running but does not trade

Check:

- strategy target scope and automatic-market health;
- whether the market snapshot is fresh;
- top-of-book liquidity and configured minimums;
- cooldown and duplicate-signal windows;
- position, exposure, open-order, and daily-loss limits;
- pending exit reservations and the kill switch;
- journaled risk-decision reasons.

A quiet bot can be correct behavior: risk rejection is preferable to forcing a low-quality trade.

### An order receives HTTP 400

Inspect the sanitized journal reason and verify token ID, side, price precision, tick size, minimum order/notional, time in force, balance, allowance, signature type, funder address, and whether the market is still accepting orders. Do not retry malformed orders blindly.

### WebSocket data stops

The manager reconnects with capped backoff. If transport or market-data heartbeats remain stale, runtime risk activates the kill switch. Verify network access and the active market before restarting.

### Live trading halted

Treat the persisted halt reason as an intervention request. Check Polymarket orders and positions first, preserve `data/snapshots/state.json` and `data/journal/events.jsonl`, then follow the live runbook. Restarting the process does not intentionally erase the persisted kill switch.

## Deployment supervision

For multi-day unattended operation, run the operator dashboard under a
process supervisor. Reference files:

- `Dockerfile` — liveness `HEALTHCHECK` over the atomic runtime health file.
- `docker-compose.example.yml` — `restart: unless-stopped`, persistent
  `/data` volume, env-file injection, log-size limits.
- `deploy/polymarket-bot.service` — systemd unit with `Restart=on-failure`,
  `RestartSec=5`, burst limits, dedicated environment file, non-root user,
  and a `TimeoutStopSec` longer than bot shutdown so final cancel/snapshot
  work completes.

Both entry points run `python -m dashboard.main`: startup performs the full
lease-based auto-resume gate (fresh preflight + reconciliation). The
command line never grants fresh live authorization; `--live` remains an
explicit interactive choice and `--resume-live` only validates a persisted
lease. A fatal supervised failure exits nonzero so the supervisor restarts;
a safe `HALTED` state keeps the dashboard alive for operator inspection and
must not enter a restart loop. Copy `deploy/polymarket-bot.env.example`,
fill it locally, and never commit it. See
`docs/unattended-operations-runbook.md` for per-alert playbooks.

## Security

- Never commit `.env`, `config/operator.yaml`, `data/`, private keys, CLOB credentials, bot tokens, or funded account details.
- Keep the dashboard bound to loopback; use SSH tunneling rather than exposing it directly.
- Use a dedicated low-balance wallet and the smallest practical live order cap.
- Rotate any credential that may have been exposed.
- Review staged changes before every push:

  ```bash
  git diff --cached --check
  git status --short
  ```

## Development workflow

Changes to live execution, reconciliation, accounting, exits, runtime controls, or persistence should include regression tests that fail before the fix and pass afterward. Keep exchange SDK interaction inside client adapters, preserve typed boundaries, and prefer fail-closed behavior when exchange truth is unavailable.
