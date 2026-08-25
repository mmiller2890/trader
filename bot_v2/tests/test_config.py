from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.loader import ConfigError, load_config
from config.schema import AppConfig, Mode, TimeInForce

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_production_position_management_defaults() -> None:
    config = load_config(PROJECT_ROOT / "config")
    policy = config.position_management
    # Brackets are disaster guards, not the primary exit: the measured edge is
    # a mid-to-mid result over a fixed horizon with no stops, and a stop set
    # near the mean continuation fires on noise before the move develops.
    assert policy.take_profit_bps == Decimal("800")
    assert policy.stop_loss_bps == Decimal("800")
    # The clock is the primary exit, set to the horizon the edge was measured
    # over. Never hold to resolution; leave before the final scramble.
    assert policy.max_hold_seconds == 60
    assert policy.exit_before_market_end_seconds == 30
    assert policy.exit_retry_interval_seconds == 2
    assert policy.max_exit_attempts == 3
    assert policy.position_confirmation_grace_seconds == 30
    assert policy.exit_time_in_force == TimeInForce.IOC


@pytest.mark.parametrize(
    "field,value",
    [
        ("take_profit_bps", "0"),
        ("stop_loss_bps", "0"),
        ("max_hold_seconds", 0),
        ("exit_before_market_end_seconds", 0),
        ("exit_retry_interval_seconds", 0),
        ("max_exit_attempts", 0),
        ("position_confirmation_grace_seconds", 0),
    ],
)
def test_position_management_rejects_non_positive_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        AppConfig(position_management={field: value})


def test_exchange_defaults_are_production_v2_and_live_stays_off() -> None:
    config = AppConfig()
    assert config.exchange.clob_host == "https://clob.polymarket.com"
    assert config.exchange.data_api_host == "https://data-api.polymarket.com"
    assert config.exchange.chain_id == 137
    assert config.exchange.signature_type == 3
    assert config.exchange.geoblock_url == "https://polymarket.com/api/geoblock"
    assert config.exchange.ws_ping_interval_seconds == 10
    assert config.execution.max_live_order_notional == Decimal("1")
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True


def test_production_yaml_uses_bounded_quoting_execution() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # Sized for the active taker strategy: a position must be small enough to
    # unwind against real book depth, or the exit budget exhausts and halts.
    assert config.execution.default_order_size == Decimal("5")
    assert config.execution.max_order_size == Decimal("10")
    # GTC so a marketable remainder rests instead of being killed; quoting
    # depends on it. Exits still force IOC.
    assert config.execution.time_in_force == TimeInForce.GTC
    assert config.execution.min_live_buy_notional == Decimal("1")
    # The per-order live ceiling is the hard cap on a single mistake. It is
    # held small until the live order path is proven; see bot.yaml.
    assert config.execution.max_live_order_notional == Decimal("2")
    assert config.execution.post_only_maker_quotes is True
    assert config.risk.max_data_staleness_seconds == 15
    assert config.market_data.heartbeat_timeout_seconds == 30
    assert config.market_data.automatic_market.enabled is True
    assert config.market_data.automatic_market.asset == "btc"
    assert config.market_data.automatic_market.duration_minutes == 15
    assert config.exchange.signature_type == 1


def test_live_notional_cap_must_not_exceed_large_order_threshold() -> None:
    with pytest.raises(ValidationError):
        AppConfig(execution={"max_live_order_notional": "101"})


def test_share_limit_is_independent_from_marked_notional_limit() -> None:
    config = AppConfig(
        risk={
            "max_single_position_size": "100",
            "max_total_exposure": "10",
        }
    )

    assert config.risk.max_single_position_size == Decimal("100")
    assert config.risk.max_total_exposure == Decimal("10")


def test_load_config_merges_yaml_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_text(
        tmp_path / "bot.yaml",
        """
bot:
  mode: dry_run
execution:
  dry_run_force: true
  allow_live_trading: false
""".strip(),
    )
    write_text(
        tmp_path / "risk.yaml",
        """
risk:
  max_open_orders: 7
""".strip(),
    )
    write_text(
        tmp_path / "strategies" / "spike.yaml",
        """
spike_strategy:
  spike_threshold_bps: 125
""".strip(),
    )
    monkeypatch.setenv("CLOB_API_KEY", "abc123")

    config = load_config(tmp_path)

    assert config.bot.mode.value == "dry_run"
    assert config.risk.max_open_orders == 7
    assert config.spike_strategy.spike_threshold_bps == 125
    assert config.secrets.clob_api_key is not None
    assert config.secrets.clob_api_key.get_secret_value() == "abc123"


