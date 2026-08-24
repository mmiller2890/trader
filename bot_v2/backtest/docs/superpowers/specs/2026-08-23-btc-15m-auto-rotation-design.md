# Bitcoin 15-Minute Automatic Market Rotation Design

## Purpose

Replace manual, expiring token configuration for Polymarket's recurring Bitcoin Up/Down 15-minute markets with validated automatic discovery and safe runtime rotation. The bot must start a credential-free dry run against the current market, subscribe to both outcome books, rotate at each 15-minute boundary, and fail closed when discovery or market data cannot be trusted.

This feature does not enable live trading, accept credentials through the dashboard, choose a directional prediction, or weaken any existing pre-trade or runtime risk gate.

## Scope

The feature provides:

- calculation of the current UTC 15-minute Bitcoin market window;
- read-only discovery through Polymarket's public Gamma API;
- strict validation of the active event and its single binary market;
- explicit mapping of `Up` and `Down` to their CLOB token IDs;
- subscription to both outcome books;
- automatic rotation to a new pair of tokens at each boundary;
- a WebSocket reconnect when the subscription set changes;
- dashboard visibility for the current market, window, outcomes, and discovery status;
- safe startup failure and runtime degradation when discovery fails;
- deterministic unit and integration coverage without depending on the live API.

The feature does not provide:

- browser-based live enablement;
- private API authentication;
- automatic wallet, allowance, or order configuration;
- a guarantee of market availability, liquidity, signals, simulated fills, or live fills;
- automatic clearing of a kill switch after data becomes stale;
- support for assets or durations other than Bitcoin 15-minute markets in this release.

## Configuration

Add a typed market-discovery section under `market_data`:

```yaml
market_data:
  automatic_market:
    enabled: true
    asset: btc
    duration_minutes: 15
    gamma_api_url: https://gamma-api.polymarket.com
    slug_prefix: btc-updown-15m
    refresh_lead_seconds: 10
    request_timeout_seconds: 5
```

`asset` is restricted to `btc` and `duration_minutes` to `15` for this release. `refresh_lead_seconds` is bounded below the market duration. The checked-in configuration enables automatic discovery for the requested dry-run profile.

When automatic discovery is enabled:

- static `market_data.subscribed_token_ids` and `spike_strategy.target_token_ids` are ignored at runtime;
- the dashboard manual token editor is read-only and explains that discovery owns the active IDs;
- both discovered outcome tokens are eligible for the spike strategy;
- live-mode preflight still requires every existing live gate and does not treat discovery as authorization to trade.

When automatic discovery is disabled, the existing static configuration flow remains unchanged.

## Discovery Model

Create `clients/gamma_markets.py` with these public types:

- `MarketOutcome`: normalized outcome name and decimal CLOB token ID;
- `DiscoveredMarket`: event ID, market ID, condition ID, slug, title, UTC start/end timestamps, `Up` outcome, and `Down` outcome;
- `GammaMarketDiscoveryClient`: async public-data client with `discover_active(now=None)` and `close()`.

The active slug is derived as:

```text
window_start_epoch = floor(utc_epoch_seconds / 900) * 900
slug = "btc-updown-15m-{window_start_epoch}"
```

Discovery calls `GET /events/slug/{slug}` with an explicit timeout and no authentication. The response is accepted only when all of the following hold:

- the event slug exactly matches the requested slug;
- the event is active, not closed, and has exactly one market;
- the market is active, not closed, order-book enabled, and accepting orders;
- `eventStartTime <= now < endDate`;
- `outcomes` and `clobTokenIds` decode to lists of equal length;
- the normalized outcome names are exactly `Up` and `Down`, each appearing once;
- both token IDs are non-empty decimal strings and are different;
- the market and condition identifiers are non-empty.

The outcome-to-token mapping is positional only after validating both arrays. This prevents an accidental reversal of Up and Down IDs.

The client returns typed, redacted errors such as `market_not_found`, `market_not_active`, `market_window_mismatch`, and `invalid_outcome_tokens`. Raw response bodies are never copied into dashboard responses or logs.

