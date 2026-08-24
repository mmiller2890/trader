"""Deterministic configuration fingerprint for live lease authorization."""

from __future__ import annotations

import hashlib
import json

from config.schema import AppConfig


def config_fingerprint(config: AppConfig) -> str:
    """Return a 64-char lowercase hex fingerprint of safety-relevant fields.

    The fingerprint covers bot mode, execution guards, market scope, and
    strategy targets — never secrets. Serialization order is canonical.
    """

    payload = {
        "bot_mode": config.bot.mode.value,
        "allow_live_trading": config.execution.allow_live_trading,
        "dry_run_force": config.execution.dry_run_force,
        "subscribed_token_ids": sorted(config.market_data.subscribed_token_ids),
        "target_token_ids": sorted(config.spike_strategy.target_token_ids),
        "automatic_market_enabled": config.market_data.automatic_market.enabled,
    }
    serialized = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
