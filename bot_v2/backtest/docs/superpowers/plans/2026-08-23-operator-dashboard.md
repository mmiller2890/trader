# Local Operator Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local web console that safely starts and observes the dry-run bot, exposes shared runtime state, runs redacted preflight checks, edits only token subscriptions, and provides confirmed halt/cancel controls.

**Architecture:** Extract the existing bot lifecycle into one async `BotRuntime`, then let a loopback-only FastAPI application own that runtime. Typed read models serialize live state or historical snapshots without secrets; plain JavaScript polls once per second and calls origin/token-protected control endpoints.

**Tech Stack:** Python 3.11+, FastAPI 0.141.x, Uvicorn 0.52.x, Jinja2 3.1.x, Pydantic 2, pytest, pytest-asyncio, httpx, plain HTML/CSS/JavaScript.

**Spec:** `backtest/docs/superpowers/specs/2026-08-23-operator-dashboard-design.md`

## Global Constraints

- Bind only to `127.0.0.1`; reject non-loopback hosts in `dashboard.main`.
- Never serialize or log secret values; readiness exposes booleans only.
- Dashboard start must reject live configuration with `live_start_disabled_pending_review`.
- State changes require same-origin validation and `X-Operator-Token`.
- `HALT` and `CANCEL ALL` are exact, case-sensitive confirmation phrases.
- Configuration editing is stopped-only and allowlists only subscribed and strategy target token IDs.
- Write local configuration atomically to ignored `config/operator.yaml`.
- Preserve the existing `python -m app.main` command and shutdown behavior.
- Use test-first red-green-refactor cycles for every production behavior.
- Do not store or use credentials supplied through chat.

---

### Task 1: Shared Bot Runtime Lifecycle

**Files:**
- Create: `app/runtime.py`
- Modify: `app/main.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Consumes: `bootstrap_app()`, `housekeeping_loop()`, `shutdown_app()`, `Mode`.
- Produces: `RuntimePhase`, `RuntimeStatus`, `ControlResult`, and `BotRuntime` with async `start()`, `stop()`, `emergency_halt()`, `cancel_all()`, and `wait()`.

- [ ] **Step 1: Write failing lifecycle tests**

Create fake services with a state store, reconciliation probe, WebSocket manager, submitter, and snapshot store. Assert stopped-to-running-to-stopped transitions, idempotent start/stop, and live-start refusal:

```python
@pytest.mark.asyncio
async def test_runtime_starts_and_stops_dry_run() -> None:
    services = fake_services(mode=Mode.DRY_RUN)
    runtime = BotRuntime(bootstrap=lambda _: async_value(services))
    assert (await runtime.start()).phase == RuntimePhase.RUNNING
    assert services.ws_manager.started is True
    assert (await runtime.stop()).phase == RuntimePhase.STOPPED
    assert services.ws_manager.stopped is True

