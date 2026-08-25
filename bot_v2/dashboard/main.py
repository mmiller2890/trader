"""Command-line entrypoint for the local operator dashboard."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import uvicorn

from app.process_services import build_process_reliability_services
from app.runtime import BotRuntime
from config.loader import load_config
from dashboard.app import create_app
from dashboard.controller import DashboardController


def validate_host(host: str) -> str:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("dashboard host must be a loopback address")
    return host


def browser_origin(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run the local bot operator dashboard")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", default=8000, type=int)
    result.add_argument("--config-dir", type=Path, default=Path("config"))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    host = validate_host(args.host)
    data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
    config = load_config(args.config_dir)
    process_services = build_process_reliability_services(
        config=config,
        data_dir=data_dir,
    )
    controller = DashboardController(
        runtime=BotRuntime(process_services=process_services),
        config_dir=args.config_dir,
        data_dir=data_dir,
        process_services=process_services,
    )
    app = create_app(
        controller=controller,
        process_services=process_services,
        trusted_origins={
            browser_origin(host, args.port),
            f"http://localhost:{args.port}",
        },
    )
    print(f"Operator dashboard: {browser_origin(host, args.port)}")
    uvicorn.run(app, host=host, port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
