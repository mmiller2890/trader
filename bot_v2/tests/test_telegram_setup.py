"""Telegram setup helper behaviour."""
from __future__ import annotations
import pytest
from scripts import telegram_setup


def test_token_is_never_printed(capsys, monkeypatch, tmp_path):
    """A setup helper that echoes the token would leak it into scrollback."""
    secret = "1234567890:AAsecretsecretsecretsecretsecrets"
    monkeypatch.setattr(
        telegram_setup, "_call",
        lambda token, method, payload=None: (
            {"ok": True, "result": {"username": "b", "first_name": "B"}}
            if method == "getMe" else {"ok": True, "result": []}
        ),
    )
    cfg = type("C", (), {})()
    cfg.secrets = type("S", (), {
        "telegram_bot_token": type("T", (), {"get_secret_value": lambda self: secret})(),
        "telegram_chat_id": None,
    })()
    cfg.notifications = type("N", (), {"telegram_enabled": True})()
    monkeypatch.setattr(telegram_setup, "load_config", lambda _: cfg)
    telegram_setup.main([])
    assert secret not in capsys.readouterr().out


def test_missing_token_explains_botfather(capsys, monkeypatch):
    cfg = type("C", (), {})()
    cfg.secrets = type("S", (), {"telegram_bot_token": None, "telegram_chat_id": None})()
    cfg.notifications = type("N", (), {"telegram_enabled": False})()
    monkeypatch.setattr(telegram_setup, "load_config", lambda _: cfg)
    assert telegram_setup.main([]) == 2
    assert "@BotFather" in capsys.readouterr().out


def test_not_ready_when_chat_id_missing(monkeypatch):
    monkeypatch.setattr(
        telegram_setup, "_call",
        lambda token, method, payload=None: (
            {"ok": True, "result": {"username": "b", "first_name": "B"}}
            if method == "getMe" else {"ok": True, "result": []}
        ),
    )
    cfg = type("C", (), {})()
    cfg.secrets = type("S", (), {
        "telegram_bot_token": type("T", (), {"get_secret_value": lambda self: "t"})(),
        "telegram_chat_id": None,
    })()
    cfg.notifications = type("N", (), {"telegram_enabled": True})()
    monkeypatch.setattr(telegram_setup, "load_config", lambda _: cfg)
    assert telegram_setup.main([]) == 1


def test_discovers_chat_id_from_updates(capsys, monkeypatch):
    def fake(token, method, payload=None):
        if method == "getMe":
            return {"ok": True, "result": {"username": "b", "first_name": "B"}}
        return {"ok": True, "result": [
            {"message": {"chat": {"id": 987654321, "first_name": "Morgan"}}}
        ]}
    monkeypatch.setattr(telegram_setup, "_call", fake)
    cfg = type("C", (), {})()
    cfg.secrets = type("S", (), {
        "telegram_bot_token": type("T", (), {"get_secret_value": lambda self: "t"})(),
        "telegram_chat_id": None,
    })()
    cfg.notifications = type("N", (), {"telegram_enabled": True})()
    monkeypatch.setattr(telegram_setup, "load_config", lambda _: cfg)
    telegram_setup.main([])
    out = capsys.readouterr().out
    assert "TELEGRAM_CHAT_ID=987654321" in out
    assert "Morgan" in out
