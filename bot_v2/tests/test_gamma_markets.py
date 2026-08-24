from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from clients.gamma_markets import (
    GammaMarketDiscoveryClient,
    MarketDiscoveryError,
    window_start_epoch,
)
from config.schema import AutomaticMarketConfig


NOW = datetime(2026, 8, 24, 2, 59, 59, tzinfo=UTC)
SLUG = "btc-updown-15m-1787539500"


def payload(**market_updates: object) -> dict[str, object]:
    market: dict[str, object] = {
        "id": "market-1",
        "conditionId": "condition-1",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
        "acceptingOrders": True,
        "eventStartTime": "2026-08-24T02:45:00Z",
        "outcomes": json.dumps(["Up", "Down"]),
        "clobTokenIds": json.dumps(["111", "222"]),
    }
    market.update(market_updates)
    return {
        "id": "event-1",
        "slug": SLUG,
        "title": "Bitcoin Up or Down",
        "active": True,
        "closed": False,
        "startDate": "2026-08-23T02:53:49.360684Z",
        "endDate": "2026-08-24T03:00:00Z",
        "markets": [market],
    }


def client_for(response_payload: object, *, status: int = 200) -> GammaMarketDiscoveryClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=response_payload, request=request)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gamma-api.polymarket.com",
    )
    return GammaMarketDiscoveryClient(
        AutomaticMarketConfig(enabled=True), client=http_client
    )


def test_window_start_epoch_floors_to_utc_quarter_hour() -> None:
    assert window_start_epoch(NOW) == 1787539500
    assert window_start_epoch(datetime(2026, 8, 24, 2, 45, tzinfo=UTC)) == 1787539500
    with pytest.raises(ValueError, match="timezone-aware"):
        window_start_epoch(datetime(2026, 8, 24, 2, 45))


@pytest.mark.asyncio
async def test_discovery_maps_up_and_down_tokens_positionally() -> None:
    client = client_for(payload())

    market = await client.discover_active(now=NOW)
    await client.close()

    assert market.slug == SLUG
    assert market.up.name == "Up"
    assert market.up.token_id == "111"
    assert market.down.name == "Down"
    assert market.down.token_id == "222"
    assert market.asset_ids == ["111", "222"]


@pytest.mark.asyncio
async def test_discovery_maps_reversed_outcomes_by_position() -> None:
    client = client_for(
        payload(
            outcomes=json.dumps(["Down", "Up"]),
            clobTokenIds=json.dumps(["222", "111"]),
        )
    )

    market = await client.discover_active(now=NOW)

    assert market.up.token_id == "111"
    assert market.down.token_id == "222"


@pytest.mark.asyncio
async def test_discovery_accepts_live_shape_with_top_level_start_time() -> None:
    live_shape = payload()
    market = live_shape["markets"][0]
    assert isinstance(market, dict)
    live_shape["startTime"] = market["eventStartTime"]
    client = client_for(live_shape)

    market = await client.discover_active(now=NOW)

    assert market.slug == SLUG
    assert market.start_at == datetime(2026, 8, 24, 2, 45, tzinfo=UTC)


@pytest.mark.asyncio
async def test_discovery_rejects_naive_now() -> None:
    client = client_for(payload())

    with pytest.raises(ValueError, match="timezone-aware"):
        await client.discover_active(now=datetime(2026, 8, 24, 2, 59, 59))


@pytest.mark.asyncio
async def test_close_does_not_close_injected_http_client() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload(), request=request)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gamma-api.polymarket.com",
    )
    client = GammaMarketDiscoveryClient(
        AutomaticMarketConfig(enabled=True), client=http_client
    )

    await client.close()

    assert http_client.is_closed is False
    await http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_payload", "reason"),
    [
        (payload(closed=True), "market_not_active"),
        (payload(outcomes=json.dumps(["Up", "Sideways"])), "invalid_outcome_tokens"),
        (payload(clobTokenIds=json.dumps(["111"])), "invalid_outcome_tokens"),
        (payload(clobTokenIds=json.dumps(["111", "111"])), "invalid_outcome_tokens"),
        (payload(clobTokenIds=json.dumps(["111", "token-x"])), "invalid_outcome_tokens"),
        (payload(clobTokenIds=json.dumps([111, 222])), "invalid_outcome_tokens"),
    ],
)
async def test_discovery_rejects_invalid_market_payloads(
    response_payload: object,
    reason: str,
) -> None:
    client = client_for(response_payload)

    with pytest.raises(MarketDiscoveryError) as captured:
        await client.discover_active(now=NOW)
    await client.close()

    assert captured.value.reason == reason


@pytest.mark.asyncio
async def test_discovery_rejects_expired_or_mismatched_window() -> None:
    expired = payload()
    expired["endDate"] = "2026-08-24T02:45:00Z"
    client = client_for(expired)

    with pytest.raises(MarketDiscoveryError) as captured:
        await client.discover_active(now=NOW)
    await client.close()

    assert captured.value.reason == "market_window_mismatch"


@pytest.mark.asyncio
async def test_discovery_errors_never_include_remote_response_body() -> None:
    sentinel = "never-return-this-remote-body"
    client = client_for({"detail": sentinel}, status=503)

    with pytest.raises(MarketDiscoveryError) as captured:
        await client.discover_active(now=NOW)
    await client.close()

    assert captured.value.reason == "gamma_http_error"
    assert sentinel not in str(captured.value)
