# Dashboard Live Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make preflight automatic-market-aware and expose a guarded complete dry/live runtime lifecycle through the local dashboard.

**Architecture:** Resolve the effective account/funder centrally, feed discovered token IDs into one preflight implementation, persist the three coupled live flags atomically in the operator overlay, and make the dashboard controller enforce fresh-preflight and exact-confirmation gates before live actions. Runtime bootstrap repeats all safety checks independently.

**Tech Stack:** Python 3.11, Pydantic 2, FastAPI, PyYAML, py-clob-client-v2, pytest/pytest-asyncio, plain HTML/CSS/JavaScript.

**Spec:** `docs/superpowers/specs/2026-08-24-dashboard-live-control-design.md`

## Global Constraints

- Never return or log credential values or the derived account address.
- Keep loopback, same-origin, and operator-token protections.
- Live changes are stopped-only and atomically couple all three live flags.
- Require a fresh passed preflight plus exact confirmation for live activation/start.
- Runtime live startup repeats full preflight and fails closed.
- Do not bypass geoblock, authentication, collateral, allowance, discovery, or reconciliation failures.
- Keep `max_live_order_notional: 1` and `time_in_force: FOK`.

---

### Task 1: Effective EOA Funder

**Files:** Modify `clients/auth.py`, `clients/clob_client.py`, `config/bot.yaml`; test `tests/test_clob_client.py` and `tests/test_config.py`.

- [ ] Add failing tests proving signature type `0` derives the signer address and does not require a configured proxy, while types `1`–`3` still reject a missing funder.
- [ ] Run the focused tests and confirm the expected failures.
- [ ] Implement centralized effective-funder resolution and change the checked-in account type to `0`.
- [ ] Run focused tests and confirm they pass.

### Task 2: Automatic-Market-Aware Preflight

**Files:** Modify `scripts/live_preflight.py`, `app/bootstrap.py`; test `tests/test_live_preflight.py`, `tests/test_bootstrap.py`.

- [ ] Add failing tests proving discovered outcome tokens satisfy preflight and live bootstrap discovers before preflight.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Let preflight accept an explicit effective token scope; discover it in the CLI and before bootstrap preflight; close discovery resources on every branch.
- [ ] Run focused tests and confirm they pass.

### Task 3: Atomic Mode Overlay

**Files:** Modify `config/loader.py`, `dashboard/config_editor.py`; test `tests/test_dashboard_config.py`, `tests/test_config.py`.

- [ ] Add failing tests for exact dry/live flag bundles, preservation of token scope, stopped-only writes, and rejection of partial/extra fields.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Extend the strict overlay model/editor and loader allowlist with the three coupled mode fields.
- [ ] Run focused tests and confirm they pass.

### Task 4: Guarded Controller and API

**Files:** Modify `dashboard/models.py`, `dashboard/controller.py`, `dashboard/app.py`, `dashboard/read_model.py`; test `tests/test_dashboard_controller.py`, `tests/test_dashboard_api.py`, `tests/test_dashboard_read_model.py`.

- [ ] Add failing tests for five-minute freshness, invalidation, exact confirmations, mode switching, live-start refusal, and safe state booleans.
- [ ] Run focused tests and confirm the expected failures.
- [ ] Implement state-derived gating, `PUT /api/mode`, and mode-aware `POST /api/control/start`.
- [ ] Run focused tests and confirm they pass.

### Task 5: Operator UI

**Files:** Modify `dashboard/templates/index.html`, `dashboard/static/dashboard.js`, `dashboard/static/dashboard.css`; test `tests/test_dashboard_ui.py`.

- [ ] Add failing structural tests for Enable Live, Return to Dry Run, Start Live, and confirmation wiring.
- [ ] Run the focused UI test and confirm the expected failures.
- [ ] Implement accessible state-driven controls and messages without `innerHTML` or credential fields.
- [ ] Run focused UI/API tests and confirm they pass.

### Task 6: Verification and Runbook

**Files:** Modify `README.md`, `docs/live-runbook.md`.

- [ ] Run all focused tests, then the entire suite with warnings as errors.
- [ ] Run compileall and `git diff --check`.
- [ ] Restart the dashboard, run real read-only preflight, and verify the UI in the in-app browser.
- [ ] Record any unavoidable external blocker exactly; never convert it into a passing gate.
