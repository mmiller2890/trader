"""Typed client for the Polymarket Data API positions endpoint."""

from __future__ import annotations

from decimal import Decimal

import httpx

from config.schema import AppConfig
from models.position import Position


class DataApiError(RuntimeError):
    """Raised when the Data API cannot satisfy a requested read."""


class DataApiClient:
    """Paginated current-position reads from the Data API."""

    def __init__(self, config: AppConfig, *, transport: httpx.Client | None = None) -> None:
        self._config = config
        self._transport = transport or httpx.Client()

    def get_positions(self, user_address: str) -> list[Position]:
        """Fetch every current position for ``user_address``."""

        positions: list[Position] = []
        offset = 0
        while True:
            if offset > 10000:
                raise DataApiError(f"positions pagination exceeded offset limit: {offset}")
            page = self._fetch_page(user_address, offset)
            positions.extend(page)
            if len(page) < 500:
                return positions
            offset += len(page)

    def _fetch_page(self, user_address: str, offset: int) -> list[Position]:
        url = f"{self._config.exchange.data_api_host}/positions"
        params = {
            "user": user_address,
            "sizeThreshold": "0",
            "redeemable": "false",
            "limit": "500",
            "offset": str(offset),
        }
        try:
            response = self._transport.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DataApiError(f"positions HTTP request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise DataApiError("positions response is not valid JSON") from exc
        if not isinstance(payload, list):
            raise DataApiError(f"positions response is not a list: {type(payload).__name__}")

        normalized: list[Position] = []
        for index, row in enumerate(payload):
            if not isinstance(row, dict):
                raise DataApiError(f"positions row {index} is not an object")
            try:
                normalized.append(
                    Position(
                        market_id=str(row["conditionId"]),
                        token_id=str(row["asset"]),
                        quantity=Decimal(str(row["size"])),
                        average_entry_price=Decimal(str(row["avgPrice"])),
                        mark_price=Decimal(str(row["curPrice"])),
                        unrealized_pnl=Decimal(str(row["cashPnl"])),
                        realized_pnl=Decimal(str(row["realizedPnl"])),
                    )
                )
            except KeyError as exc:
                raise DataApiError(f"positions row {index} missing field {exc}") from exc
            except Exception as exc:
                raise DataApiError(f"positions row {index} has invalid numeric fields: {exc}") from exc
        return normalized
