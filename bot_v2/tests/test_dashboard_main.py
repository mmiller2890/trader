from __future__ import annotations

from pathlib import Path

from config.schema import AppConfig
import dashboard.main as dashboard_main


def test_dashboard_main_injects_one_process_reliability_graph(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = AppConfig()
    process_services = object()
    runtime = object()
    controller = object()
    app = object()
    calls: list[object] = []

    def build_process_services(*, config: AppConfig, data_dir: Path) -> object:
        calls.append(("build", config, data_dir))
        return process_services

    def build_runtime(*, process_services: object) -> object:
        calls.append(("runtime", process_services))
        return runtime

    def build_controller(**kwargs: object) -> object:
        calls.append(("controller", kwargs))
        return controller

    def build_app(**kwargs: object) -> object:
        calls.append(("app", kwargs))
        return app

    monkeypatch.setattr(dashboard_main, "load_config", lambda _: config, raising=False)
    monkeypatch.setattr(
        dashboard_main,
        "build_process_reliability_services",
        build_process_services,
        raising=False,
    )
    monkeypatch.setattr(dashboard_main, "BotRuntime", build_runtime)
    monkeypatch.setattr(dashboard_main, "DashboardController", build_controller)
    monkeypatch.setattr(dashboard_main, "create_app", build_app)
    monkeypatch.setattr(
        dashboard_main.uvicorn,
        "run",
        lambda built_app, **kwargs: calls.append(("uvicorn", built_app, kwargs)),
    )
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))

    assert dashboard_main.main(["--config-dir", str(tmp_path / "config")]) == 0

    assert calls[0] == ("build", config, tmp_path / "data")
    assert calls[1] == ("runtime", process_services)
    assert calls[2] == (
        "controller",
        {
            "runtime": runtime,
            "config_dir": tmp_path / "config",
            "data_dir": tmp_path / "data",
            "process_services": process_services,
        },
    )
    assert calls[3][0] == "app"
    assert calls[3][1]["controller"] is controller
    assert calls[3][1]["process_services"] is process_services
    assert calls[4][0:2] == ("uvicorn", app)
