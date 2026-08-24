# Bitcoin 15-Minute Automatic Market Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically discover, subscribe to, and rotate both outcome tokens for Polymarket's active Bitcoin Up/Down 15-minute market so a credential-free dry run remains healthy across market windows.

**Architecture:** A strict public Gamma API client returns a typed `DiscoveredMarket`. A rotation coordinator initializes the first subscription before runtime start and replaces WebSocket asset IDs near each boundary by forcing a reconnect. Runtime and dashboard models expose safe discovery state while existing heartbeat and kill-switch policies remain authoritative.

**Tech Stack:** Python 3.11+, asyncio, httpx, Pydantic 2, websockets 14–17, FastAPI, pytest, pytest-asyncio, plain JavaScript.

**Spec:** `backtest/docs/superpowers/specs/2026-08-23-btc-15m-auto-rotation-design.md`

## Global Constraints

- Automatic discovery is restricted to Bitcoin and 15-minute windows in this release.
- Discovery uses only the public Gamma API and never receives or serializes credentials.
- Subscribe to both `Up` and `Down` CLOB token IDs in validated outcome order.
- Reject closed, expired, mismatched, malformed, duplicated, or non-decimal tokens.
- Startup fails before WebSocket start when the current market cannot be validated.
- Rotation replaces subscriptions through an intentional WebSocket reconnect.
- Existing heartbeat, kill-switch, live preflight, and dashboard live-start lock remain authoritative.
- Static token configuration continues to work when automatic discovery is disabled.
- No raw remote response body or secret value may enter logs, exceptions, events, or dashboard responses.
- Every production behavior is implemented through a witnessed red-green-refactor cycle.

---

### Task 1: Typed Configuration and Gamma Discovery

**Files:**
- Create: `clients/gamma_markets.py`
- Modify: `config/schema.py`
- Modify: `config/bot.yaml`
- Test: `tests/test_gamma_markets.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`, UTC-aware `datetime`, `MarketDataConfig`.
- Produces: `AutomaticMarketConfig`, `MarketOutcome`, `DiscoveredMarket`, `MarketDiscoveryError`, `window_start_epoch()`, `GammaMarketDiscoveryClient.discover_active(now=None)`, and `GammaMarketDiscoveryClient.close()`.

- [ ] **Step 1: Write failing configuration and epoch tests**

```python
def test_window_start_epoch_floors_to_utc_quarter_hour() -> None:
    now = datetime(2026, 8, 24, 2, 59, 59, tzinfo=UTC)
    assert window_start_epoch(now) == 1787539500

def test_checked_in_config_enables_btc_15m_discovery() -> None:
    config = load_config()
    assert config.market_data.automatic_market.enabled is True
    assert config.market_data.automatic_market.asset == "btc"
    assert config.market_data.automatic_market.duration_minutes == 15
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_gamma_markets.py tests/test_config.py`

Expected: collection fails because `clients.gamma_markets` and `automatic_market` do not exist.

- [ ] **Step 3: Add the strict automatic-market schema**

Add `AutomaticMarketConfig` with `extra="forbid"`, `enabled=False`, `asset: Literal["btc"]`, `duration_minutes: Literal[15]`, fixed HTTPS Gamma base URL, slug prefix, `refresh_lead_seconds` bounded from 1 to 60, and request timeout bounded from 1 to 30. Add it to `MarketDataConfig` and enable the approved profile in `config/bot.yaml`.

- [ ] **Step 4: Implement epoch calculation and typed discovery models**

```python
def window_start_epoch(now: datetime, duration_minutes: int = 15) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    seconds = duration_minutes * 60
    epoch = int(now.astimezone(UTC).timestamp())
    return epoch - (epoch % seconds)
```

`DiscoveredMarket.asset_ids` returns `[up.token_id, down.token_id]`. Every model uses `ConfigDict(extra="forbid")`.

- [ ] **Step 5: Write failing response-validation tests**

Use `httpx.MockTransport` and assert a valid payload maps outcomes positionally. Add separate tests for closed/expired markets, slug/window mismatch, missing outcomes, unequal array lengths, duplicate IDs, non-decimal IDs, and a response body sentinel that must not appear in `MarketDiscoveryError`.

- [ ] **Step 6: Run the focused tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_gamma_markets.py`

Expected: validation tests fail because `discover_active()` is not implemented.

- [ ] **Step 7: Implement the minimal Gamma client**

Generate the slug locally, request only `/events/slug/{slug}`, call `raise_for_status()`, parse JSON, validate the exact event/market contract, and return safe reason codes. Convert transport, timeout, status, JSON, and validation failures into `MarketDiscoveryError(reason)` without copying response content.

- [ ] **Step 8: Verify GREEN**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_gamma_markets.py tests/test_config.py`

Expected: all focused tests pass.

### Task 2: Atomic WebSocket Subscription Replacement

**Files:**
- Modify: `clients/ws_client.py`
- Modify: `tests/test_ws_client.py`

