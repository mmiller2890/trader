"""Time-limited live operating lease lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from config.schema import AppConfig
from config.fingerprint import config_fingerprint
from models.operations import LeaseStatus, LiveOperatingLease
from persistence.operations import OperationsRepository


class LiveResumeRejected(RuntimeError):
    """Raised when automatic live resume fails a safety gate."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _credentials_present(config: AppConfig) -> bool:
    secrets = config.secrets
    if not secrets.private_key:
        return False
    if not (secrets.clob_api_key and secrets.clob_secret and secrets.clob_passphrase):
        return False
    if config.exchange.signature_type == 0:
        return True
    return bool(secrets.polymarket_proxy_address)


class LiveLeaseService:
    """Issues, validates, revokes, and monitors the live operating lease."""

    def __init__(
        self,
        repository: OperationsRepository,
        *,
        live_lease_hours: float = 72,
    ) -> None:
        self._repository = repository
        self._lease_hours = live_lease_hours

    async def issue(self, config: AppConfig, *, now: datetime) -> LiveOperatingLease:
        from uuid import uuid4

        lease = LiveOperatingLease(
            lease_id=f"lease-{uuid4().hex}",
            issued_at=now,
            expires_at=now + timedelta(hours=self._lease_hours),
            config_fingerprint=config_fingerprint(config),
            status=LeaseStatus.ACTIVE,
        )
        await self._repository.create_lease(lease)
        return lease

    async def validate_for_resume(
        self, config: AppConfig, *, now: datetime
    ) -> LiveOperatingLease:
        lease = await self._repository.get_active_lease()
        if lease is None or lease.status != LeaseStatus.ACTIVE:
            raise LiveResumeRejected("lease_missing_or_revoked")
        if now >= lease.expires_at:
            raise LiveResumeRejected("lease_expired")
        current_fp = config_fingerprint(config)
        if lease.config_fingerprint != current_fp:
            raise LiveResumeRejected("config_fingerprint_mismatch")
        return lease

    async def revoke(self, reason: str, *, now: datetime) -> LiveOperatingLease | None:
        return await self._repository.revoke_active_lease(
            reason=reason[:512], revoked_at=now
        )

    async def expiration_state(
        self, *, now: datetime
    ) -> Literal["valid", "warn_24h", "warn_1h", "expired", "missing"]:
        lease = await self._repository.get_active_lease()
        if lease is None:
            return "missing"
        remaining = lease.expires_at - now
        if remaining <= timedelta(0):
            return "expired"
        if remaining <= timedelta(hours=1):
            return "warn_1h"
        if remaining <= timedelta(hours=24):
            return "warn_24h"
        return "valid"
