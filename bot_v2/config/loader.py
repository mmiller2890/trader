"""YAML + environment configuration loader."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.schema import AppConfig


class ConfigError(RuntimeError):
    """Raised when config loading or validation fails."""


class EnvSecrets(BaseSettings):
    """Environment-backed secret values."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    private_key: str | None = Field(default=None, alias="PRIVATE_KEY")
    polymarket_proxy_address: str | None = Field(default=None, alias="POLYMARKET_PROXY_ADDRESS")
    clob_api_key: str | None = Field(default=None, alias="CLOB_API_KEY")
    clob_secret: str | None = Field(default=None, alias="CLOB_SECRET")
    clob_passphrase: str | None = Field(default=None, alias="CLOB_PASSPHRASE")
    rpc_url: str | None = Field(default=None, alias="RPC_URL")
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str | None = Field(default=None, alias="TELEGRAM_CHAT_ID")


def _read_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"required config file missing: {path}")
        return {}

    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"config file must deserialize to a mapping: {path}")
    return loaded


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
            continue
        merged[key] = value
    return merged


def _wrap_fragment(fragment_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        return {}
    if fragment_name in payload:
        fragment_value = payload[fragment_name]
        if not isinstance(fragment_value, dict):
            raise ConfigError(f"{fragment_name} fragment must be a mapping")
        return {fragment_name: fragment_value}
    return {fragment_name: payload}


def _env_secrets_payload() -> dict[str, Any]:
    env = EnvSecrets()
    raw = env.model_dump(mode="python")
    return {key: value for key, value in raw.items() if value is not None}


def _validate_operator_overlay(payload: dict[str, Any]) -> None:
    allowed = {
        "bot": {"mode"},
        "execution": {"allow_live_trading", "dry_run_force"},
        "market_data": {"subscribed_token_ids"},
        "spike_strategy": {"target_token_ids"},
    }
    extra_sections = set(payload) - set(allowed)
    if extra_sections:
        raise ConfigError(
            f"operator config contains forbidden sections: {sorted(extra_sections)}"
        )
    for section, values in payload.items():
        if not isinstance(values, dict):
            raise ConfigError(f"operator config section must be a mapping: {section}")
        extra_keys = set(values) - allowed[section]
        if extra_keys:
            raise ConfigError(
                f"operator config contains forbidden keys in {section}: {sorted(extra_keys)}"
            )

    has_mode_bundle = "bot" in payload or "execution" in payload
    if has_mode_bundle:
        bot = payload.get("bot")
        execution = payload.get("execution")
        if (
            not isinstance(bot, dict)
            or set(bot) != {"mode"}
            or not isinstance(execution, dict)
            or set(execution) != {"allow_live_trading", "dry_run_force"}
        ):
            raise ConfigError("operator config requires a complete mode bundle")
        bundle = (
            bot["mode"],
            execution["allow_live_trading"],
            execution["dry_run_force"],
        )
        if bundle not in {
            ("live", True, False),
            ("dry_run", False, True),
        }:
            raise ConfigError("operator config contains an invalid mode bundle")


def load_config(config_dir: str | Path | None = None) -> AppConfig:
    """Load app configuration from YAML fragments and environment variables."""

    config_root = Path(config_dir) if config_dir is not None else Path(__file__).resolve().parent
    if not config_root.exists():
        raise ConfigError(f"config directory does not exist: {config_root}")

    base_config = _read_yaml(config_root / "bot.yaml", required=True)
    risk_config = _read_yaml(config_root / "risk.yaml", required=False)
    spike_config = _read_yaml(config_root / "strategies" / "spike.yaml", required=False)

    merged = deepcopy(base_config)
    merged = _deep_merge(merged, _wrap_fragment("risk", risk_config))
    merged = _deep_merge(merged, _wrap_fragment("spike_strategy", spike_config))
    operator_config = _read_yaml(config_root / "operator.yaml", required=False)
    _validate_operator_overlay(operator_config)
    merged = _deep_merge(merged, operator_config)

    existing_secrets = merged.get("secrets", {})
    if existing_secrets and not isinstance(existing_secrets, dict):
        raise ConfigError("secrets section must be a mapping")
    merged["secrets"] = _deep_merge(existing_secrets, _env_secrets_payload())

    try:
        return AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"invalid app configuration: {exc}") from exc