**Interfaces:**
- Consumes: validated decimal asset IDs from `DiscoveredMarket.asset_ids`.
- Produces: `WebSocketManager.asset_ids`, `WebSocketManager.replace_asset_ids(asset_ids) -> bool`.

- [ ] **Step 1: Write failing replacement tests**

```python
@pytest.mark.asyncio
async def test_replace_asset_ids_closes_socket_and_reconnect_uses_new_ids() -> None:
    manager = make_manager(socket=first, asset_ids=["1", "2"])
    manager._ws = first
    assert await manager.replace_asset_ids(["3", "4"]) is True
    assert first.closed is True
    await manager._consume_connection()
    assert json.loads(second.sent[0])["assets_ids"] == ["3", "4"]

@pytest.mark.asyncio
async def test_replace_asset_ids_is_noop_for_same_ids() -> None:
    assert await manager.replace_asset_ids(["1", "2"]) is False
```

Add rejection tests for empty, duplicated, non-decimal, and replacement-after-stop inputs.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_ws_client.py`

Expected: `replace_asset_ids` is missing.

- [ ] **Step 3: Implement locked replacement**

Add an async subscription lock. Normalize IDs without reordering, compare under the lock, update the list, capture the current socket, then close it outside the lock. `_consume_connection()` copies the IDs under the lock immediately before sending the subscription. `stop()` marks the manager stopping before closing the socket.

- [ ] **Step 4: Verify GREEN and reconnect regressions**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_ws_client.py`

Expected: all WebSocket tests pass with no deprecation warnings.

### Task 3: Boundary-Aware Rotation Coordinator

**Files:**
- Create: `clients/market_rotation.py`
- Test: `tests/test_market_rotation.py`

**Interfaces:**
- Consumes: `GammaMarketDiscoveryClient`, `WebSocketManager.replace_asset_ids()`, injected UTC clock and async sleep.
- Produces: `MarketRotationState`, `MarketRotationStatus`, `Btc15mMarketRotator.initialize()`, `run(stop_event)`, `stop()`, and `status()`.

- [ ] **Step 1: Write failing initialization tests**

Assert `initialize()` stores a healthy current market, exposes its two tokens, and propagates a safe discovery failure without calling WebSocket replacement.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_market_rotation.py`

Expected: collection fails because `clients.market_rotation` does not exist.

- [ ] **Step 3: Implement typed status and initialization**

Use states `disabled`, `starting`, `healthy`, `degraded`, and `failed`. Status contains only public market fields, last success timestamp, and a safe reason. `initialize()` must be idempotent for the same current market.

- [ ] **Step 4: Write failing boundary and retry tests**

With an injected mutable clock, assert the coordinator waits until `end_at - refresh_lead_seconds`, ignores an unchanged market, replaces both IDs for a changed market, and retries safe discovery failures with delays capped at the remaining boundary interval.

- [ ] **Step 5: Run tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_market_rotation.py`

Expected: rotation tests fail because `run()` has no boundary behavior.

- [ ] **Step 6: Implement rotation and shutdown**

Use `asyncio.wait_for(stop_event.wait(), timeout=delay)` instead of an uninterruptible sleep. On success, replace IDs and atomically update status. On failure, set `degraded` with the safe reason and retry with 1, 2, 4, then at most 10 seconds. `stop()` closes the discovery client exactly once.

- [ ] **Step 7: Verify GREEN**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_market_rotation.py tests/test_ws_client.py`

Expected: all coordinator and WebSocket tests pass.

### Task 4: Bootstrap, Runtime, and Shutdown Integration

**Files:**
- Modify: `app/bootstrap.py`
- Modify: `app/runtime.py`
- Modify: `app/shutdown.py`
- Modify: `strategies/spike.py` only if a small runtime-target interface is required; prefer a copied effective config in bootstrap.
- Test: `tests/test_bootstrap.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Consumes: automatic-market config, discovery client, initialized rotator.
- Produces: `AppServices.market_rotator`, runtime-owned rotation task, cleanup on every startup/shutdown path.

- [ ] **Step 1: Write failing bootstrap tests**

Inject a discovery-client factory into bootstrap. Assert automatic mode initializes before WebSocket construction, passes both token IDs to `WebSocketManager`, copies spike configuration with an empty runtime target list, and static mode performs no discovery.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_bootstrap.py`

Expected: bootstrap has no discovery factory or rotator service.

- [ ] **Step 3: Wire automatic discovery into bootstrap**

Construct the client and rotator only when enabled, discover before returning services, use `model_copy(update={"target_token_ids": []})` for effective strategy configuration, and close the client when any later bootstrap step raises.

- [ ] **Step 4: Write failing runtime lifecycle tests**

Assert runtime creates a named `market-rotation` task when the service exists, creates none in static mode, and calls rotator stop during graceful shutdown and failed startup cleanup.

- [ ] **Step 5: Run tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_runtime.py tests/test_bootstrap.py`

Expected: rotation task and cleanup assertions fail.

- [ ] **Step 6: Implement lifecycle ownership**

Add the optional service to `AppServices`. Start its task after WebSocket start, include it in the runtime task list, and make `shutdown_app()` close the rotator before stopping the WebSocket. Keep cancellation idempotent and preserve existing live cancel-all ordering.