@pytest.mark.asyncio
async def test_runtime_refuses_live_start_from_dashboard() -> None:
    runtime = BotRuntime(bootstrap=lambda _: async_value(fake_services(mode=Mode.LIVE)))
    status = await runtime.start(allow_live=False)
    assert status.phase == RuntimePhase.FAILED
    assert status.reason == "live_start_disabled_pending_review"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/python -m pytest tests/test_runtime.py -q`

Expected: collection fails because `app.runtime` does not exist.

- [ ] **Step 3: Implement the lifecycle object**

Implement explicit phases and serialize transitions with one lock:

```python
class RuntimePhase(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    HALTED = "halted"
    FAILED = "failed"
```

The exact `BotRuntime` public signatures are `start(config_dir: Path | None = None, *, allow_live: bool = True) -> RuntimeStatus`, `stop() -> RuntimeStatus`, `emergency_halt(confirmation: str) -> RuntimeStatus`, `cancel_all(confirmation: str) -> ControlResult`, and `wait() -> None`.

Reuse startup reconciliation, heartbeat, BOT_STARTED emission, WebSocket start, housekeeping task, and `shutdown_app`. On emergency halt, call `set_kill_switch(True)` before cancel-all. Preserve cancellation errors in `last_control_error`.

- [ ] **Step 4: Make the CLI use `BotRuntime`**

Replace duplicated lifecycle setup in `app.main.run()` with:

```python
runtime = BotRuntime()
await runtime.start(allow_live=True)
await runtime.wait()
await runtime.stop()
```

Signal handlers call `runtime.request_stop`; CLI live behavior remains guarded by existing configuration and preflight.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_runtime.py tests/test_live_kill_switch.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit the lifecycle checkpoint**

```bash
git add bot_v2/app/runtime.py bot_v2/app/main.py bot_v2/tests/test_runtime.py
git commit -m "feat: extract controllable bot runtime"
```

### Task 2: Safe Operator Configuration Overlay

**Files:**
- Create: `dashboard/config_editor.py`
- Create: `config/operator.example.yaml`
- Modify: `config/loader.py`
- Modify: `.gitignore`
- Test: `tests/test_dashboard_config.py`

**Interfaces:**
- Consumes: `load_config(config_dir)` and YAML configuration fragments.
- Produces: `EditableConfig`, `OperatorConfigEditor.load()`, and `OperatorConfigEditor.save(config)`.

- [ ] **Step 1: Write failing overlay tests**

Test overlay precedence, decimal token validation, stable deduplication, extra-field rejection, stopped-only enforcement, and preservation of live safety flags:

```python
def test_operator_overlay_changes_only_token_lists(config_dir: Path) -> None:
    editor = OperatorConfigEditor(config_dir / "operator.yaml", is_running=lambda: False)
    editor.save(EditableConfig(subscribed_token_ids=["123", "123"], target_token_ids=["123"]))
    config = load_config(config_dir)
    assert config.market_data.subscribed_token_ids == ["123"]
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True

def test_operator_overlay_rejects_non_decimal_token() -> None:
    with pytest.raises(ValidationError):
        EditableConfig(subscribed_token_ids=["token-x"], target_token_ids=[])
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_config.py -q`

Expected: import failure for `dashboard.config_editor`.

- [ ] **Step 3: Implement strict models and atomic persistence**

Use `ConfigDict(extra="forbid")`, a list validator that accepts at most 20 non-empty decimal strings, and `tempfile.NamedTemporaryFile` in the destination directory followed by `Path.replace`. Persist exactly:

```yaml
market_data:
  subscribed_token_ids: ["123"]
spike_strategy:
  target_token_ids: ["123"]
```

Load `operator.yaml` after strategy/risk fragments and before environment secrets. Add `config/operator.yaml` to `.gitignore` and provide an empty example overlay.

- [ ] **Step 4: Verify GREEN and configuration regressions**

Run: `.venv/bin/python -m pytest tests/test_dashboard_config.py tests/test_config.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the configuration checkpoint**

```bash
git add bot_v2/dashboard/config_editor.py bot_v2/config/operator.example.yaml bot_v2/config/loader.py bot_v2/.gitignore bot_v2/tests/test_dashboard_config.py
git commit -m "feat: add safe operator config overlay"
```

### Task 3: Secret-Free Dashboard Read Model

**Files:**
- Create: `dashboard/__init__.py`
- Create: `dashboard/models.py`
- Create: `dashboard/read_model.py`
- Modify: `state/store.py`
- Test: `tests/test_dashboard_read_model.py`

**Interfaces:**
- Consumes: `BotRuntime`, `AppConfig`, `StateSnapshot`, `JsonlJournal`, `InMemoryStateStore`.
- Produces: `DashboardState`, `HeartbeatView`, `ReadinessItem`, `CredentialReadiness`, `tail_events()`, and `DashboardReadModel.build()`.

- [ ] **Step 1: Write failing serialization tests**

Build live and stopped read models and assert phase, historical flag, heartbeat age, orders, positions, and blockers. Seed configuration with sentinel secret strings and assert none appear in `model_dump_json()`:

```python
@pytest.mark.asyncio
async def test_read_model_never_serializes_secrets(tmp_path: Path) -> None:
    config = AppConfig(secrets={"private_key": "never-return-me", "clob_secret": "also-private"})
    payload = (await build_read_model(config=config, runtime=stopped_runtime(), data_dir=tmp_path)).model_dump_json()
    assert "never-return-me" not in payload
    assert "also-private" not in payload
    assert '"private_key_configured":true' in payload
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_read_model.py -q`

Expected: import failure for dashboard models.

- [ ] **Step 3: Implement typed read models**

Include runtime phase, mode, source (`live` or `historical`), kill switch, credential booleans, subscription counts, heartbeats, open orders, positions, balances, aggregate exposure/PnL, last control error, and readiness items. Include permanent blocker `live_start_disabled_pending_review` in this release.

Add `get_heartbeats() -> dict[str, datetime]` to `InMemoryStateStore` so the read model does not probe private fields.

- [ ] **Step 4: Implement bounded journal tailing**

Read no more than the final 256 KiB, return the newest `limit` valid `BotEvent` objects, and count malformed lines as warnings. Never include arbitrary raw malformed content in the response.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_dashboard_read_model.py tests/test_state_store.py tests/test_snapshots.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the read-model checkpoint**

```bash
git add bot_v2/dashboard bot_v2/state/store.py bot_v2/tests/test_dashboard_read_model.py
git commit -m "feat: expose secret-free dashboard state"
```

### Task 4: Dashboard Controller and Redacted Preflight

**Files:**
- Create: `dashboard/controller.py`
- Create: `dashboard/preflight.py`
- Test: `tests/test_dashboard_controller.py`

**Interfaces:**
- Consumes: `BotRuntime`, `OperatorConfigEditor`, `DashboardReadModel`, and `scripts.live_preflight.main` behavior.
- Produces: `DashboardController.state()`, `events()`, `start()`, `stop()`, `halt()`, `cancel_all()`, `run_preflight()`, `get_config()`, and `save_config()`.

- [ ] **Step 1: Write failing controller tests**

Assert dry-run start, exact halt/cancel phrases, live refusal, stopped-only save, and persistence of preflight results. Use fake runtime methods that record call order, not exchange mocks.

```python
@pytest.mark.asyncio
async def test_halt_requires_exact_confirmation(controller: DashboardController) -> None:
    with pytest.raises(ConfirmationError):
        await controller.halt("halt")
    await controller.halt("HALT")
    assert controller.runtime.calls == ["kill_switch", "cancel_all"]
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_controller.py -q`

Expected: import failure for controller.

- [ ] **Step 3: Implement controller mapping**

Keep operator-facing exceptions typed: `ConfirmationError`, `RuntimeConflictError`, and `PreflightBusyError`. Protect preflight with its own lock and retain the last redacted report and timestamp.

- [ ] **Step 4: Implement preflight subprocess boundary**

Run `[sys.executable, "-m", "scripts.live_preflight", "--config-dir", str(config_dir)]` with `asyncio.create_subprocess_exec`, a 30-second timeout, captured output, and no shell. Parse JSON on exit 0 or 2; otherwise return a redacted `PreflightView` whose reason contains only the exception class or a known safe preflight prefix. Never copy environment secret values into command arguments.

- [ ] **Step 5: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_dashboard_controller.py tests/test_live_preflight.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit the controller checkpoint**

```bash
git add bot_v2/dashboard/controller.py bot_v2/dashboard/preflight.py bot_v2/tests/test_dashboard_controller.py
git commit -m "feat: add dashboard operator controls"
```

### Task 5: Loopback-Only FastAPI Application

**Files:**
- Create: `dashboard/app.py`
- Create: `dashboard/main.py`
- Modify: `pyproject.toml`
- Test: `tests/test_dashboard_api.py`

**Interfaces:**
- Consumes: `DashboardController` and typed request/response models.
- Produces: `create_app(controller, operator_token=None, trusted_origins=None) -> FastAPI` and `python -m dashboard.main`.

- [ ] **Step 1: Add pinned-compatible web dependencies**

Add:

```toml
"fastapi>=0.141.1,<0.142",
"uvicorn>=0.52.1,<0.53",
"Jinja2>=3.1.6,<4",
```

Install with `uv sync --extra dev` and do not commit a generated lockfile unless the repository deliberately adopts it.

- [ ] **Step 2: Write failing API security and behavior tests**

Use `httpx.AsyncClient(transport=ASGITransport(app=app))`. Assert GET routes, token/origin rejection, control status codes, extra-field rejection, secret absence, and exact confirmation handling.

```python
@pytest.mark.asyncio
async def test_mutation_requires_same_origin_and_operator_token(client: AsyncClient) -> None:
    assert (await client.post("/api/control/start")).status_code == 403
    response = await client.post(
        "/api/control/start",
        headers={"Origin": "http://127.0.0.1:8000", "X-Operator-Token": "test-token"},
    )
    assert response.status_code == 200
```

- [ ] **Step 3: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_api.py -q`

Expected: import failure for `dashboard.app`.

- [ ] **Step 4: Implement FastAPI routes and mutation guard**

Generate the production token with `secrets.token_urlsafe(32)`. Render it only into the root page. For every POST/PUT route require trusted Origin and `hmac.compare_digest` on the operator token. Use strict Pydantic request bodies and map typed controller exceptions to 403/409/422 without leaking exception internals.

- [ ] **Step 5: Implement local entrypoint**

Parse `--host` and `--port`; accept only `127.0.0.1`, `localhost`, or `::1`. Start Uvicorn with access logging disabled so operator tokens cannot enter logs. Print `Operator dashboard: http://127.0.0.1:8000`.

- [ ] **Step 6: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_dashboard_api.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit the API checkpoint**

```bash
git add bot_v2/dashboard/app.py bot_v2/dashboard/main.py bot_v2/pyproject.toml bot_v2/tests/test_dashboard_api.py
git commit -m "feat: serve loopback operator dashboard API"
```

### Task 6: Responsive Operator Console UI

**Files:**
- Create: `dashboard/templates/index.html`
- Create: `dashboard/static/dashboard.css`
- Create: `dashboard/static/dashboard.js`
- Modify: `dashboard/app.py`
- Test: `tests/test_dashboard_ui.py`

**Interfaces:**
- Consumes: `/api/state`, `/api/events`, `/api/config`, and control endpoints.
- Produces: a keyboard-accessible local dashboard at `/`.

- [ ] **Step 1: Write failing UI contract tests**

Assert the HTML includes labeled runtime, safety, health, portfolio, order, position, configuration, and event regions; destructive dialog labels; CSS/JS assets; and the operator token without any config secrets.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m pytest tests/test_dashboard_ui.py -q`

Expected: root template or required semantic regions are missing.

- [ ] **Step 3: Build the semantic HTML shell**

Use `<header>`, `<main>`, `<section aria-labelledby>`, real `<button>` controls, `<dialog>` confirmation forms, status text with `aria-live="polite"`, and tables with captions. Embed the operator token in a nonce-free same-origin bootstrap object and never place it in a URL.

- [ ] **Step 4: Add polished responsive styling**

Use CSS custom properties, a high-contrast dark palette, a sticky safety rail, responsive card grids, tabular numerals, visible focus states, status icons plus text, and a single-column layout below 760px. Respect `prefers-reduced-motion`.

- [ ] **Step 5: Add browser behavior**

Poll state every second with request overlap prevention; render values using `textContent`, never `innerHTML`; disable actions during transitions; validate token lists locally and server-side; open exact-confirmation dialogs for halt/cancel-all; preserve focus on close; and show network failure without changing bot state.

- [ ] **Step 6: Verify GREEN**

Run: `.venv/bin/python -m pytest tests/test_dashboard_ui.py tests/test_dashboard_api.py -q`

Expected: all tests pass.

- [ ] **Step 7: Commit the UI checkpoint**

```bash
git add bot_v2/dashboard/templates bot_v2/dashboard/static bot_v2/dashboard/app.py bot_v2/tests/test_dashboard_ui.py
git commit -m "feat: build operator console interface"
```

### Task 7: Documentation and Complete Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/live-runbook.md`
- Test: existing complete suite.

**Interfaces:**
- Consumes: completed dashboard command and safety behavior.
- Produces: exact setup, run, and emergency-use instructions.

- [ ] **Step 1: Document dashboard operation**

Add exact commands:

```bash
cd /Users/ghost/Projects/trader/bot_v2
source .venv/bin/activate
python -m dashboard.main
```

Document loopback-only access, dry-run-only dashboard start, token-list editing, preflight, halt/cancel confirmations, and the fact that chat-exposed credentials must never be reused.

- [ ] **Step 2: Run formatting and static checks**

Run:

```bash
git diff --check
.venv/bin/python -m compileall -q app dashboard
```

Expected: both commands exit 0.

- [ ] **Step 3: Run the full test suite with warnings as errors**

Run: `.venv/bin/python -m pytest -q -W error::DeprecationWarning`

Expected: all tests pass with no deprecation warnings.

- [ ] **Step 4: Start the dashboard and perform visual verification**

Run: `.venv/bin/python -m dashboard.main --port 8000`

Open `http://127.0.0.1:8000`, capture desktop and 390px-wide screenshots, verify stopped and running dry-run states, exercise preflight and safe config saving, inspect browser console errors, then gracefully stop the bot and dashboard.

- [ ] **Step 5: Verify repository hygiene**

Confirm `config/operator.yaml`, `.env`, `.venv`, runtime data, credentials, and screenshot artifacts are untracked. Confirm no unrelated user changes were staged.

- [ ] **Step 6: Commit documentation only if requested by the operator**

```bash
git add bot_v2/README.md bot_v2/docs/live-runbook.md
git commit -m "docs: add operator dashboard runbook"
```
