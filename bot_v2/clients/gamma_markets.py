"""Strict public discovery for recurring Polymarket BTC markets."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from config.schema import AutomaticMarketConfig


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def window_start_epoch(now: datetime, duration_minutes: int = 15) -> int:
    """Return the epoch at the start of the containing UTC market window."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    duration_seconds = duration_minutes * 60
    epoch = int(now.astimezone(UTC).timestamp())
    return epoch - (epoch % duration_seconds)


class MarketDiscoveryError(RuntimeError):
    """Discovery failure containing only an operator-safe reason code."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class MarketOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    token_id: str = Field(pattern=r"^\d+$")


class DiscoveredMarket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    market_id: str
    condition_id: str
    slug: str
    title: str
    start_at: datetime
    end_at: datetime
    up: MarketOutcome
    down: MarketOutcome

    @property
    def asset_ids(self) -> list[str]:
        return [self.up.token_id, self.down.token_id]


def _decode_list(value: object) -> list[object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MarketDiscoveryError("invalid_outcome_tokens") from exc
    if not isinstance(value, list):
        raise MarketDiscoveryError("invalid_outcome_tokens")
    return value


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise MarketDiscoveryError("market_window_mismatch")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketDiscoveryError("market_window_mismatch") from exc
    if parsed.tzinfo is None:
        raise MarketDiscoveryError("market_window_mismatch")
    return parsed.astimezone(UTC)


class GammaMarketDiscoveryClient:
    """Resolve the active BTC 15-minute event through public Gamma metadata."""

    def __init__(
        self,
        config: AutomaticMarketConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=config.gamma_api_url,
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
        )
        self._closed = False

    async def discover_active(self, now: datetime | None = None) -> DiscoveredMarket:
        current = now or utc_now()
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(UTC)
        start_epoch = window_start_epoch(
            current, self._config.duration_minutes
        )
        slug = f"{self._config.slug_prefix}-{start_epoch}"
        try:
            response = await self._client.get(f"/events/slug/{slug}")
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            reason = (
                "market_not_found"
                if exc.response.status_code == 404
                else "gamma_http_error"
            )
            raise MarketDiscoveryError(reason) from None
        except (httpx.HTTPError, ValueError):
            raise MarketDiscoveryError("gamma_unavailable") from None

        return self._validate(payload, slug=slug, now=current)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    def _validate(
        self,
        payload: Any,
        *,
        slug: str,
        now: datetime,
    ) -> DiscoveredMarket:
        if not isinstance(payload, dict) or payload.get("slug") != slug:
            raise MarketDiscoveryError("market_not_found")
        if not payload.get("active") or payload.get("closed"):
            raise MarketDiscoveryError("market_not_active")
        markets = payload.get("markets")
        if not isinstance(markets, list) or len(markets) != 1:
            raise MarketDiscoveryError("invalid_market_shape")
        market = markets[0]
        if not isinstance(market, dict):
            raise MarketDiscoveryError("invalid_market_shape")
        if (
            not market.get("active")
            or market.get("closed")
            or not market.get("enableOrderBook")
            or not market.get("acceptingOrders")
        ):
            raise MarketDiscoveryError("market_not_active")

        start_at = _parse_datetime(
            market.get("eventStartTime") or payload.get("startTime")
        )
        end_at = _parse_datetime(payload.get("endDate"))
        expected_start = datetime.fromtimestamp(
            window_start_epoch(now, self._config.duration_minutes), tz=UTC
        )
        if start_at != expected_start or not (start_at <= now < end_at):
            raise MarketDiscoveryError("market_window_mismatch")

        outcomes = _decode_list(market.get("outcomes"))
        token_ids = _decode_list(market.get("clobTokenIds"))
        if (
            len(outcomes) != 2
            or len(token_ids) != 2
            or any(not isinstance(outcome, str) for outcome in outcomes)
            or set(outcomes) != {"Up", "Down"}
        ):
            raise MarketDiscoveryError("invalid_outcome_tokens")
        if (
            any(not isinstance(token_id, str) for token_id in token_ids)
            or len(set(token_ids)) != 2
            or any(
                not token_id.isascii() or not token_id.isdecimal()
                for token_id in token_ids
            )
        ):
            raise MarketDiscoveryError("invalid_outcome_tokens")
        outcome_tokens = dict(zip(outcomes, token_ids, strict=True))

        identifiers = (
            payload.get("id"),
            market.get("id"),
            market.get("conditionId"),
            payload.get("title"),
        )
        if any(not isinstance(value, str) or not value for value in identifiers):
            raise MarketDiscoveryError("invalid_market_shape")

        return DiscoveredMarket(
            event_id=identifiers[0],
            market_id=identifiers[1],
            condition_id=identifiers[2],
            slug=slug,
            title=identifiers[3],
            start_at=start_at,
            end_at=end_at,
            up=MarketOutcome(name="Up", token_id=outcome_tokens["Up"]),
            down=MarketOutcome(name="Down", token_id=outcome_tokens["Down"]),
        )
