from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.app import create_app


@pytest.mark.asyncio
async def test_dashboard_page_has_operator_regions_and_assets() -> None:
    app = create_app(controller=object(), operator_token="ui-test-token")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    page = response.text
    for region in (
        'id="safety-rail"',
        'id="runtime-controls"',
        'id="health-panel"',
        'id="market-rotation-panel"',
        'id="portfolio-panel"',
        'id="orders-panel"',
        'id="positions-panel"',
        'id="config-panel"',
        'id="events-panel"',
        'id="readiness-panel"',
    ):
        assert region in page
    for control in (
        'id="start-button"',
        'id="enable-live-button"',
        'id="dry-run-button"',
        'id="preflight-button"',
        'id="preflight-checks"',
    ):
        assert control in page
    assert "Enable live mode" in page
    assert "Return to dry run" in page
    assert 'href="/static/dashboard.css"' in page
    assert 'src="/static/dashboard.js"' in page
    assert 'data-operator-token="ui-test-token"' in page
    assert "never-return-me" not in page


@pytest.mark.asyncio
async def test_dashboard_static_assets_are_served() -> None:
    app = create_app(controller=object(), operator_token="ui-test-token")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        css = await client.get("/static/dashboard.css")
        javascript = await client.get("/static/dashboard.js")

    assert css.status_code == 200
    assert "prefers-reduced-motion" in css.text
    assert javascript.status_code == 200
    assert "textContent" in javascript.text
    assert "state.preflight" in javascript.text
    assert "state.market_rotation" in javascript.text
    assert "state.preflight_fresh" in javascript.text
    assert "state.preflight.checks" in javascript.text
    assert "state.preflight_expires_at" in javascript.text
    assert "state.kill_switch_reason" in javascript.text
    assert 'const stopped = state.runtime.phase === "stopped" || state.runtime.phase === "failed"' in javascript.text
    assert "state.live_start_ready" in javascript.text
    assert '"ENABLE LIVE"' in javascript.text
    assert '"START LIVE"' in javascript.text
    assert '"/api/mode"' in javascript.text
    assert "automatic_market_owns_token_scope" in javascript.text
    assert "innerHTML" not in javascript.text


@pytest.mark.asyncio
async def test_dashboard_page_has_managed_position_columns() -> None:
    app = create_app(controller=object(), operator_token="ui-test-token")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    page = response.text
    for header in ("Return", "Held", "Deadline", "Exit"):
        assert header in page
    assert 'id="managed-positions-body"' in page
    assert 'id="closed-positions-body"' in page


@pytest.mark.asyncio
async def test_dashboard_javascript_renders_managed_positions_safely() -> None:
    app = create_app(controller=object(), operator_token="ui-test-token")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        javascript = await client.get("/static/dashboard.js")

    assert javascript.status_code == 200
    for phrase in (
        "Monitoring",
        "Exit pending",
        "Awaiting account confirmation",
        "Dust",
        "Closed",
    ):
        assert phrase in javascript.text
    assert "state.managed_positions" in javascript.text
    assert "state.closed_positions" in javascript.text
    assert "replaceChildren" in javascript.text
    assert "innerHTML" not in javascript.text
