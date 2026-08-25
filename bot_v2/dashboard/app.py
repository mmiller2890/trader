"""FastAPI application for the loopback-only operator console."""

from __future__ import annotations

import hmac
import secrets
from contextlib import asynccontextmanager
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from config.schema import Mode
from dashboard.config_editor import EditableConfig
from dashboard.controller import (
    ConfirmationError,
    DashboardController,
    PreflightBusyError,
)
from datetime import UTC, datetime
from models.operations import OperationalState
from persistence.health import HealthAnswer, HealthSnapshotStore


class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation: str | None = None


class ModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry_run", "live"]
    confirmation: str | None = None


class ClearHaltRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: str
    confirmation: str


def create_app(
    *,
    controller: DashboardController,
    operator_token: str | None = None,
    trusted_origins: set[str] | None = None,
    health_store: HealthSnapshotStore | None = None,
) -> FastAPI:
    """Create one dashboard app with a per-process mutation token."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        try:
            yield
        finally:
            await controller.stop()

    token = operator_token or secrets.token_urlsafe(32)
    origins = trusted_origins or {
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://[::1]:8000",
    }
    app = FastAPI(
        title="Polymarket Bot Operator Console",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.controller = controller
    app.state.operator_token = token
    dashboard_root = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=dashboard_root / "templates")
    app.mount(
        "/static",
        StaticFiles(directory=dashboard_root / "static"),
        name="static",
    )

    async def require_operator(
        request: Request,
        x_operator_token: str | None = Header(default=None),
    ) -> None:
        origin = request.headers.get("origin")
        if origin not in origins:
            raise HTTPException(status_code=403, detail="untrusted_origin")
        if x_operator_token is None or not hmac.compare_digest(
            x_operator_token, token
        ):
            raise HTTPException(status_code=403, detail="invalid_operator_token")

    OperatorGuard = Depends(require_operator)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"operator_token": token},
        )

    @app.get("/api/state")
    async def state():  # type: ignore[no-untyped-def]
        return await controller.state()

    def _fallback_answer_sync(kind: str, phase: OperationalState) -> HealthAnswer:
        ok_by_kind = {
            "live": True,
            "ready": False,
            "trading": phase == OperationalState.RUNNING,
        }
        reason_by_kind = {
            "live": "process_serving",
            "ready": "health_snapshot_unavailable",
            "trading": "trading_state_unknown",
        }
        return HealthAnswer(
            ok=ok_by_kind[kind],
            state=phase,
            reason=reason_by_kind[kind],
            generated_at=datetime.now(tz=UTC),
        )

    async def _health_answer(kind: str, ok_field: str) -> HealthAnswer:
        if health_store is not None:
            snapshot = await health_store.load()
        else:
            snapshot = None
        if snapshot is None:
            try:
                current = await controller.state()
                phase = OperationalState(current.runtime.phase.value)
            except Exception:
                phase = OperationalState.STOPPED
            return _fallback_answer_sync(kind, phase)
        return HealthAnswer(
            ok=bool(getattr(snapshot, ok_field)),
            state=snapshot.state,
            reason=snapshot.reason or "healthy",
            generated_at=datetime.now(tz=UTC),
        )

    @app.get("/api/health/live")
    async def health_live():  # type: ignore[no-untyped-def]
        return await _health_answer("live", "process_live")

    @app.get("/api/health/ready")
    async def health_ready():  # type: ignore[no-untyped-def]
        return await _health_answer("ready", "service_ready")

    @app.get("/api/health/trading")
    async def health_trading():  # type: ignore[no-untyped-def]
        return await _health_answer("trading", "trading_ready")

    @app.get("/api/events")
    async def events(limit: int = Query(default=100, ge=1, le=100)):  # type: ignore[no-untyped-def]
        return controller.events(limit=limit)

    @app.get("/api/config")
    async def get_config():  # type: ignore[no-untyped-def]
        return controller.get_config()

    async def call_control(operation: Callable[[], Awaitable[object]]):
        try:
            return await operation()
        except ConfirmationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PreflightBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/control/start", dependencies=[OperatorGuard])
    async def start(payload: StartRequest | None = None):  # type: ignore[no-untyped-def]
        confirmation = payload.confirmation if payload is not None else None
        return await call_control(lambda: controller.start(confirmation))

    @app.put("/api/mode", dependencies=[OperatorGuard])
    async def set_mode(payload: ModeRequest):  # type: ignore[no-untyped-def]
        async def operation():  # type: ignore[no-untyped-def]
            return controller.set_mode(Mode(payload.mode), payload.confirmation)

        return await call_control(operation)

    @app.post("/api/control/stop", dependencies=[OperatorGuard])
    async def stop():  # type: ignore[no-untyped-def]
        return await call_control(controller.stop)

    @app.post("/api/control/halt", dependencies=[OperatorGuard])
    async def halt(payload: ConfirmationRequest):  # type: ignore[no-untyped-def]
        return await call_control(lambda: controller.halt(payload.confirmation))

    @app.post("/api/control/clear-halt", dependencies=[OperatorGuard])
    async def clear_halt(payload: ClearHaltRequest):  # type: ignore[no-untyped-def]
        return await controller.clear_halt(payload.incident_id, payload.confirmation)

    @app.post("/api/control/cancel-all", dependencies=[OperatorGuard])
    async def cancel_all(payload: ConfirmationRequest):  # type: ignore[no-untyped-def]
        return await call_control(lambda: controller.cancel_all(payload.confirmation))

    @app.post("/api/preflight", dependencies=[OperatorGuard])
    async def preflight():  # type: ignore[no-untyped-def]
        return await call_control(controller.run_preflight)

    @app.put("/api/config", dependencies=[OperatorGuard])
    async def save_config(payload: EditableConfig):  # type: ignore[no-untyped-def]
        try:
            return controller.save_config(payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return app
