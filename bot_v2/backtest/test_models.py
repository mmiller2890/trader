from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from backtest.models import (
    BookDeltaEvent,
    BookSnapshotEvent,
    HistoricalBookEvent,
)
from config.schema import AppConfig
from models.market import OrderBookLevel


NOW = datetime(2025, 1, 1, tzinfo=UTC)


def test_backtest_config_has_conservative_defaults() -> None:
    config = AppConfig()
    assert config.backtest.starting_cash == Decimal("1000")
    assert config.backtest.taker_fee_bps == Decimal("10")
    assert config.backtest.allow_short_positions is True
    assert config.backtest.reject_sequence_gaps is True
    assert config.backtest.max_payout_per_share == Decimal("1")


def test_historical_event_union_uses_event_type_discriminator() -> None:
    payload = {
        "event_type": "book_snapshot",
        "market_id": "m1",
        "token_id": "t1",
        "bids": [{"price": "0.49", "size": "5"}],
        "asks": [{"price": "0.51", "size": "7"}],
        "sequence_id": 10,
        "source_ts": NOW.isoformat(),
        "received_ts": NOW.isoformat(),
    }
    event = TypeAdapter(HistoricalBookEvent).validate_python(payload)
    assert isinstance(event, BookSnapshotEvent)


def test_delta_accepts_zero_size_as_level_deletion() -> None:
    event = BookDeltaEvent(
        market_id="m1",
        token_id="t1",
        bid_updates=[OrderBookLevel(price=Decimal("0.49"), size=Decimal("0"))],
        ask_updates=[],
        sequence_id=11,
        source_ts=NOW,
        received_ts=NOW,
    )
    assert event.bid_updates[0].size == 0


def test_backtest_fee_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        AppConfig(backtest={"taker_fee_bps": "-1"})
