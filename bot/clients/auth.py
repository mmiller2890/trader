"""Authentication and credentials helpers for Polymarket clients."""

from __future__ import annotations

from dataclasses import dataclass

from config.schema import AppConfig, Mode


@dataclass(frozen=True, slots=True)
class ClobCredentials:
    """Typed credentials consumed by the CLOB client wrapper."""

    private_key: str | None
    proxy_address: str | None
    api_key: str | None
    secret: str | None
    passphrase: str | None
    rpc_url: str | None

    @property
    def has_l1(self) -> bool:
        """Whether L1 wallet credentials exist."""

        return bool(self.private_key)

    @property
    def has_l2(self) -> bool:
        """Whether L2 API credentials exist."""

        return bool(self.api_key and self.secret and self.passphrase)


def build_clob_credentials(config: AppConfig) -> ClobCredentials:
    """Extract credentials from validated app config."""

    secrets = config.secrets
    return ClobCredentials(
        private_key=secrets.private_key.get_secret_value() if secrets.private_key else None,
        proxy_address=secrets.polymarket_proxy_address,
        api_key=secrets.clob_api_key.get_secret_value() if secrets.clob_api_key else None,
        secret=secrets.clob_secret.get_secret_value() if secrets.clob_secret else None,
        passphrase=secrets.clob_passphrase.get_secret_value() if secrets.clob_passphrase else None,
        rpc_url=secrets.rpc_url,
    )


def is_live_trading_enabled(config: AppConfig) -> bool:
    """Return whether real order routing should be allowed."""

    return (
        config.bot.mode == Mode.LIVE
        and config.execution.allow_live_trading
        and not config.execution.dry_run_force
    )
