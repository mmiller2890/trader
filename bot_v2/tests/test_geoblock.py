from __future__ import annotations

import httpx
import pytest

from clients.geoblock import GeoblockClient
from config.schema import AppConfig


class FakeTransport(httpx.Client):
    def __init__(self, response: object) -> None:
        super().__init__()
        self._response = response

    def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:
        if isinstance(self._response, Exception):
            raise self._response
        return httpx.Response(200, json=self._response, request=request)


def test_geoblock_allows_when_blocked_is_false() -> None:
    client = GeoblockClient(AppConfig(), transport=FakeTransport({"blocked": False}))
    status = client.check()
    assert status.allowed is True
    assert status.reason == "geoblock_allowed"


def test_geoblock_blocks_when_blocked_is_true() -> None:
    client = GeoblockClient(AppConfig(), transport=FakeTransport({"blocked": True}))
    status = client.check()
    assert status.allowed is False
    assert status.reason == "geoblock_blocked"


def test_geoblock_fails_closed_on_timeout() -> None:
    request = httpx.Request("GET", "https://polymarket.com/api/geoblock")
    client = GeoblockClient(
        AppConfig(),
        transport=FakeTransport(httpx.TimeoutException("timeout", request=request)),
    )
    status = client.check()
    assert status.allowed is False
    assert status.reason == "geoblock_timeout:TimeoutException"
    assert "polymarket.com" not in status.reason


def test_geoblock_fails_closed_on_malformed_json() -> None:
    class MalformedTransport(httpx.Client):
        def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:
            return httpx.Response(200, content=b"not-json", request=request)

    client = GeoblockClient(AppConfig(), transport=MalformedTransport())
    status = client.check()
    assert status.allowed is False
    assert "malformed" in status.reason


def test_geoblock_fails_closed_on_missing_blocked_field() -> None:
    client = GeoblockClient(AppConfig(), transport=FakeTransport({"country": "US"}))
    status = client.check()
    assert status.allowed is False
    assert "malformed" in status.reason
