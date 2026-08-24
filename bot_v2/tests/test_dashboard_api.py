from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.runtime import ControlResult, RuntimePhase, RuntimeStatus
from config.schema import AppConfig, Mode
from dashboard.app import create_app
from dashboard.config_editor import EditableConfig
from dashboard.models import DashboardState, EventTail, PreflightView


class ApiController:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.last_preflight = PreflightView(
            ok=False, status="not_run", reason="preflight_not_run"
        )

    async def state(self) -> DashboardState:
        return DashboardState(
            source="historical",
            runtime=RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.DRY_RUN),
            mode=Mode.DRY_RUN,
            kill_switch=False,
            websocket_connected=False,
            credentials={
                "private_key_configured": False,
                "l2_credentials_configured": False,
                "funder_configured": False,
                "rpc_configured": False,
            },
            subscribed_token_ids=[],
            target_token_ids=[],
        )

    def events(self, *, limit: int) -> EventTail:
        self.calls.append(("events", limit))
        return EventTail()

    def get_config(self) -> EditableConfig:
        return EditableConfig()

    def save_config(self, payload: EditableConfig) -> EditableConfig:
        self.calls.append(("save", payload))
        return payload

    async def start(self, confirmation: str | None = None) -> RuntimeStatus:
        self.calls.append(("start", confirmation))
        return RuntimeStatus(phase=RuntimePhase.RUNNING, mode=Mode.DRY_RUN)

    def set_mode(self, mode: Mode, confirmation: str | None = None) -> Mode:
        self.calls.append(("mode", mode, confirmation))
        return mode

    async def stop(self) -> RuntimeStatus:
        self.calls.append("stop")
        return RuntimeStatus(phase=RuntimePhase.STOPPED, mode=Mode.DRY_RUN)

    async def halt(self, confirmation: str) -> RuntimeStatus:
        self.calls.append(("halt", confirmation))
        return RuntimeStatus(phase=RuntimePhase.HALTED, mode=Mode.DRY_RUN)

    async def cancel_all(self, confirmation: str) -> ControlResult:
        self.calls.append(("cancel_all", confirmation))
        return ControlResult(ok=True, action="cancel_all", reason="orders_cancelled")

    async def run_preflight(self) -> PreflightView:
        self.calls.append("preflight")
        return PreflightView(ok=False, status="failed", reason="credentials_incomplete")


@pytest.fixture
def api() -> tuple[object, ApiController]:
    controller = ApiController()
    return (
        create_app(
            controller=controller,
            operator_token="test-token",
            trusted_origins={"http://127.0.0.1:8000"},
        ),
        controller,
    )


def mutation_headers() -> dict[str, str]:
    return {
        "Origin": "http://127.0.0.1:8000",
        "X-Operator-Token": "test-token",
    }


@pytest.mark.asyncio
async def test_read_routes_are_available_without_operator_token(api: tuple[object, ApiController]) -> None:
    app, _ = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/")).status_code == 200
        state = await client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["runtime"]["phase"] == "stopped"
        assert (await client.get("/api/config")).status_code == 200
        assert (await client.get("/api/events?limit=25")).status_code == 200


@pytest.mark.asyncio
async def test_mutation_requires_trusted_origin_and_operator_token(api: tuple[object, ApiController]) -> None:
    app, controller = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/control/start")).status_code == 403
        assert (
            await client.post(
                "/api/control/start",
                headers={"Origin": "http://evil.example", "X-Operator-Token": "test-token"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/control/start",
                headers={"Origin": "http://127.0.0.1:8000", "X-Operator-Token": "wrong"},
            )
        ).status_code == 403
        assert (
            await client.post("/api/control/start", headers=mutation_headers())
        ).status_code == 200
    assert controller.calls == [("start", None)]


@pytest.mark.asyncio
async def test_failed_start_is_reported_as_conflict(api: tuple[object, ApiController]) -> None:
    app, controller = api

    async def failed_start(confirmation: str | None = None) -> RuntimeStatus:
        controller.calls.append(("start", confirmation))
        raise RuntimeError("live_start_disabled_pending_review")

    controller.start = failed_start
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/control/start", headers=mutation_headers()
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "live_start_disabled_pending_review"}


@pytest.mark.asyncio
async def test_mode_and_live_start_routes_forward_confirmations(
    api: tuple[object, ApiController],
) -> None:
    app, controller = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mode_response = await client.put(
            "/api/mode",
            headers=mutation_headers(),
            json={"mode": "live", "confirmation": "ENABLE LIVE"},
        )
        start_response = await client.post(
            "/api/control/start",
            headers=mutation_headers(),
            json={"confirmation": "START LIVE"},
        )

    assert mode_response.status_code == 200
    assert start_response.status_code == 200
    assert controller.calls == [
        ("mode", Mode.LIVE, "ENABLE LIVE"),
        ("start", "START LIVE"),
    ]


@pytest.mark.asyncio
async def test_mode_route_rejects_non_operator_runtime_modes(
    api: tuple[object, ApiController],
) -> None:
    app, controller = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/mode",
            headers=mutation_headers(),
            json={"mode": "backtest"},
        )

    assert response.status_code == 422
    assert controller.calls == []


@pytest.mark.asyncio
async def test_app_shutdown_stops_owned_runtime(api: tuple[object, ApiController]) -> None:
    app, controller = api

    async with app.router.lifespan_context(app):
        pass

    assert controller.calls == ["stop"]


@pytest.mark.asyncio
async def test_confirmed_controls_and_preflight_reach_controller(api: tuple[object, ApiController]) -> None:
    app, controller = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(
                "/api/control/halt",
                headers=mutation_headers(),
                json={"confirmation": "HALT"},
            )
        ).status_code == 200
        assert (
            await client.post(
                "/api/control/cancel-all",
                headers=mutation_headers(),
                json={"confirmation": "CANCEL ALL"},
            )
        ).status_code == 200
        assert (
            await client.post("/api/preflight", headers=mutation_headers())
        ).status_code == 200
    assert controller.calls == [
        ("halt", "HALT"),
        ("cancel_all", "CANCEL ALL"),
        "preflight",
    ]


@pytest.mark.asyncio
async def test_config_route_rejects_extra_safety_fields(api: tuple[object, ApiController]) -> None:
    app, controller = api
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/config",
            headers=mutation_headers(),
            json={
                "subscribed_token_ids": ["123"],
                "target_token_ids": [],
                "mode": "live",
            },
        )
    assert response.status_code == 422
    assert controller.calls == []


def test_dashboard_main_rejects_non_loopback_host() -> None:
    from dashboard.main import browser_origin, validate_host

    assert validate_host("127.0.0.1") == "127.0.0.1"
    assert browser_origin("::1", 8000) == "http://[::1]:8000"
    with pytest.raises(ValueError, match="loopback"):
        validate_host("0.0.0.0")
