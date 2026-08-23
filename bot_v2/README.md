# Polymarket Bot v2

Production-minded, dry-run-first Polymarket bot built in Python 3.11+ with typed models, async runtime boundaries, structured JSON logging, and a conservative safety posture.

This `v2` folder is intentionally separate from the first bot. It keeps the same safe-first implementation goals, but the project description and structure are also documented here as an explicit architecture design, inspired by common patterns seen across open-source Polymarket bots, copy-trading bots, market-making bots, arbitrage bots, Hummingbot, and Freqtrade.

## Start Here

This bot starts in **safe `dry_run` mode** by default.

- It does **not** place real trades by default
- Every order intent must pass through risk before execution
- Live trading is scaffolded behind a guard
- You can run the whole app locally without enabling live trading

If you do not care about the architecture yet and just want to run it safely, go straight to:

- `## Fastest Setup`
- `## Run The Bot`
- `## Common Problems`

## Fastest Setup

If you are brand new to this, copy and paste these commands **one at a time** into Terminal after you open the `bot_v2` folder in Terminal:

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
cp .env.example .env
python3 -m pytest
python3 -m app.main
```

What these commands do:

- check that Python is installed
- create a private Python environment for this project
- activate that environment
- install the project and test tools
- create your local `.env` file
- run the tests
- start the bot in safe `dry_run` mode

If any command fails, go to `## Common Problems`.

## What This v2 Tries To Do

The goal is a simple but production-minded Polymarket bot architecture:

- typed configuration
- typed domain models
- event-driven market-data pipeline
- deterministic strategy logic
- mandatory pre-trade risk checks
- dry-run execution pipeline
- structured journaling and snapshots
- clear module boundaries so live mode can be enabled later without a rewrite

## Architecture Ideas Used

This project is inspired by patterns commonly found in:

- Polymarket spike and market-making bots: real-time monitoring, small focused strategies, liquidity filters, slippage controls, Telegram alerts
- Polymarket copy-trading bots: clear config for target accounts, retry limits, and persistence of trade state
- Arbitrage bots: explicit separation of opportunity detection, decision logic, and execution
- Hummingbot: connector boundary, strategy base classes, config-driven behavior
- Freqtrade: one codebase with separate runtime modes like live, backtest, replay, and dry-run

The key design choice is to **borrow patterns, not blindly copy code**.

## Final Folder Tree

```text
bot_v2/
├── app/
│   ├── main.py
│   ├── bootstrap.py
│   ├── modes.py
│   └── shutdown.py
├── config/
│   ├── loader.py
│   ├── schema.py
│   ├── bot.yaml
│   ├── risk.yaml
│   └── strategies/
│       └── spike.yaml
├── clients/
│   ├── clob_client.py
│   ├── ws_client.py
│   ├── auth.py
│   ├── market_data_client.py
│   └── rate_limiter.py
├── models/
│   ├── market.py
│   ├── signal.py
│   ├── order.py
│   ├── position.py
│   ├── risk.py
│   └── events.py
├── state/
│   ├── store.py
│   ├── reconciliation.py
│   └── cache.py
├── strategies/
│   ├── base.py
│   └── spike.py
├── execution/
│   ├── order_builder.py
│   ├── submitter.py
│   ├── tracker.py
│   └── router.py
├── risk/
│   ├── pretrade.py
│   ├── runtime.py
│   ├── policy.py
│   └── circuit_breaker.py
├── portfolio/
│   ├── exposure.py
│   ├── pnl.py
│   └── sizing.py
├── persistence/
│   ├── journal.py
│   ├── snapshots.py
│   └── db.py
├── notifications/
│   ├── events.py
│   └── telegram.py
├── backtest/
│   ├── replay.py
│   └── metrics.py
├── scripts/
│   └── healthcheck.py
├── tests/
│   ├── test_config.py
│   ├── test_risk_pretrade.py
│   ├── test_spike_strategy.py
│   ├── test_order_builder.py
│   └── test_state_store.py
├── .env.example
├── pyproject.toml
├── README.md
└── Dockerfile
```

## Module Responsibilities

### `config/`

Responsibilities:

- load config from YAML and environment
- validate ranges, enums, and safety guards
- keep secrets separate from strategy and risk tuning