def test_live_mode_validation_fails_without_explicit_guard(tmp_path: Path) -> None:
    write_text(
        tmp_path / "bot.yaml",
        """
bot:
  mode: live
execution:
  dry_run_force: true
  allow_live_trading: false
""".strip(),
    )

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_reliability_defaults_match_unattended_operations_spec() -> None:
    config = AppConfig()
    reliability = config.reliability
    assert reliability.live_lease_hours == 72
    assert reliability.task_restart_limit == 3
    assert reliability.task_restart_window_seconds == 600
    assert reliability.degraded_alert_after_seconds == 120
    assert reliability.authoritative_state_halt_after_seconds == 300
    assert reliability.rest_fallback_after_seconds == 30
    assert reliability.retry_initial_seconds == 1
    assert reliability.retry_max_seconds == 30
    assert reliability.retry_jitter_ratio == 0.20
    assert reliability.disk_warning_percent == 80
    assert reliability.disk_degraded_percent == 90
    assert reliability.disk_halt_percent == 95
    assert config.notifications.durable_outbox_enabled is True
    assert config.notifications.telegram_deduplication_seconds == 900
    assert config.notifications.alert_retry_max_seconds == 300


@pytest.mark.parametrize(
    "payload",
    [
        {"live_lease_hours": 0},
        {"live_lease_hours": 169},
        {"retry_initial_seconds": 31, "retry_max_seconds": 30},
        {"retry_jitter_ratio": 1.01},
        {"disk_warning_percent": 91, "disk_degraded_percent": 90},
        {"disk_degraded_percent": 96, "disk_halt_percent": 95},
    ],
)
def test_reliability_rejects_unsafe_bounds(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AppConfig(reliability=payload)


def test_checked_in_config_is_never_armed_for_live() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # All three gates must ship closed. Arming live is an operator action.
    assert config.bot.mode is Mode.DRY_RUN
    assert config.execution.allow_live_trading is False
    assert config.execution.dry_run_force is True


def test_checked_in_market_maker_ships_disabled() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    assert config.market_maker.enabled is False


def test_strategy_inventory_cap_stays_within_the_risk_backstop() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    assert (
        config.market_maker.max_position_size
        <= config.risk.max_single_position_size
    )


def test_open_order_budget_admits_two_sided_quoting() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # One bid and one ask on each of the two outcome tokens.
    assert config.risk.max_open_orders >= 4


def test_live_order_cap_stays_within_the_minimum_buy_notional() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # An order cap below the exchange minimum would reject every live buy.
    assert (
        config.execution.min_live_buy_notional
        <= config.execution.max_live_order_notional
    )


def test_exit_thresholds_ship_with_spread_and_tick_floors() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    policy = config.position_management

    # Without these a bps stop sits inside the spread and fires on entry.
    assert policy.min_edge_ticks > 0
    assert policy.min_stop_ticks > 0
    assert policy.spread_floor_multiple >= 2


def test_spike_strategy_measures_moves_in_wall_clock_time() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # A tick count spans an unpredictable window at ~250 updates/sec.
    assert config.spike_strategy.lookback_seconds is not None
    assert config.spike_strategy.lookback_seconds >= 5


def test_sell_signals_route_through_the_complement_token() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # Polymarket has no borrow; a plain sell needs inventory to execute.
    assert config.spike_strategy.sell_via_complement is True


def test_disk_halt_still_protects_the_data_volume() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    reliability = config.reliability

    # Raised to clear a normally-full workstation, but a real ceiling remains:
    # under ~1% free, SQLite can fail a write mid-transaction.
    assert reliability.disk_halt_percent <= 99
    assert (
        reliability.disk_warning_percent
        < reliability.disk_degraded_percent
        < reliability.disk_halt_percent
    )


def test_order_size_can_be_unwound_against_typical_book_depth() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # The liquidity check demands max(min_top_of_book_liquidity, size) of depth
    # on the exit side. Sizing above that guarantees stuck positions.
    assert config.execution.max_order_size <= config.risk.min_top_of_book_liquidity


def test_entry_price_band_avoids_the_payout_bounds() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    spike = config.spike_strategy

    assert spike.min_entry_price >= Decimal("0.05")
    assert spike.max_entry_price <= Decimal("0.95")
    assert spike.min_entry_price < spike.max_entry_price


def test_entry_spread_guard_is_configured() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # Without this the bot crosses arbitrarily wide books and loses the spread
    # on entry; it dominated every dry-run result before it existed.
    assert config.risk.max_entry_spread_bps > 0
    assert config.risk.max_entry_spread_bps <= 1000


def test_shipped_direction_is_momentum() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # 65% of spikes continue on measured data; fading them lost.
    assert config.spike_strategy.direction == "momentum"


def test_hold_window_matches_the_measured_horizon() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")

    # The edge was measured mid-to-mid over 60s. Holding materially longer
    # than the horizon you measured is not the strategy you tested.
    assert config.position_management.max_hold_seconds <= 120


def test_brackets_are_wider_than_the_mean_continuation() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "config")
    policy = config.position_management

    # Mean continuation is ~400 bps. Brackets at or below that fire on noise
    # before the move being traded develops -- observed as 65/35 in the book
    # data but 13/13 in the bot's own trades.
    assert policy.take_profit_bps > Decimal("400")
    assert policy.stop_loss_bps > Decimal("400")