## Rotation Coordinator

Create `clients/market_rotation.py` with `Btc15mMarketRotator`. It owns the current `DiscoveredMarket`, discovery health, and refresh task. Its public interface is:

```python
async def initialize() -> DiscoveredMarket
async def run(stop_event: asyncio.Event) -> None
async def stop() -> None
def status() -> MarketRotationStatus
```

`initialize()` is called during bootstrap before the WebSocket starts. Startup fails if no valid active market can be discovered. This prevents the bot from entering a nominal running state with an empty subscription and later tripping the heartbeat kill switch.

After startup, `run()` sleeps until `current.end_at - refresh_lead_seconds`, bounded to a short minimum delay to avoid a busy loop. It queries the next/current window until a valid market is available. When a different token pair is returned, it calls `WebSocketManager.replace_asset_ids()` and records the new market atomically.

The coordinator does not write discovered IDs to `config/operator.yaml`; the IDs are ephemeral runtime state. It emits structured events for discovery success, rotation success, and redacted discovery failure.

### Rotation failure behavior

- A transient failure before the boundary retains the current subscription and retries with bounded exponential backoff up to the boundary.
- A failure after the old market expires leaves the previous subscription visible as expired, marks rotation unhealthy, and continues bounded retries.
- The coordinator never fabricates a market, reuses an expired token pair as current, or clears a kill switch.
- Without fresh order-book snapshots, the existing heartbeat policy trips the kill switch.
- In live mode, the existing cancel-all behavior remains authoritative when a runtime halt occurs.

## WebSocket Subscription Replacement

Extend `WebSocketManager` with:

```python
async def replace_asset_ids(self, asset_ids: list[str]) -> bool
```

The method validates a non-empty, unique list of decimal strings. Under an async lock it compares the requested IDs with the active set. An unchanged set returns `False` and does nothing. A changed set updates the next subscription atomically, closes the current socket, and returns `True`.

Closing the current socket intentionally ends the existing consume loop. The manager's existing reconnect loop then establishes a fresh connection and sends one full market subscription containing the new IDs. This design avoids dependence on in-place subscribe/unsubscribe protocol semantics and ensures reconnect recovery always uses the latest subscription.

Normal shutdown wins over rotation: if the manager is stopping, replacement is rejected with a typed runtime error and cannot cause a reconnect.

## Strategy Behavior

The existing spike strategy treats an empty target list as "accept every subscribed token." Automatic discovery therefore supplies both Up and Down tokens to the WebSocket while presenting an empty effective target filter to the strategy.

Static configured target IDs are not mutated. Bootstrap constructs an effective runtime strategy configuration with `target_token_ids=[]` only when automatic discovery is enabled. This keeps static-mode behavior unchanged and avoids stale target IDs silently filtering all new windows.

History and cooldown state remain keyed by `(market_id, token_id)`, so a new market starts with clean history naturally. No state is transferred across token rotations.

## Runtime Integration

`AppServices` gains an optional `market_rotator`. Bootstrap behavior becomes:

1. load and validate configuration;
2. construct the public Gamma discovery client when automatic discovery is enabled;
3. discover and validate the current market;
4. construct the WebSocket manager with both discovered token IDs;
5. construct the strategy with the effective automatic target behavior;
6. return services with the initialized rotator.

`BotRuntime.start()` starts the WebSocket and then creates both housekeeping and rotation tasks. `shutdown_app()` stops the rotator/client before or alongside the WebSocket and persists state through the existing shutdown path. A startup exception after discovery closes any created HTTP client through the existing startup cleanup path.

The rotator task is supervised. An unexpected task exception is recorded as a redacted runtime control error and cannot silently disable future rotations. The existing risk layer remains responsible for halting on stale data.

## Dashboard Changes

Extend the secret-free dashboard state with:

- whether automatic BTC 15-minute discovery is enabled;
- discovery state: `disabled`, `starting`, `healthy`, `degraded`, or `failed`;
- active market slug and title;
- UTC window start and end;
- Up and Down token IDs;
- last successful discovery timestamp;
- a safe status reason.

Token IDs and market metadata are public, not credentials. Secret readiness remains boolean-only.

The Market Scope panel shows the active rotating market and both outcome IDs. When automatic discovery is enabled, both manual token text areas and the save button are disabled with explanatory text. Existing stopped-only manual editing remains available when automatic discovery is disabled.

Launch readiness replaces the static `no_subscribed_token_ids` blocker with automatic discovery health. Live start remains permanently blocked in this dashboard release.

## Security and Safety Properties

- Gamma discovery uses a fixed configured HTTPS base URL and a generated slug; it never follows a URL supplied by an API response.
- No credentials are required, accepted, logged, persisted, or passed to the discovery request.
- HTTP timeouts and bounded response parsing prevent an indefinite startup wait.
- Response validation rejects unknown outcomes, reordered/missing array entries, non-decimal tokens, closed markets, and mismatched windows.
- Automatic rotation cannot change live flags, sizing, risk limits, wallet settings, or credentials.
- Dashboard live start remains disabled.
- Discovery errors shown to the operator use known safe reason codes, never raw remote bodies.
- The kill switch is never cleared automatically.

## Error Handling

Expected discovery failures use a `MarketDiscoveryError` containing a safe reason code. Network-library exception messages are logged only by exception class plus the safe reason. HTTP status codes may be included; bodies may not.

Startup discovery failure returns a failed runtime state instead of starting an unsubscribed bot. Runtime rotation failure remains visible in dashboard state and events while bounded retries continue. If data becomes stale, risk trips the kill switch. Operator stop remains available in every degraded state.

## Testing Strategy

All production behavior follows red-green-refactor cycles.

Unit tests cover:

- UTC epoch flooring at exact and near-boundary timestamps;
- current slug construction;
- valid Up/Down positional token mapping;
- rejection of closed, expired, mismatched, malformed, non-decimal, duplicated, or incomplete market data;
- safe errors that do not contain response bodies;
- unchanged and changed WebSocket subscription replacement;
- replacement during shutdown;
- reconnect sending only the latest IDs;
- initial discovery failure blocking startup;
- boundary rotation replacing both IDs;
- retry behavior before and after expiry;
- strategy accepting both automatically discovered tokens;
- dashboard serialization and manual-editor disabling;
- absence of credentials in every new model and error response.

Integration tests use injected clocks and fake HTTP/WebSocket boundaries. They do not depend on the real Gamma API.

Final verification includes:

- the complete test suite with deprecation warnings treated as errors;
- package build verification;
- one read-only live Gamma discovery check;
- a browser-started dry run that remains running with an inactive kill switch and fresh market-data heartbeat beyond the startup timeout;
- observation of the active market metadata in the dashboard;
- graceful stop after verification.

## Operational Behavior

The operator continues to run:

```bash
cd /Users/ghost/Projects/trader/bot_v2
source .venv/bin/activate
python -m dashboard.main
```

With automatic discovery enabled, pressing **Start dry run** discovers the current Bitcoin 15-minute market before reporting `RUNNING`. The dashboard shows both Up and Down IDs and rotates them automatically. The operator does not paste credentials or manually update IDs every 15 minutes.

## Acceptance Criteria

The feature is complete when:

- dry-run startup discovers a valid current Bitcoin 15-minute market without credentials;
- both Up and Down books produce normalized snapshots and a fresh market-data heartbeat;
- the bot remains running with the kill switch inactive beyond the heartbeat startup grace period;
- a simulated 15-minute boundary rotates to a new validated token pair via WebSocket reconnect;
- stale or malformed discovery data cannot become an active subscription;
- runtime discovery failure is visible and fails closed;
- the dashboard shows the active market and disables conflicting manual token editing;
- live start remains unavailable from the dashboard;
- all focused and repository tests pass with deprecation warnings treated as errors.
