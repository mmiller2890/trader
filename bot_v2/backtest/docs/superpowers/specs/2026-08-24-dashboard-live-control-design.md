# Dashboard Live Control Design

**Status:** Approved by the operator's request to finish the dashboard and make live mode operable from it.

## Goal

Allow the loopback-only dashboard to run the complete bot lifecycle—dry run, read-only preflight, guarded live-mode activation, live start, graceful stop, emergency halt, cancel-all, and return to dry run—without exposing credentials to the browser or weakening runtime safety gates.

## Account Resolution

`exchange.signature_type` selects the account model. Signature type `0` is an EOA: the effective funder is derived from `PRIVATE_KEY`, and `POLYMARKET_PROXY_ADDRESS` is not required. Signature types `1`, `2`, and `3` are contract/proxy account modes and continue to require an explicit `POLYMARKET_PROXY_ADDRESS`. Invalid keys or missing required funders fail before SDK construction.

The checked-in profile uses signature type `0` because the configured credentials have been verified through read-only authenticated CLOB calls as the derived EOA account. The dashboard returns only readiness booleans and never returns the derived address or any credential value.

## Preflight

Read-only preflight runs while the checked-in profile is still dry-run. When automatic BTC 15-minute market discovery is enabled, preflight first discovers the current validated market and uses its two outcome token IDs for the subscription gate. Both the standalone command and live startup use this same behavior.

Preflight checks live-guard intent, complete credentials/effective funder, geoblock, CLOB health, authenticated open-order reads, Data API positions, collateral balance and allowance, automatic/current subscription scope, and reconciliation. Operator preflight may treat the three live flags as not yet required; live startup repeats preflight with those guards required.

Preflight output is redacted. A passed result is valid for five minutes in the current dashboard process. Any failed/new preflight replaces it; relevant configuration changes invalidate it.

## Dashboard Control Flow

While stopped, the operator can:

1. start dry run when the configured mode is `dry_run`;
2. run read-only preflight;
3. activate live configuration only after a fresh passing preflight and the exact phrase `ENABLE LIVE`;
4. start the live bot only with the exact phrase `START LIVE` and a fresh passing preflight;
5. return to dry-run configuration with one stopped-only action.

Live activation atomically writes only these coupled values to ignored `config/operator.yaml`:

```yaml
bot:
  mode: live
execution:
  allow_live_trading: true
  dry_run_force: false
```

Returning to dry run atomically writes `mode: dry_run`, `allow_live_trading: false`, and `dry_run_force: true`. Token scope remains preserved in the same overlay. The loader allowlists exactly these fields and rejects partial or contradictory live settings.

The start endpoint chooses the path from loaded configuration. Dry-run start does not accept a live confirmation. Live start requires `START LIVE`, calls `BotRuntime.start(..., allow_live=True)`, and runtime bootstrap independently repeats full preflight before opening market data or submitting any order.

## Safety Contract

- Dashboard remains loopback-only and every mutation remains same-origin/operator-token protected.
- Credential values and the derived wallet address never enter HTML or JSON.
- The bot must be stopped for mode changes.
- A stale, failed, or absent preflight blocks live activation and live start.
- Geoblock, authentication, balance, allowance, market discovery, or reconciliation failure cannot be overridden from the dashboard.
- Live activation does not place an order; orders can only originate from the configured strategy after a successful live start.
- Existing `HALT` and `CANCEL ALL` exact confirmations remain unchanged.
- The checked-in live order cap remains `1` and time-in-force remains `FOK`.

## API and UI

Add `PUT /api/mode` with `{mode, confirmation}` and extend `POST /api/control/start` to accept an optional confirmation. The state response adds `preflight_fresh`, `live_armed`, and `live_start_ready` booleans.

The control panel shows mode-specific Start Dry Run or Start Live controls, Run Preflight, Enable Live, Return to Dry Run, Graceful Stop, Emergency Halt, and Cancel All. Live activation/start use the existing accessible confirmation dialog. Buttons are disabled from authoritative state rather than optimistic browser state, and API errors remain visible.

## Verification

Regression tests cover EOA derivation, non-EOA funder requirements, automatic-market subscription preflight, bootstrap ordering, atomic mode persistence, preflight freshness/invalidation, controller confirmation/gating, protected API routes, secret-free serialization, and UI control wiring. Completion also requires the full suite with warnings as errors, a real read-only preflight, and browser verification of stopped, dry-run, preflight, mode-control, and live-blocker states.
