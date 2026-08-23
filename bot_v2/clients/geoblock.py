"""Fail-closed geographic compliance client."""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field

from config.schema import AppConfig

logger = logging.getLogger(__name__)


class GeoblockStatus(BaseModel):
    """Normalized geographic compliance result."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    country: str | None = None
    region: str | None = None
    reason: str = Field(min_length=1)


class GeoblockClient:
    """Query the Polymarket geoblock endpoint and fail closed."""

    def __init__(self, config: AppConfig, *, transport: httpx.Client | None = None) -> None:
        self._config = config
        self._transport = transport or httpx.Client(
            timeout=config.exchange.compliance_timeout_seconds
        )

    def check(self) -> GeoblockStatus:
        """Return allowed only for an explicit ``{"blocked": false}`` response."""

        try:
            response = self._transport.get(self._config.exchange.geoblock_url)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            return GeoblockStatus(allowed=False, reason=f"geoblock_timeout:{exc}")
        except httpx.HTTPError as exc:
            return GeoblockStatus(allowed=False, reason=f"geoblock_http_error:{exc}")

        try:
            payload = response.json()
        except ValueError:
            return GeoblockStatus(allowed=False, reason="geoblock_malformed_json")
        if not isinstance(payload, dict) or "blocked" not in payload:
            return GeoblockStatus(allowed=False, reason="geoblock_malformed_response")
        if not isinstance(payload["blocked"], bool):
            return GeoblockStatus(allowed=False, reason="geoblock_malformed_response")

        if payload["blocked"]:
            return GeoblockStatus(allowed=False, reason="geoblock_blocked")
        return GeoblockStatus(
            allowed=True,
            country=str(payload["country"]) if payload.get("country") else None,
            region=str(payload["region"]) if payload.get("region") else None,
            reason="geoblock_allowed",
        )
