# Polymarket Bot v1

Production-minded, dry-run-first Polymarket bot built in Python 3.11+ with typed models, async runtime boundaries, structured JSON logging, and a conservative safety posture.

## Start Here

This bot starts in **safe `dry_run` mode** by default.

- It will **not place real trades**
- It is safe to run without live trading enabled
- You do **not** need Polymarket API secrets for a first local test

If you have never run a Python project before, follow the steps below exactly.

## What You Need

Before running the bot, install:

1. **Python 3.11 or newer**
2. **A terminal app**

On macOS:

1. Install Python from [python.org/downloads](https://www.python.org/downloads/)
2. Open the `Terminal` app

After installing Python, check that it worked:

```bash
python3 --version
```

You should see something like `Python 3.11.x` or `Python 3.12.x`.

## First-Time Setup

### 1. Open Terminal

Open the macOS `Terminal` app.

### 2. Go into the project folder

Use `cd` to move into this bot folder.

Example:

```bash
cd /full/path/to/bot
```

If you are not sure of the path, you can type `cd ` and then drag the `bot` folder into the Terminal window.

### 3. Create a virtual environment

Run:

```bash
python3 -m venv .venv
```

This creates a private Python environment just for this project.

### 4. Activate the virtual environment

Run:

```bash
source .venv/bin/activate
```

If activation worked, you will usually see `(.venv)` at the start of the Terminal line.

### 5. Install the project and test tools

Run:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -e ".[dev]"
```

This may take a minute.

### 6. Create your local environment file

Run:

```bash
cp .env.example .env
```

For a first dry-run test, you can leave `.env` alone.

If you later want Telegram alerts or live trading credentials, edit `.env` and fill in the values there.

### 7. Make sure the bot stays in safe mode

Open `config/bot.yaml` and confirm this line is present:

```yaml
mode: dry_run
```

Also confirm these lines are present under `execution`:

```yaml
dry_run_force: true
allow_live_trading: false
```

Those settings keep the bot from sending real orders.

## Run The Bot

From inside the `bot` folder, with the virtual environment activated, run:

```bash
python3 -m app.main
```

## What Should Happen

When the bot starts successfully:

- it should start in `dry_run` mode
- it should create a `data/` folder if one does not already exist
- it should write runtime files like snapshots and journals as it runs

Files created during runtime:

- `data/journal/events.jsonl`
- `data/snapshots/state.json`
- `data/bot.sqlite3`

## How To Stop It

In Terminal, press:

```bash
Ctrl+C
```

The bot will try to shut down cleanly and save a snapshot.

## Run The Tests

From inside the `bot` folder, with the virtual environment activated, run:

```bash
python3 -m pytest
```

## Very Common Problems

### `python3: command not found`

Python is not installed correctly. Install Python 3.11+ from [python.org/downloads](https://www.python.org/downloads/) and try again.

### `No module named ...`

You probably have not installed dependencies yet, or the virtual environment is not activated.

Run:

```bash
source .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

### `pytest: command not found`

Run tests this way instead:

```bash
python3 -m pytest
```

### Nothing seems to happen

That can be normal in early dry-run setup, especially if you have not configured live market subscriptions yet. The bot is still useful for validating startup, config loading, logging, persistence, and the execution/risk pipeline boundaries.

## If You Want To Start Over

From inside the `bot` folder:

1. Stop the bot with `Ctrl+C`
2. Remove the virtual environment:

```bash
rm -rf .venv
```

3. Remove runtime data if you want a fresh local state:

```bash
rm -rf data
```

## Runtime Modes

- `dry_run`: fully implemented in v1
- `live`: guarded behind explicit safety checks and intentionally blocked at bootstrap pending explicit SDK host wiring
- `backtest`: skeletal boundary
- `replay`: skeletal boundary

## Polymarket SDK Boundary

All Polymarket SDK uncertainty is intentionally isolated to `clients/clob_client.py`.

- SDK init differences are contained in the adapter
- Method-name differences are contained in the adapter
- Dry-run mode does not submit real orders
- Live bootstrap is intentionally guarded rather than guessing unsupported host/auth details

## Data Files

By default, runtime artifacts are written under `data/`:

- `data/journal/events.jsonl`
- `data/snapshots/state.json`
- `data/bot.sqlite3`

You can override the base directory with `BOT_DATA_DIR`.

## What v1 Includes

- Typed config loading from YAML + env overlays
- Typed domain models across all internal boundaries
- Async-safe in-memory state store
- Structured JSON logging
- WebSocket manager + normalized market-data client boundary
- Deterministic spike strategy
- Pre-trade and runtime risk checks
- Dry-run execution pipeline with synthetic order results
- JSONL event journal
- Snapshot persistence and healthcheck utility
- Minimal reconciliation boundary
- Skeletal backtest/replay boundaries
- Focused unit tests for config, risk, strategy, order building, and state

## Safety Model

- Default mode is `dry_run`
- Every `TradeSignal` passes through pre-trade risk before execution
- `live` mode is guarded and intentionally not enabled by default
- Kill-switch state is tracked in the shared state store
- Runtime risk can trip the kill switch on stale heartbeats, loss thresholds, or repeated failures

## Project Structure

- `app/`: bootstrap, runtime loop, shutdown helpers
- `config/`: typed schema, loader, starter YAML config
- `clients/`: Polymarket/WS/client boundaries
- `models/`: typed market, signal, order, risk, event, position models
- `state/`: in-memory store, reconciliation, cache
- `strategies/`: strategy interface and spike implementation
- `risk/`: policy interfaces, pretrade/runtime risk, circuit breaker
- `execution/`: order builder, submitter, tracker, router
- `portfolio/`: sizing, exposure, pnl helpers
- `persistence/`: journal, snapshots, sqlite key-value helper
- `notifications/`: event bus and Telegram notifier
- `backtest/`: replay/metrics boundaries
- `scripts/`: healthcheck
- `tests/`: unit tests
