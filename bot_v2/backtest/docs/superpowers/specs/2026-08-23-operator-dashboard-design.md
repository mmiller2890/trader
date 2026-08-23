# Local Operator Dashboard Design

## Purpose

Build a local, safety-focused web console for operating and observing the Polymarket bot without turning the browser into an unrestricted trading terminal. The console must make dry-run operation approachable, expose the bot's real runtime state, and provide immediate emergency controls while preserving the bot's existing fail-closed posture.

The first release is an integrated Python application served only on `127.0.0.1`. It owns the bot runtime in the same process so lifecycle and emergency commands act on the real in-memory services rather than stale files or a delayed inter-process queue.

## Scope

The console provides:

- bot start and graceful stop;
- emergency kill switch and cancel-all;
- current mode, runtime phase, WebSocket state, and safety status;
- heartbeat age and freshness for app, market data, housekeeping, and execution;
- open orders, positions, balances, and available PnL values;
- recent signals, risk decisions, order results, errors, and kill-switch events;
- read-only live preflight execution and its redacted results;
- a safe configuration editor for subscribed token IDs and strategy target token IDs;
- visible launch blockers and exact operator guidance;
- a responsive local web UI with no Node.js build step.

The first release does not provide:

- manual order entry;
- editing of private keys, API credentials, funder addresses, RPC URLs, or Telegram secrets;
- editing of `bot.mode`, `allow_live_trading`, `dry_run_force`, order size, notional caps, or risk limits;
- browser-based activation of live trading;
- remote access, multi-user accounts, cloud hosting, or mobile push notifications;
- a promise that orders will fill.

## Safety Contract

1. The HTTP server binds to `127.0.0.1` by default. A non-loopback bind is rejected unless a future authenticated remote-access design explicitly replaces this rule.
2. The dashboard never returns secret values or serializes the `secrets` configuration object.
3. State-changing endpoints require a per-process operator token embedded only in the same-origin dashboard page and supplied in an `X-Operator-Token` header.
4. State-changing requests reject missing or foreign `Origin` headers except for test clients using an explicit trusted-host configuration.
5. Start is allowed in dry-run mode. If loaded configuration is live, dashboard start is rejected with `live_start_disabled_pending_review` in this release, regardless of credentials or preflight status.
6. Stop is graceful and uses the existing shutdown path. In live mode that path attempts cancel-all before completing.
7. Emergency halt first activates the in-memory kill switch, then attempts cancel-all when live services exist. Cancellation failure cannot clear the kill switch.
8. Explicit cancel-all requires the exact confirmation phrase `CANCEL ALL`. The UI also displays the active mode and open-order count beside the confirmation control.
9. Configuration writes are accepted only while the bot is stopped and only for the two allowlisted token-ID lists.
10. Configuration is written atomically to a local ignored overlay, `config/operator.yaml`; base safety configuration remains unchanged.
11. Read-only preflight never submits, signs, cancels, or modifies orders. It may fail because credentials or network access are missing, and that failure is shown without secrets.
12. Any internal exception produces a typed error response and a journaled operator event where possible. The UI must never reinterpret an exception as success.

## Architecture

### Runtime ownership

`app/runtime.py` introduces a `BotRuntime` lifecycle object extracted from the current `app.main.run` flow. It owns `AppServices`, the stop event, and background tasks. Its public async interface is:

- `start(config_dir: Path | None = None) -> RuntimeStatus`
- `stop() -> RuntimeStatus`
- `emergency_halt(confirmation: str) -> RuntimeStatus`
- `cancel_all(confirmation: str) -> ControlResult`
- `status() -> RuntimeStatus`
- `services -> AppServices | None` as a read-only property

`start` is idempotent when already running and refuses overlapping startup attempts. `stop` is idempotent when stopped. Failed startup returns to a stopped state with a redacted failure reason. The existing `python -m app.main` command uses the same lifecycle object, so the CLI and dashboard cannot drift into separate implementations.

Runtime phases are `stopped`, `starting`, `running`, `stopping`, `halted`, and `failed`. A single `asyncio.Lock` serializes lifecycle transitions.

### Dashboard application

The `dashboard` package contains focused units:

- `dashboard/app.py`: FastAPI factory, middleware, dependency wiring, and route registration;
- `dashboard/controller.py`: maps authenticated operator commands to `BotRuntime` operations;
- `dashboard/read_model.py`: creates secret-free typed dashboard responses from config, state, snapshots, and events;
- `dashboard/config_editor.py`: validates and atomically persists the allowlisted operator overlay;
- `dashboard/preflight.py`: runs the existing read-only preflight behind a bounded async interface;
- `dashboard/main.py`: local Uvicorn entrypoint;
- `dashboard/templates/index.html`: semantic dashboard shell;
- `dashboard/static/dashboard.css`: responsive operator-console styling;
- `dashboard/static/dashboard.js`: polling, rendering, and confirmed operator actions.

FastAPI serves the page and JSON API. Plain browser JavaScript polls `/api/state` once per second. Polling is chosen over a WebSocket or SSE layer because the state volume is small, one-second operator visibility is sufficient, and reconnect behavior stays transparent.

### Data flow

The dashboard read model has two modes:

- While the bot is running, it reads `InMemoryStateStore` and the live service graph directly.
- While stopped, it loads the last state snapshot and journal so the page still shows the previous run and clearly labels it historical.

