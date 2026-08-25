"""
Verify Telegram alerting and discover your chat id.

Live start is gated on a delivered Telegram alert, so this has to work before
the bot will trade. Run it after putting TELEGRAM_BOT_TOKEN in .env:

    python3 -m scripts.telegram_setup

It checks the token, lists the chats that have messaged your bot, and prints
the chat id to paste into .env. With --send it delivers a test message.

The token is read from the environment and never printed, logged, or written
anywhere by this script.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import httpx

from config.loader import load_config

API = "https://api.telegram.org"


def _token(config: Any) -> str | None:
    secret = config.secrets.telegram_bot_token
    return secret.get_secret_value() if secret is not None else None


def _configured_chat_id(config: Any) -> str | None:
    secret = config.secrets.telegram_chat_id
    return secret.get_secret_value() if secret is not None else None


def _call(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"{API}/bot{token}/{method}"
    try:
        response = httpx.post(url, json=payload or {}, timeout=15.0)
    except Exception as exc:
        return {"ok": False, "description": f"transport failure: {type(exc).__name__}"}
    try:
        return response.json()
    except Exception:
        return {"ok": False, "description": f"non-JSON reply ({response.status_code})"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument(
        "--send",
        action="store_true",
        help="send a test message to the configured chat id",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config_dir)
    token = _token(config)
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env.")
        print()
        print("In Telegram, message @BotFather, send /newbot, follow the")
        print("prompts, then copy the token it gives you into .env as:")
        print("    TELEGRAM_BOT_TOKEN=<token>")
        return 2

    identity = _call(token, "getMe")
    if not identity.get("ok"):
        print(f"Token rejected by Telegram: {identity.get('description')}")
        print("Check for stray whitespace or a truncated paste in .env.")
        return 2
    bot = identity["result"]
    print(f"Bot OK: @{bot.get('username')} ({bot.get('first_name')})")

    if not config.notifications.telegram_enabled:
        print()
        print("NOTE: notifications.telegram_enabled is false, so the bot will")
        print("not send alerts yet. Turn it on with the dashboard toggle, or")
        print("in config/bot.yaml, then restart.")

    chat_id = _configured_chat_id(config)
    if chat_id:
        print(f"Configured chat id: {chat_id}")
    else:
        print()
        print("TELEGRAM_CHAT_ID is not set. Open Telegram, send your bot any")
        print(f"message (@{bot.get('username')}), then re-run this command.")

    updates = _call(token, "getUpdates")
    seen: dict[str, str] = {}
    if updates.get("ok"):
        for update in updates.get("result", []):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is None:
                continue
            label = (
                chat.get("title")
                or " ".join(
                    part
                    for part in (chat.get("first_name"), chat.get("last_name"))
                    if part
                )
                or chat.get("username")
                or chat.get("type", "chat")
            )
            seen[str(chat["id"])] = str(label)

    if seen:
        print()
        print("Chats that have messaged your bot:")
        for found_id, label in seen.items():
            marker = "  <-- already configured" if found_id == chat_id else ""
            print(f"    TELEGRAM_CHAT_ID={found_id}    ({label}){marker}")
    elif not chat_id:
        print()
        print("No messages seen yet. Telegram only reveals a chat id after you")
        print("message the bot first, so send it anything and re-run.")

    if args.send:
        if not chat_id:
            print()
            print("Cannot send: set TELEGRAM_CHAT_ID in .env first.")
            return 2
        result = _call(
            token,
            "sendMessage",
            {"chat_id": chat_id, "text": "polymarket-bot: test alert"},
        )
        if result.get("ok"):
            print()
            print("Test message sent. Check your Telegram.")
        else:
            print()
            print(f"Send failed: {result.get('description')}")
            return 2

    ready = bool(chat_id) and config.notifications.telegram_enabled
    print()
    print(f"Ready for live start gate: {'yes' if ready else 'no'}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