Key files:

- `config/schema.py`: typed Pydantic config models
- `config/loader.py`: merge YAML fragments + env secrets
- `config/bot.yaml`: runtime config
- `config/risk.yaml`: risk limits
- `config/strategies/spike.yaml`: strategy parameters

How it interacts:

- loaded first by `app/bootstrap.py`
- passed into clients, strategy, risk, execution, notifications

### `clients/`

Responsibilities:

- isolate Polymarket-specific client logic
- isolate WebSocket transport and reconnect logic
- normalize raw exchange payloads into typed internal models

Key files:

- `clients/clob_client.py`: adapter boundary around `py-clob-client`
- `clients/ws_client.py`: resilient async WS manager
- `clients/market_data_client.py`: raw transport -> typed market models
- `clients/auth.py`: typed credentials extraction
- `clients/rate_limiter.py`: simple async limiter

How it interacts:

- market data flows from `ws_client.py` -> `market_data_client.py` -> `state/` and `strategies/`
- execution live path will call `clob_client.py`

### `models/`

Responsibilities:

- define all internal typed objects so modules do not pass raw dicts

Key files:

- `market.py`: orderbook and market snapshot types
- `signal.py`: strategy outputs
- `order.py`: order request/result types
- `position.py`: positions and balances
- `risk.py`: risk decisions and check results
- `events.py`: internal event payloads

How it interacts:

- every other module depends on these types

### `state/`

Responsibilities:

- keep the latest runtime truth in memory
- expose async-safe access for strategies, risk, execution, and housekeeping
- provide startup reconciliation and historical cache helpers

Key files:

- `store.py`: in-memory state store
- `reconciliation.py`: startup reconciliation boundary
- `cache.py`: recent snapshot history for strategies

How it interacts:

- market data updates state
- strategy reads state history
- risk checks state
- execution updates orders and heartbeats

### `strategies/`

Responsibilities:

- convert market conditions into typed signals
- never place orders directly

Key files:

- `base.py`: strategy interface
- `spike.py`: deterministic spike strategy

How it interacts:

- consumes typed market snapshots
- emits `TradeSignal`
- `execution/router.py` handles the rest

### `risk/`

Responsibilities:

- enforce all pre-trade and runtime safety checks
- act as mandatory gate before execution

Key files:

- `policy.py`: shared risk interfaces
- `pretrade.py`: order-intent risk checks
- `runtime.py`: periodic runtime safety checks
- `circuit_breaker.py`: repeated-failure guard

How it interacts:

- every signal/order intent must pass through pre-trade risk
- housekeeping loop runs runtime risk checks continuously

### `execution/`

Responsibilities:

- convert approved signals into concrete orders
- submit in dry-run or live mode
- track lifecycle updates

Key files:

- `order_builder.py`: deterministic order creation
- `submitter.py`: dry-run simulation or live submission
- `tracker.py`: state updates from order results
- `router.py`: glue between signal, risk, execution, and journaling

How it interacts:

- input: `TradeSignal`
- output: `OrderRequest`, `OrderResult`, state updates, events, journal entries

### `portfolio/`

Responsibilities:

- small helpers for sizing, exposure, and PnL

Key files:

- `sizing.py`
- `exposure.py`
- `pnl.py`

### `persistence/`

Responsibilities:

- record enough runtime state to recover and inspect behavior after crashes

Key files:

- `journal.py`: append-only JSONL event log
- `snapshots.py`: save/load current state snapshots
- `db.py`: minimal SQLite adapter

### `notifications/`

Responsibilities:

- internal event pub/sub
- operator notifications like Telegram

Key files:

- `events.py`
- `telegram.py`

### `backtest/`

Responsibilities:

- preserve module boundaries for replay/backtest mode

Key files:

- `replay.py`
- `metrics.py`

### `app/`

Responsibilities:

- bootstrap everything
- run reconciliation
- start loops
- manage shutdown

Key files:

- `bootstrap.py`
- `main.py`
- `modes.py`
- `shutdown.py`

## Runtime Loop Design

### Option 1: Snapshot + Poll

Pros:

- easy to reason about
- simple for low-frequency bots

Cons:

- slower reaction time
- more redundant work
- easier to miss short-lived spikes

### Option 2: WebSocket Streaming + Event-Driven Signals

Pros:

- closer to how Polymarket-specific spike and market-making bots are usually structured
- lower latency
- strategies only run when useful market data arrives
- fits asyncio naturally

Cons:

- requires reconnect logic and stale-data protection

### Option 3: Fixed-Interval Loop Only

Pros:

- simple housekeeping pattern

Cons:

- poor fit for short-lived orderbook moves
- wastes cycles when nothing changes

### State Machine vs Simple Loop

Explicit state machine:

- useful later for more complex strategies and fill handling
- helpful for copy-trading, ladder orders, or market-making workflows

Simpler event-driven loop:

- better for v1
- less code
- easier to validate safely

### Recommended Design For This Bot

Use:

- **WebSocket-first market data**
- **event-driven strategy evaluation on market updates**
- **a small housekeeping loop for runtime risk and snapshots**
- **simple execution flow instead of a large explicit state machine**

That is what this project implements.

Why:

- it captures the real-time feel of Polymarket spike/market-making bots
- it keeps execution and safety paths explicit like good arbitrage bots
- it still preserves clear future extension points for more advanced order-state machines later

## Risk And Safety Layer

The risk layer lives in `risk/` and sits **between strategy and execution**.

Every order intent must pass through `risk/pretrade.py`.

Implemented checks:

- dry-run/live mode guard
- kill switch
- stale market data
- max single position size
- max total exposure
- max open orders
- duplicate signal/order guard
- min top-of-book liquidity
- slippage threshold

Runtime checks:

- stale heartbeat
- daily loss boundary
- repeated failures via circuit breaker

Circuit breaker behavior:

- tracks failures within a rolling window
- trips after configured threshold
- blocks further action until cooldown expires

## Config And UX

### `.env`

Use `.env` for secrets and environment-specific values:

- `PRIVATE_KEY`
- `POLYMARKET_PROXY_ADDRESS`
- `CLOB_API_KEY`
- `CLOB_SECRET`
- `CLOB_PASSPHRASE`
- `RPC_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### YAML

Use YAML for tunable behavior:

- bot mode
- intervals
- risk limits
- strategy thresholds
- notification behavior

Example keys:

- `bot.mode`
- `market_data.reconnect_initial_seconds`
- `market_data.heartbeat_timeout_seconds`
- `execution.dry_run_force`
- `execution.allow_live_trading`
- `risk.max_open_orders`
- `risk.max_slippage_bps`
- `spike_strategy.spike_threshold_bps`
- `spike_strategy.cooldown_seconds`

Loading flow:

1. load `config/bot.yaml`
2. overlay `config/risk.yaml`
3. overlay `config/strategies/spike.yaml`
4. overlay env secrets from `.env` / process env
5. validate into typed `AppConfig`

## Logging And Monitoring

This project uses structured JSON logging.

Each important log/event should include where relevant:

- `component`
- `event_type`
- `market_id`
- `token_id`
- `strategy_name`
- `signal_id`
- `client_order_id`
- `mode`
- `reason`
- `latency_ms`

Persistence for reconstruction:

- append-only event journal in `data/journal/events.jsonl`
- runtime snapshot in `data/snapshots/state.json`
- minimal SQLite metadata store in `data/bot.sqlite3`

Telegram notifier:

- safe no-op if not configured
- can send `bot_started`, `kill_switch_tripped`, `repeated_failures`, and large simulated-order alerts

## Deployment And Secrets

### Minimal Deployment

Good v1 deployment target:

- one Linux VM
- Docker
- one process running the bot
- process supervision or container restart policy
- log collection from stdout

### Docker

This repo includes a basic `Dockerfile`.

### systemd

Not implemented here, but a reasonable production pattern would be:

- run from a dedicated Linux user
- inject env vars from an env file
- restart on failure
- send stdout/stderr to journald

### Secrets

For v1:

- `.env` is acceptable

For stronger production hygiene later:

- VM secret store
- Docker secrets
- cloud secret manager

## Data Flow

Incoming market data flows like this:

1. `clients/ws_client.py` receives raw messages
2. `clients/market_data_client.py` normalizes them into typed market models
3. `state/store.py` stores latest market snapshot/orderbook
4. `strategies/spike.py` evaluates the new snapshot
5. strategy emits a `TradeSignal`
6. `execution/router.py` records the signal and asks `risk/pretrade.py` for a decision
7. if risk approves, `execution/order_builder.py` creates an `OrderRequest`
8. `execution/submitter.py` simulates or submits
9. `execution/tracker.py` updates state
10. `persistence/journal.py` and `notifications/events.py` record and publish what happened

## Prioritized Implementation Plan

If you wanted the safest possible path to a usable bot, the order should be:

### First

- typed config
- typed models
- state store
- structured logging

### Second

- websocket boundary
- market-data normalization
- one deterministic strategy

### Third

- pre-trade risk engine
- dry-run execution path
- journaling and snapshots

### Fourth

- app bootstrap
- graceful shutdown
- runtime risk
- tests

### Fifth

- reconciliation hardening
- live-mode adapter validation with real SDK behavior
- deeper order-state handling
- additional strategies like copy-trading, laddering, or end-cycle behavior

## First-Time Setup

### 1. Install Python

Install Python 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/).

Check it:

```bash
python3 --version
```

### 2. Open Terminal and go into this folder

```bash
cd /full/path/to/bot_v2
```

### 3. Create a virtual environment

```bash
python3 -m venv .venv
```

### 4. Activate it

```bash
source .venv/bin/activate
```

If this worked, you will usually see `(.venv)` at the beginning of the line in Terminal.

### 5. Install dependencies

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
```

