"""The quoting strategy is actually reached by a bootstrapped runtime."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_app
from models.market import MarketSnapshot
from models.signal import SignalSide, SignalType
from models.tick import DEFAULT_TICK_SIZE
from persistence.journal import JsonlJournal


def write_config(path: Path, *, market_maker_enabled: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "bot.yaml").write_text(
        """
bot:
  mode: dry_run
market_data:
  subscribed_token_ids: ["900", "901"]
  automatic_market:
    enabled: false
execution:
  dry_run_force: true
  allow_live_trading: false
  default_order_size: 100
  max_order_size: 500
  min_order_size: 1
  time_in_force: GTC
reliability:
  journal_rotation_mib: 7
  journal_retention_days: 3
  journal_total_limit_mib: 21
""".strip(),
        encoding="utf-8",
    )
    (path / "risk.yaml").write_text(
        """
risk:
  max_single_position_size: 200
  max_total_exposure: 200
  max_open_orders: 10
  min_top_of_book_liquidity: 1
""".strip(),
        encoding="utf-8",
    )
    strategies = path / "strategies"
    strategies.mkdir(parents=True, exist_ok=True)
    (strategies / "market_maker.yaml").write_text(
        f"""
market_maker:
  enabled: {str(market_maker_enabled).lower()}
  base_quote_size: 100
  min_quote_size: 5
  max_position_size: 200
""".strip(),
        encoding="utf-8",
    )


def book() -> MarketSnapshot:
    return MarketSnapshot(
        market_id="m1",
        token_id="900",
        best_bid=Decimal("0.49"),
        best_ask=Decimal("0.51"),
        mid_price=Decimal("0.50"),
        top_bid_size=Decimal("500"),
        top_ask_size=Decimal("500"),
    )


@pytest.mark.asyncio
async def test_disabled_market_maker_is_not_constructed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    write_config(config_dir, market_maker_enabled=False)

    services = await bootstrap_app(config_dir=config_dir)

    assert services.market_maker is None


@pytest.mark.asyncio
async def test_enabled_market_maker_quotes_from_a_book_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    write_config(config_dir, market_maker_enabled=True)

    services = await bootstrap_app(config_dir=config_dir)
    assert services.market_maker is not None

    plan = await services.market_maker.plan_quotes(book())
    await services.router.route_quote_plan(
        plan, strategy=services.market_maker, snapshot=book()
    )

    resting = services.market_maker.resting_quotes()
    assert len(resting) == 1
    assert resting[0].price == Decimal("0.49")
    assert resting[0].side.value == "buy"


@pytest.mark.asyncio
async def test_quotes_carry_post_only_through_the_order_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    write_config(config_dir, market_maker_enabled=True)

    services = await bootstrap_app(config_dir=config_dir)
    assert services.market_maker is not None
    plan = await services.market_maker.plan_quotes(book())

    order = services.order_builder.build(signal=plan.quotes[0], snapshot=book())

    assert order.post_only is True
    assert order.tick_size == DEFAULT_TICK_SIZE
    assert plan.quotes[0].signal_type is SignalType.MAKER_QUOTE
    assert plan.quotes[0].side is SignalSide.BUY


@pytest.mark.asyncio
async def test_journal_honours_configured_retention_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    config_dir = tmp_path / "config"
    write_config(config_dir, market_maker_enabled=False)

    services = await bootstrap_app(config_dir=config_dir)

    journal = services.journal
    assert isinstance(journal, JsonlJournal)
    mib = 1024 * 1024
    assert journal._rotate_bytes == 7 * mib
    assert journal._retention_days == 3
    assert journal._total_limit_bytes == 21 * mib
