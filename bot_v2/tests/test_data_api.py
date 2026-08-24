from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from clients.data_api import DataApiClient, DataApiError
from config.schema import AppConfig


class FakeTransport(httpx.Client):
    def __init__(self, responses: list[object]) -> None:
        super().__init__()
        self._responses = responses
        self.requests: list[httpx.Request] = []

    def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return httpx.Response(200, json=response, request=request)


def position_row(
    *,
    condition: str = "c1",
    asset: str = "t1",
    size: str = "5",
    avg: str = "0.50",
    cur: str = "0.55",
    cash_pnl: str = "0.25",
    realized_pnl: str = "0.10",
) -> dict[str, object]:
    return {
        "conditionId": condition,
        "asset": asset,
        "size": size,
        "avgPrice": avg,
        "curPrice": cur,
        "cashPnl": cash_pnl,
        "realizedPnl": realized_pnl,
    }


def test_data_api_requests_exact_first_page_params() -> None:
    transport = FakeTransport([[]])
    client = DataApiClient(AppConfig(), transport=transport)
    positions = client.get_positions("0x1111111111111111111111111111111111111111")
    assert positions == []
    request = transport.requests[0]
    assert request.url.host == "data-api.polymarket.com"
    assert request.url.path == "/positions"
    assert request.url.params["user"] == "0x1111111111111111111111111111111111111111"
    assert request.url.params["sizeThreshold"] == "0"
    assert request.url.params["redeemable"] == "false"
    assert request.url.params["limit"] == "500"
    assert request.url.params["offset"] == "0"


def test_data_api_paginates_until_short_page() -> None:
    first_page = [position_row(condition=f"c{i}", asset=f"t{i}") for i in range(500)]
    second_page = [position_row(condition="c500", asset="t500")]
    transport = FakeTransport([first_page, second_page])
    client = DataApiClient(AppConfig(), transport=transport)
    positions = client.get_positions("0x1111111111111111111111111111111111111111")
    assert len(positions) == 501
    assert positions[0].market_id == "c0"
    assert positions[0].token_id == "t0"
    assert positions[0].quantity == Decimal("5")
    assert positions[0].average_entry_price == Decimal("0.50")
    assert positions[0].mark_price == Decimal("0.55")
    assert positions[0].unrealized_pnl == Decimal("0.25")
    assert positions[0].realized_pnl == Decimal("0.10")
    assert positions[500].market_id == "c500"
    assert transport.requests[1].url.params["offset"] == "500"


def test_data_api_returns_empty_for_empty_account() -> None:
    transport = FakeTransport([[]])
    client = DataApiClient(AppConfig(), transport=transport)
    assert client.get_positions("0x1111111111111111111111111111111111111111") == []


def test_data_api_rejects_http_errors() -> None:
    transport = FakeTransport([httpx.HTTPStatusError("boom", request=httpx.Request("GET", "https://x"), response=httpx.Response(500))])
    client = DataApiClient(AppConfig(), transport=transport)
    with pytest.raises(DataApiError, match="HTTP"):
        client.get_positions("0x1111111111111111111111111111111111111111")


def test_data_api_rejects_non_list_payloads() -> None:
    transport = FakeTransport([{"data": "not-a-list"}])
    client = DataApiClient(AppConfig(), transport=transport)
    with pytest.raises(DataApiError, match="list"):
        client.get_positions("0x1111111111111111111111111111111111111111")


def test_data_api_rejects_malformed_rows() -> None:
    transport = FakeTransport([[{"conditionId": "c1"}]])
    client = DataApiClient(AppConfig(), transport=transport)
    with pytest.raises(DataApiError, match="row"):
        client.get_positions("0x1111111111111111111111111111111111111111")


def test_data_api_rejects_invalid_numeric_fields() -> None:
    transport = FakeTransport([[position_row(size="not-a-number")]])
    client = DataApiClient(AppConfig(), transport=transport)
    with pytest.raises(DataApiError, match="numeric"):
        client.get_positions("0x1111111111111111111111111111111111111111")


def test_data_api_rejects_offset_above_limit() -> None:
    full_page = [position_row(condition=f"c{i}", asset=f"t{i}") for i in range(500)]
    transport = FakeTransport([full_page for _ in range(25)])
    client = DataApiClient(AppConfig(), transport=transport)
    with pytest.raises(DataApiError, match="offset"):
        client.get_positions("0x1111111111111111111111111111111111111111")