This may take a minute or two the first time.

### 6. Create `.env`

```bash
cp .env.example .env
```

For your first dry-run test, you can leave `.env` alone.

### 7. Confirm safe mode

In `config/bot.yaml`, make sure:

```yaml
mode: dry_run
```

And under `execution`:

```yaml
dry_run_force: true
allow_live_trading: false
```

## Run The Bot

```bash
python3 -m app.main
```

## Run A Backtest

The backtest is fully offline: it never starts the WebSocket, instantiates a CLOB client, or calls the exchange. It replays normalized historical book events through the configured spike strategy, existing pre-trade risk checks, and a deterministic paper exchange that reconstructs full books, consumes depth, produces partial fills, charges fees, and tracks cash, collateral, positions, and equity.

```bash
python3 -m backtest.cli \
  --snapshots backtest/example_orderbook_events.json \
  --output backtest/results/realistic-backtest.json
```

For a legacy smoke test, use the included [`backtest/example_snapshots.json`](backtest/example_snapshots.json) file as `--snapshots`; it is automatically converted to one-level book events.

### Input formats

The CLI accepts either a JSON array of legacy `MarketSnapshot` objects (no `event_type` field) or a JSON array of richer `book_snapshot` and `book_delta` events. Legacy objects become one-level full-book snapshots with deterministic sequence IDs. See [`backtest/example_orderbook_events.json`](backtest/example_orderbook_events.json) for the event format:

```json
[
  {
    "event_type": "book_snapshot",
    "market_id": "market-1",
    "token_id": "token-1",
    "bids": [{"price": "0.49", "size": "100"}],
    "asks": [{"price": "0.51", "size": "100"}],
    "sequence_id": 100,
    "source_ts": "2025-01-01T00:00:00+00:00",
    "received_ts": "2025-01-01T00:00:00+00:00"
  }
]
```

A full snapshot replaces the book for that `(market_id, token_id)`. A delta upserts non-zero levels and deletes zero-size levels. Events are processed deterministically in `(received_ts, source_ts, sequence_id)` order. Out-of-order sequence numbers and crossed books are rejected; sequence gaps are rejected by default and only full snapshots may resynchronize the book.

### Paper-exchange behavior

