from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path("Dockerfile").read_text(encoding="utf-8")
COMPOSE = Path("docker-compose.example.yml").read_text(encoding="utf-8")
SERVICE = Path("deploy/polymarket-bot.service").read_text(encoding="utf-8")
ENV_EXAMPLE = Path("deploy/polymarket-bot.env.example").read_text(encoding="utf-8")


def test_dockerfile_has_liveness_healthcheck() -> None:
    assert "HEALTHCHECK" in DOCKERFILE
    assert "python -m scripts.healthcheck --kind liveness" in DOCKERFILE


def test_dockerfile_runs_operator_dashboard_process() -> None:
    assert "dashboard.main" in DOCKERFILE
    assert "--live" not in DOCKERFILE


def test_compose_restarts_and_persists_data() -> None:
    assert "restart: unless-stopped" in COMPOSE
    assert "BOT_DATA_DIR=/data" in COMPOSE
    assert "/data" in COMPOSE
    assert "env_file:" in COMPOSE


def test_compose_limits_log_size() -> None:
    assert "max-size" in COMPOSE
    assert "max-file" in COMPOSE


def test_systemd_unit_restarts_only_on_failure() -> None:
    assert "Restart=on-failure" in SERVICE
    assert "RestartSec=5" in SERVICE
    assert "StartLimitBurst" in SERVICE
    assert "StartLimitIntervalSec" in SERVICE
    assert "EnvironmentFile" in SERVICE
    assert "TimeoutStopSec" in SERVICE


def test_systemd_unit_runs_as_non_root_with_persistent_paths() -> None:
    user_line = next(
        line for line in SERVICE.splitlines() if line.startswith("User=")
    )
    assert user_line.split("=", 1)[1].strip() not in {"", "root"}
    assert "WorkingDirectory=" in SERVICE
    assert "data" in SERVICE


def test_deployment_commands_use_lease_auto_resume() -> None:
    for text in (DOCKERFILE, COMPOSE, SERVICE):
        assert "-m app.main --live" not in text
        assert "--resume-live as fresh authorization" not in text


def test_no_real_secrets_in_deployment_files() -> None:
    hex_pattern = re.compile(r"0x[0-9a-fA-F]{64}")
    for name, text in (
        ("Dockerfile", DOCKERFILE),
        ("compose", COMPOSE),
        ("service", SERVICE),
        ("env example", ENV_EXAMPLE),
    ):
        assert not hex_pattern.search(text), f"private key material in {name}"
        for marker in ("sk-", "BEGIN PRIVATE KEY"):
            assert marker not in text, f"secret-looking value in {name}"


def test_env_example_documents_variables_without_values() -> None:
    required = [
        "POLYMARKET_PRIVATE_KEY",
        "CLOB_API_KEY",
        "CLOB_SECRET",
        "CLOB_PASSPHRASE",
        "POLYMARKET_PROXY_ADDRESS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    for variable in required:
        pattern = re.compile(rf"^#\s*{variable}=|^# {variable}=", re.MULTILINE)
        assert pattern.search(ENV_EXAMPLE), f"{variable} undocumented"