- [ ] **Step 7: Verify GREEN**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_bootstrap.py tests/test_runtime.py tests/test_live_kill_switch.py`

Expected: all lifecycle tests pass.

### Task 5: Dashboard Discovery State and Editor Lock

**Files:**
- Modify: `dashboard/models.py`
- Modify: `dashboard/read_model.py`
- Modify: `dashboard/controller.py`
- Modify: `dashboard/templates/index.html`
- Modify: `dashboard/static/dashboard.js`
- Modify: `dashboard/static/dashboard.css`
- Test: `tests/test_dashboard_read_model.py`
- Test: `tests/test_dashboard_controller.py`
- Test: `tests/test_dashboard_ui.py`

**Interfaces:**
- Consumes: `Btc15mMarketRotator.status()` and automatic-market configuration.
- Produces: `MarketRotationView`, `DashboardState.market_rotation`, stopped-only save conflict `automatic_market_owns_token_scope`, and visible active-market UI.

- [ ] **Step 1: Write failing secret-free read-model tests**

Build a live runtime with a healthy fake rotator and assert the JSON includes state, slug, UTC window, and Up/Down token IDs but no credential sentinel. Build stopped automatic mode and assert it reports `starting` without fabricating a market.

- [ ] **Step 2: Run tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_dashboard_read_model.py`

Expected: `market_rotation` is missing.

- [ ] **Step 3: Implement the typed dashboard view**

Map rotator status when services are live. When stopped, expose `enabled=True`, `state="starting"`, and no IDs. Launch readiness passes the subscription gate only when the live rotator is healthy with exactly two unique tokens.

- [ ] **Step 4: Write failing editor and UI tests**

Assert controller save raises `automatic_market_owns_token_scope`; the page contains an Active BTC 15m Market region; JavaScript renders `state.market_rotation` with `textContent`; and the manual editor is disabled from the initial state payload and every poll.

- [ ] **Step 5: Run tests and verify RED**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_dashboard_controller.py tests/test_dashboard_ui.py`

Expected: editor and UI assertions fail.

- [ ] **Step 6: Implement editor lock and market card**

Add market slug/title/window, health reason, and labeled Up/Down IDs. Use `textContent` and `replaceChildren`, never `innerHTML`. Explain that IDs rotate every 15 minutes and that credentials are not used for dry run.

- [ ] **Step 7: Verify GREEN**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/test_dashboard_read_model.py tests/test_dashboard_controller.py tests/test_dashboard_api.py tests/test_dashboard_ui.py`

Expected: all dashboard tests pass.

### Task 6: Documentation, Complete Verification, and Real Dry Run

**Files:**
- Modify: `README.md`
- Modify: `docs/live-runbook.md`
- Test: complete repository suite.

**Interfaces:**
- Consumes: completed automatic discovery workflow.
- Produces: exact operator instructions and fresh verification evidence.

- [ ] **Step 1: Document automatic rotation**

Explain that the checked-in dry-run profile discovers both BTC 15-minute outcomes, rotates without credentials, disables manual token editing, and fails closed. Document the safe static-mode fallback and reiterate that exposed credentials must be revoked rather than reused.

- [ ] **Step 2: Run static and packaging checks**

Run:

```bash
git diff --check
PYTHONPYCACHEPREFIX=/private/tmp/polymarket-btc-rotation-pycache .venv/bin/python -m compileall -q app clients config dashboard risk state strategies tests
```

Build a wheel from a temporary source copy and confirm it contains dashboard assets plus the new client modules.

- [ ] **Step 3: Run the complete suite with deprecations as errors**

Run: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider -W error::DeprecationWarning`

Expected: zero failures and zero deprecation warnings.

- [ ] **Step 4: Request independent code review**

Review discovery validation, retry timing, task cleanup, WebSocket replacement races, dashboard secret exposure, and live-start reachability. Fix every Critical or Important finding through a new failing regression test.

- [ ] **Step 5: Perform a public Gamma smoke check**

Fetch the computed active slug with the production discovery client. Confirm the title, window, outcomes, and IDs without printing credentials or remote response bodies.

- [ ] **Step 6: Start and verify the dashboard dry run**

Start `python -m dashboard.main` on loopback. From the dashboard, start dry run and verify:

- runtime is `RUNNING`;
- kill switch remains `INACTIVE` beyond `heartbeat_timeout_seconds`;
- market-data heartbeat is fresh;
- active market slug/window and both outcomes are visible;
- open order and position counts remain observable;
- browser console has no warnings or errors.

- [ ] **Step 7: Leave the safe operator state**

If verification succeeds, leave the dashboard server running with the dry-run bot running as explicitly requested. If discovery, heartbeat, or rotation is unhealthy, gracefully stop the bot, leave the dashboard available, and report the exact safe reason.

- [ ] **Step 8: Report repository state**

Report the feature branch, test count, live smoke result, current market window, and any remaining operator action. Do not stage or commit pre-existing user changes unless separately authorized.