- Approved buys consume asks from lowest price upward; approved sells consume bids from highest price downward.
- The execution limit derives from the current best quote plus `execution.max_slippage_bps`.
- `FOK` orders fill completely or leave the book and portfolio untouched. `IOC` and `GTC` may partially fill; this version does not model a resting maker queue, so any remainder is reported unfilled.
- Taker fees are `notional * taker_fee_bps / 10000` and reduce cash and net P&L. Position `realized_pnl` and `unrealized_pnl` remain gross of fees.
- Signed positions are preserved: a negative position is a synthetic short and reserves `max_payout_per_share` USDC per short share (maximum prediction-market payout liability). Available cash is `cash - reserved_cash`, and a fill is rejected when projected cash falls below projected reserves.
- After every event, `equity == cash + sum(position.quantity * last_mark_price)` and `net_pnl == equity - starting_cash`.

### Backtest YAML settings

Under `backtest` in `bot.yaml`:

```yaml
backtest:
  starting_cash: 1000          # initial USDC balance
  taker_fee_bps: 10            # taker fee in basis points
  allow_short_positions: true  # permit synthetic shorts with collateral
  reject_sequence_gaps: true   # require contiguous delta sequence ids
  max_payout_per_share: 1      # collateral reserved per short share
```

### Output

The output file retains `signals`, `order_results`, `positions`, `equity_curve`, and `metrics`, and adds `execution_reports` (individual fills, VWAP, fees, filled size, unfilled remainder, and executable liquidity inside the limit) and `portfolio_snapshots` (cash, reserved/available cash, position value, equity, realized/unrealized/gross/net P&L, fees, and positions per event). Metrics add `starting_cash`, `ending_cash`, `ending_equity`, `reserved_cash`, `fees_paid`, `gross_pnl`, `net_pnl`, `fill_rate`, and maximum drawdown; `total_pnl` remains as an alias for `net_pnl`.

Use `--config-dir /path/to/config` to supply a different `bot.yaml`, `risk.yaml`, or `strategies/spike.yaml`. The command always forces `backtest` mode, then writes signals, accepted/rejected orders, final positions, metrics, and an equity curve to the output file.

### Modeling limitations

This is a conservative taker simulator, not an execution-quality estimate:

- no maker queue or resting-order model; unfilled remainder is cancelled
- no latency model
- fixed configurable taker fee rather than market-specific Polymarket fee curves
- synthetic shorts reserve full payout collateral but do not emulate token minting
- no settlement/resolution event yet

Future work (maker queues, latency, settlement, parameter sweeps, market-specific fee curves) is intentionally out of scope until the taker simulator above is verified.

## What Should Happen

If the bot starts correctly:

- it runs in `dry_run` mode
- it does not place real trades
- it creates a `data/` folder if needed
- it starts writing runtime files

The main files it writes are:

- `data/journal/events.jsonl`
- `data/snapshots/state.json`
- `data/bot.sqlite3`

## How To Stop The Bot

Press:

```bash
Ctrl+C
```

The bot should stop cleanly and save a snapshot before exiting.

## Run The Tests

```bash
python3 -m pytest
```

## Runtime Files

By default, the bot writes:

- `data/journal/events.jsonl`
- `data/snapshots/state.json`
- `data/bot.sqlite3`

You can override the base directory with:

```bash
export BOT_DATA_DIR=/path/to/data
```

## Common Problems

### `python3: command not found`

Python is not installed yet, or macOS cannot find it.

Install Python 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/) and then run:

```bash
python3 --version
```

### `No module named pytest`

The test tools are not installed yet.

Run:

```bash
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

Then try:

```bash
python3 -m pytest
```

### `No module named pydantic` or another package name

The project dependencies are not installed yet, or the virtual environment is not active.

Run:

```bash
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

### `source .venv/bin/activate` says file not found

You have not created the virtual environment yet.

Run:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### The bot starts but seems quiet

That can be normal in early dry-run usage.

This version is mainly for:

- validating startup
- validating config loading
- validating risk and execution flow
- validating journaling and snapshots
- validating the runtime architecture

### I want to start over

Stop the bot first with `Ctrl+C`, then run:

```bash
rm -rf .venv
rm -rf data
```

Then repeat the steps in `## Fastest Setup`.

## Important Live-Mode Note

`live` mode is intentionally scaffolded but guarded.

This project does **not** guess unsupported Polymarket SDK hosts, methods, or auth flows. Any uncertainty is isolated in `clients/clob_client.py`, and live startup remains blocked until that adapter is explicitly validated against the official SDK behavior you plan to use.
