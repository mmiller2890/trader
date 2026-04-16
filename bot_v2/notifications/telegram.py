"""Telegram notifier boundary."""

from __future__ import annotations

import logging

import httpx

from config.schema import AppConfig
from models.events import BotEvent, EventType

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Safe no-op telegram notifier unless configured."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._enabled = (
            config.notifications.telegram_enabled
            and config.secrets.telegram_bot_token is not None
            and config.secrets.telegram_chat_id is not None
        )

    async def notify_event(self, event: BotEvent) -> None:
        """Send alert for selected event types when configured."""

        if not self._enabled:
            return
        if event.event_type not in {
            EventType.BOT_STARTED,
            EventType.KILL_SWITCH_TRIPPED,
            EventType.REPEATED_FAILURES,
            EventType.ORDER_RESULT,
        }:
            return
        if event.event_type == EventType.ORDER_RESULT and "large_order_simulated" not in (event.reason or ""):
            return

        token = self._config.secrets.telegram_bot_token
        chat_id = self._config.secrets.telegram_chat_id
        assert token is not None
        assert chat_id is not None

        text = self._format_message(event)
        url = f"https://api.telegram.org/bot{token.get_secret_value()}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}

        retries = self._config.notifications.telegram_send_retries + 1
        async with httpx.AsyncClient(timeout=10.0) as client:
            last_error: Exception | None = None
            for _ in range(retries):
                try:
                    response = await client.post(url, json=payload)
                    response.raise_for_status()
                    return
                except Exception as exc:
                    last_error = exc
            logger.warning(
                "telegram notification failed",
                extra={
                    "component": "telegram",
                    "event_type": "notification_failed",
                    "reason": str(last_error) if last_error else "unknown",
                },
            )

    def _format_message(self, event: BotEvent) -> str:
        parts = [
            f"event={event.event_type.value}",
            f"mode={event.mode}",
            f"component={event.component}",
            f"message={event.message}",
        ]
        if event.market_id:
            parts.append(f"market_id={event.market_id}")
        if event.token_id:
            parts.append(f"token_id={event.token_id}")
        if event.strategy_name:
            parts.append(f"strategy={event.strategy_name}")
        if event.client_order_id:
            parts.append(f"client_order_id={event.client_order_id}")
        if event.reason:
            parts.append(f"reason={event.reason}")
        return "\n".join(parts)