Recent events are read from the append-only JSONL journal with a bounded tail of 100 valid events. Malformed trailing lines are reported as a data-quality warning rather than crashing the page.

No browser response includes configuration secrets. Credential readiness is represented only as booleans such as `private_key_configured`.

### Configuration overlay

`config/loader.py` loads optional `config/operator.yaml` after the checked-in YAML fragments and before environment secrets. The dashboard editor accepts:

- `market_data.subscribed_token_ids`;
- `spike_strategy.target_token_ids`.

Every token ID is a non-empty decimal string, duplicates are removed while preserving order, and at most 20 values are accepted. Saving an empty subscription list is allowed for a stopped dry-run bot but is shown as a readiness blocker. The overlay file is ignored by Git; `config/operator.example.yaml` documents its schema.

### API surface

Read endpoints:

- `GET /`: dashboard HTML;
- `GET /api/state`: complete secret-free read model;
- `GET /api/events?limit=100`: bounded recent events;
- `GET /api/config`: allowlisted editable configuration and read-only safety fields.

Control endpoints:

- `POST /api/control/start`;
- `POST /api/control/stop`;
- `POST /api/control/halt` with `{"confirmation": "HALT"}`;
- `POST /api/control/cancel-all` with `{"confirmation": "CANCEL ALL"}`;
- `POST /api/preflight`;
- `PUT /api/config` with only the two allowlisted token lists.

Responses use typed Pydantic models. Expected operator errors use 409 for invalid runtime state, 422 for invalid confirmation or configuration, and 403 for origin/token failures. Unexpected failures use a redacted 500 response and are logged.

## User Interface

The page uses a dark, high-contrast console layout:

1. A persistent safety rail shows mode, runtime phase, kill-switch state, preflight state, and whether data is live or historical.
2. Primary controls show Start Dry Run, Graceful Stop, Emergency Halt, Run Preflight, and Cancel All. Dangerous controls are visually separated and require typed confirmation.
3. Health cards show each heartbeat's timestamp, age, and fresh/stale/missing state.
4. Portfolio cards show position count, exposure, reported PnL, balance, and open-order count, while clearly labeling unavailable values.
5. Tables show open orders and positions without truncating identifiers needed for investigation.
6. The configuration panel edits subscription and strategy target token IDs only.
7. The event timeline supports severity and event-type filtering in the browser.
8. A launch-readiness panel lists every blocker rather than presenting a single green/red badge.

All controls remain keyboard accessible. Status is conveyed through text and icons in addition to color. Destructive confirmation dialogs return focus to the invoking control when closed.

## Error Handling and Recovery

- A dashboard server error does not silently stop a running bot.
- A bot startup error leaves the dashboard alive, displays the redacted reason, and permits another dry-run attempt after configuration is corrected.
- A lost browser connection has no effect on bot operation.
- Repeated start/stop clicks are serialized and return the current transition instead of spawning duplicate runtimes.
- Emergency halt is safe to repeat.
- Cancel-all errors remain visible until a subsequent explicitly successful operation; they are never cleared by refreshing the page.
- Snapshot or journal read errors are displayed as warnings while live in-memory state remains usable.

## Testing Strategy

Implementation follows red-green-refactor cycles.

Unit coverage includes:

- lifecycle state transitions and idempotence;
- live-start refusal;
- emergency halt ordering and cancellation-failure behavior;
- exact destructive confirmation phrases;
- allowlisted configuration parsing, validation, atomic writes, and preservation of safety settings;
- secret-free read-model serialization;
- heartbeat freshness and historical/live labeling;
- bounded journal tailing with malformed-line handling.

API coverage uses FastAPI's ASGI test client and includes:

- page and state responses;
- operator-token and origin enforcement;
- start, stop, halt, cancel-all, preflight, and configuration response codes;
- rejection of extra configuration fields;
- confirmation that secret strings never appear in any response.

Integration coverage starts a fake runtime through the HTTP API, observes running state, halts it, and confirms both kill-switch activation and cancellation ordering. The existing full suite remains green with deprecation warnings treated as errors.

Visual verification runs the dashboard locally, captures desktop and narrow-width screenshots, checks control states in stopped and running dry-run modes, and verifies that no browser console errors occur.

## Dependencies and Operations

The Python project adds FastAPI, Uvicorn, and Jinja2 with compatible bounded versions selected during implementation from their current official releases. No frontend package manager or compilation step is introduced.

The operator command is:

```bash
cd /Users/ghost/Projects/trader/bot_v2
source .venv/bin/activate
python -m dashboard.main
```

The console prints the local URL, defaulting to `http://127.0.0.1:8000`. Existing `python -m app.main` behavior remains supported.

## Acceptance Criteria

The feature is complete when:

- the dashboard opens locally with a stopped bot and historical state clearly labeled;
- a configured dry-run bot can be started and gracefully stopped from the browser;
- emergency halt immediately activates the shared kill switch and attempts live cancel-all in the required order;
- live configuration cannot be started through the dashboard;
- preflight output is redacted and visible;
- subscription and strategy token IDs can be safely persisted only while stopped;
- secrets cannot be retrieved through HTML, JSON, errors, logs, or editable configuration;
- dashboard controls are keyboard accessible and usable at desktop and narrow widths;
- all dashboard tests and the existing repository suite pass without deprecation warnings.
