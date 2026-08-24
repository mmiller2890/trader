from __future__ import annotations

from datetime import UTC, datetime

import pytest

from config.schema import AppConfig
from notifications.outbox import NotificationDeliveryError, TelegramTransport
from models.operations import IncidentSeverity, OutboxAlert


NOW = datetime(2026, 8, 24, tzinfo=UTC)


def alert() -> OutboxAlert:
    return OutboxAlert(
        alert_id="alert-12345678",
        incident_fingerprint="fp",
        severity=IncidentSeverity.INFO,
        text="test body",
        created_at=NOW,
        next_attempt_at=NOW,
    )


def unconfigured_config() -> AppConfig:
    return AppConfig()


@pytest.mark.asyncio
async def test_unconfigured_transport_raises_sanitized_error() -> None:
    transport = TelegramTransport(unconfigured_config())
    with pytest.raises(NotificationDeliveryError, match="telegram_not_configured"):
        await transport.send(alert())


@pytest.mark.asyncio
async def test_http_error_maps_to_sanitized_delivery_error() -> None:
    import httpx

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("token=secret-value in url")

    transport = TelegramTransport(
        AppConfig(
            secrets={
                "telegram_bot_token": "tok",
                "telegram_chat_id": "chat",
            }
        ),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(failing_handler)
        ),
    )
    with pytest.raises(NotificationDeliveryError) as captured:
        await transport.send(alert())
    assert "secret" not in str(captured.value)
