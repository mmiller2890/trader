"""Typed configuration schema for the bot runtime."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from models.fees import DEFAULT_FEE_RATE
from models.tick import normalize_tick_size


class Mode(str, Enum):
    """Supported app runtime modes."""

    DRY_RUN = "dry_run"
    LIVE = "live"
    BACKTEST = "backtest"
    REPLAY = "replay"


class LogLevel(str, Enum):
    """Supported application log levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class TimeInForce(str, Enum):
    """Supported order time-in-force values."""

    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class BotConfig(BaseModel):
    """Core bot runtime config."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="polymarket-bot-v2", min_length=1)
    mode: Mode = Mode.DRY_RUN
    log_level: LogLevel = LogLevel.INFO
    heartbeat_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    housekeeping_interval_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    snapshot_interval_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)
    shutdown_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    kill_switch_on_startup: bool = False


class AutomaticMarketConfig(BaseModel):
    """Public recurring-market discovery settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    asset: Literal["btc"] = "btc"
    duration_minutes: Literal[15] = 15
    gamma_api_url: Literal["https://gamma-api.polymarket.com"] = (
        "https://gamma-api.polymarket.com"
    )
    slug_prefix: Literal["btc-updown-15m"] = "btc-updown-15m"
    refresh_lead_seconds: float = Field(default=10, ge=1, le=60)
    request_timeout_seconds: float = Field(default=5, ge=1, le=30)


class MarketDataConfig(BaseModel):
    """Market data ingest config."""

    model_config = ConfigDict(extra="forbid")

    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    reconnect_initial_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    reconnect_max_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    stale_after_seconds: float = Field(default=10.0, ge=1.0, le=600.0)
    heartbeat_timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    subscribed_market_ids: list[str] = Field(default_factory=list)
    subscribed_token_ids: list[str] = Field(default_factory=list)
    automatic_market: AutomaticMarketConfig = Field(
        default_factory=AutomaticMarketConfig
    )

    @model_validator(mode="after")
    def validate_backoff_range(self) -> Self:
        if self.reconnect_max_seconds < self.reconnect_initial_seconds:
            msg = "reconnect_max_seconds must be >= reconnect_initial_seconds"
            raise ValueError(msg)
        return self


class ExecutionConfig(BaseModel):
    """Order execution config."""

    model_config = ConfigDict(extra="forbid")

    dry_run_force: bool = True
    allow_live_trading: bool = False
    default_order_size: Decimal = Field(default=Decimal("5"), gt=Decimal("0"))
    max_order_size: Decimal = Field(default=Decimal("25"), gt=Decimal("0"))
    min_order_size: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
    max_slippage_bps: float = Field(default=20.0, ge=0.0, le=1000.0)
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id_prefix: str = Field(default="pm-bot", min_length=1, max_length=24)
    large_order_notional: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))
    min_live_buy_notional: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
    max_live_order_notional: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))
    default_tick_size: Decimal = Field(default=Decimal("0.01"), gt=Decimal("0"))
    post_only_maker_quotes: bool = True
    fee_rate: Decimal = Field(default=DEFAULT_FEE_RATE, ge=Decimal("0"), le=Decimal("1"))

    @model_validator(mode="after")
    def validate_order_size_bounds(self) -> Self:
        if self.min_order_size > self.default_order_size:
            raise ValueError("min_order_size must be <= default_order_size")
        if self.default_order_size > self.max_order_size:
            raise ValueError("default_order_size must be <= max_order_size")
        if self.min_live_buy_notional > self.max_live_order_notional:
            raise ValueError(
                "min_live_buy_notional must be <= max_live_order_notional"
            )
        normalize_tick_size(self.default_tick_size)
        return self


class BacktestConfig(BaseModel):
    """Deterministic paper-exchange settings."""

    model_config = ConfigDict(extra="forbid")

    starting_cash: Decimal = Field(default=Decimal("1000"), gt=Decimal("0"))
    # Per-share fee rate, not basis points: the real fee is price-dependent
    # (rate * p * (1 - p)), so a flat bps figure is wrong at every price
    # except by coincidence. Set to 0 in tests that want fee-free arithmetic.
    fee_rate: Decimal = Field(default=DEFAULT_FEE_RATE, ge=Decimal("0"), le=Decimal("1"))
    allow_short_positions: bool = True
    reject_sequence_gaps: bool = True
    max_payout_per_share: Decimal = Field(default=Decimal("1"), gt=Decimal("0"), le=Decimal("1"))


class ExchangeConfig(BaseModel):
    """Production exchange endpoints and live-safety settings."""

    model_config = ConfigDict(extra="forbid")

    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    chain_id: int = Field(default=137, gt=0)
    signature_type: int = Field(default=3, ge=0, le=3)
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    ws_ping_interval_seconds: float = Field(default=10, ge=5, le=60)
    compliance_timeout_seconds: float = Field(default=5, gt=0, le=30)


class PositionManagementConfig(BaseModel):
    """Position lifecycle and exit policy settings."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    take_profit_bps: Decimal = Field(default=Decimal("300"), gt=0)
    stop_loss_bps: Decimal = Field(default=Decimal("200"), gt=0)
    # A bps threshold is the wrong unit on a 0..1 market: one tick is 100 bps
    # at price 1.00 but 2000 bps at price 0.05, and the tick itself differs by
    # 10x between the 15m (0.001) and daily (0.01) markets. These floors
    # re-express both thresholds in units the exchange actually trades in, so
    # a stop can never sit inside the spread and fire on entry.
    min_edge_ticks: Decimal = Field(default=Decimal("2"), ge=Decimal("0"))
    min_stop_ticks: Decimal = Field(default=Decimal("2"), ge=Decimal("0"))
    # Floor both thresholds at this multiple of the observed bid/ask spread.
    # Entry fills at the ask and is marked against the bid, so a round trip
    # costs one spread before the market moves at all.
    spread_floor_multiple: Decimal = Field(default=Decimal("2"), ge=Decimal("0"))
    max_hold_seconds: float = Field(default=180, gt=0)
    exit_before_market_end_seconds: float = Field(default=60, gt=0)
    exit_retry_interval_seconds: float = Field(default=2, gt=0)
    max_exit_attempts: int = Field(default=3, ge=1)
    position_confirmation_grace_seconds: float = Field(default=30, gt=0)
    exit_time_in_force: TimeInForce = TimeInForce.IOC
    exit_on_strategy_sell: bool = True
    liquidate_full_position: bool = True


class RiskConfig(BaseModel):
    """Risk configuration used by pre-trade and runtime guards."""

    model_config = ConfigDict(extra="forbid")

    max_single_position_size: Decimal = Field(default=Decimal("50"), gt=Decimal("0"))
    max_total_exposure: Decimal = Field(default=Decimal("150"), gt=Decimal("0"))
    max_open_orders: int = Field(default=5, ge=1, le=500)
    max_daily_loss: Decimal = Field(default=Decimal("50"), gt=Decimal("0"))
    max_data_staleness_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    min_top_of_book_liquidity: Decimal = Field(default=Decimal("20"), ge=Decimal("0"))
    # Refuse to cross a book this wide. Depth is not the same as tightness: a
    # book can show 100 shares on each side while quoting 0.09 / 0.91, and
    # crossing it pays 82 cents a share the instant you enter. 0 disables.
    max_entry_spread_bps: float = Field(default=600.0, ge=0.0, le=20000.0)
    max_slippage_bps: float = Field(default=25.0, ge=0.0, le=1000.0)
    duplicate_signal_window_seconds: float = Field(default=15.0, ge=0.0, le=3600.0)
    circuit_breaker_failures: int = Field(default=5, ge=1, le=1000)
    circuit_breaker_window_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    circuit_breaker_cooldown_seconds: float = Field(default=120.0, ge=1.0, le=86400.0)
    # "enforce" blocks trades that cannot clear cost. "shadow" journals the
    # decision and routes anyway, which is the only way to gather the fill data
    # needed to calibrate the exit-cost model -- see the design doc. "off"
    # disables the gate entirely.
    edge_gate_mode: Literal["enforce", "shadow", "off"] = "enforce"
    safety_margin_bps: Decimal = Field(default=Decimal("50"), ge=Decimal("0"))

class SpikeStrategyConfig(BaseModel):
    """Single-strategy configuration for deterministic spike signals."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # Which way to trade a detected spike.
    #   "momentum"  -- go with the move. Measured on 95 episodes: 65% of
    #                  spikes continue, worth ~+187 bps net of spread.
    #   "reversion" -- fade the move. The original behaviour; measured
    #                  negative on the same data.
    # Flip this to A/B the two on identical conditions.
    direction: Literal["momentum", "reversion"] = "momentum"
    lookback_ticks: int = Field(default=3, ge=2, le=50000)
    # Measure the move over a wall-clock window instead of a fixed number of
    # book updates. Book updates arrive at ~250/sec per token, so a count of 8
    # spans about 30 milliseconds -- noise, not a move. When set, this takes
    # precedence and lookback_ticks becomes only a cap on retained history.
    lookback_seconds: float | None = Field(default=None, gt=0.0, le=3600.0)
    spike_threshold_bps: float = Field(default=80.0, gt=0.0, le=5000.0)
    cooldown_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    min_top_of_book_liquidity: Decimal = Field(default=Decimal("20"), ge=Decimal("0"))
    emit_on_upward_spike: bool = True
    emit_on_downward_spike: bool = True
    # Polymarket has no borrow, so "sell YES" only executes against inventory
    # already held. With this enabled an upward spike instead BUYs the paired
    # NO token, which is the same economic trade and always executable.
    sell_via_complement: bool = True
    # Refuse entries near the payout bounds. A fade bought at 0.97 risks 97
    # cents to make 3; the reward/risk is upside-down no matter how reliable
    # the reversion is. This band is what keeps the strategy in the part of
    # the curve where being right pays.
    min_entry_price: Decimal = Field(
        default=Decimal("0.10"), gt=Decimal("0"), lt=Decimal("1")
    )
    max_entry_price: Decimal = Field(
        default=Decimal("0.90"), gt=Decimal("0"), lt=Decimal("1")
    )
    target_market_ids: list[str] = Field(default_factory=list)
    target_token_ids: list[str] = Field(default_factory=list)

    # Maker entries rest inside the spread and pay no fee; taker entries cross
    # and pay ~350 bps at even odds. See the fee-aware execution design.
    entry_style: Literal["taker", "maker"] = "maker"
    maker_offset_ticks: int = Field(default=1, ge=0, le=20)
    quote_ttl_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    # MAKER_QUOTE signals must carry their own size; the order builder clamps
    # it to execution min/max and the live notional cap.
    maker_quote_size: Decimal = Field(default=Decimal("5"), gt=Decimal("0"))

    @model_validator(mode="after")
    def validate_entry_band(self) -> Self:
        if self.min_entry_price >= self.max_entry_price:
            raise ValueError("min_entry_price must be < max_entry_price")
        return self


class MarketMakerConfig(BaseModel):
    """Two-sided quoting configuration for the market-making strategy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Total quoted spread in ticks; each side sits half of this from fair.
    quote_spread_ticks: int = Field(default=2, ge=1, le=50)
    # Spread used on the reducing side once inventory forces an unwind.
    unwind_spread_ticks: int = Field(default=1, ge=0, le=50)
    # Largest fair-value skew, in ticks, applied at full inventory.
    max_skew_ticks: Decimal = Field(default=Decimal("2"), ge=Decimal("0"))
    base_quote_size: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))
    min_quote_size: Decimal = Field(default=Decimal("5"), gt=Decimal("0"))
    # Re-quote once the mid has moved more than this many ticks.
    refresh_move_ticks: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    # Fraction of max inventory beyond which the accumulating side stops.
    inventory_unwind_ratio: float = Field(default=0.8, gt=0.0, le=1.0)
    max_position_size: Decimal = Field(default=Decimal("200"), gt=Decimal("0"))
    min_book_liquidity: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    # Refresh a resting quote at least this often even in a quiet book.
    quote_ttl_seconds: float = Field(default=30.0, gt=0.0, le=3600.0)
    # Stop quoting this long before the market closes.
    stop_quoting_before_end_seconds: float = Field(default=60.0, ge=0.0)
    target_market_ids: list[str] = Field(default_factory=list)
    target_token_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_quote_bounds(self) -> Self:
        if self.min_quote_size > self.base_quote_size:
            raise ValueError("min_quote_size must be <= base_quote_size")
        if self.base_quote_size > self.max_position_size:
            raise ValueError("base_quote_size must be <= max_position_size")
        if self.unwind_spread_ticks > self.quote_spread_ticks:
            raise ValueError(
                "unwind_spread_ticks must be <= quote_spread_ticks"
            )
        return self


class ReliabilityConfig(BaseModel):
    """Multi-day unattended operations reliability settings."""

    model_config = ConfigDict(extra="forbid")

    live_lease_hours: float = Field(default=72, gt=0, le=168)
    task_restart_limit: int = Field(default=3, ge=1)
    task_restart_window_seconds: float = Field(default=600, gt=0)
    degraded_alert_after_seconds: float = Field(default=120, gt=0)
    authoritative_state_halt_after_seconds: float = Field(default=300, gt=0)
    rest_fallback_after_seconds: float = Field(default=30, gt=0)
    retry_initial_seconds: float = Field(default=1, gt=0)
    retry_max_seconds: float = Field(default=30, gt=0)
    retry_jitter_ratio: float = Field(default=0.20, ge=0, le=1)
    disk_warning_percent: float = Field(default=80, ge=0, le=100)
    disk_degraded_percent: float = Field(default=90, ge=0, le=100)
    disk_halt_percent: float = Field(default=95, ge=0, le=100)
    retention_interval_seconds: float = Field(default=3600, gt=0)
    signal_retention_count: int = Field(default=10000, ge=1)
    signal_retention_hours: float = Field(default=24, gt=0)
    fill_checkpoint_retention_days: float = Field(default=7, gt=0)
    realized_pnl_hot_days: int = Field(default=90, ge=1)
    closed_lifecycle_hot_count: int = Field(default=100, ge=1)
    journal_rotation_mib: float = Field(default=50, gt=0)
    journal_retention_days: int = Field(default=14, ge=1)
    journal_total_limit_mib: float = Field(default=500, gt=0)

    @model_validator(mode="after")
    def validate_reliability_bounds(self) -> Self:
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds must be >= retry_initial_seconds")
        if not (
            self.disk_warning_percent
            < self.disk_degraded_percent
            < self.disk_halt_percent
        ):
            raise ValueError("disk thresholds must be strictly increasing")
        return self


class NotificationsConfig(BaseModel):
    """Alerting and operator notifications config."""

    model_config = ConfigDict(extra="forbid")

    telegram_enabled: bool = False
    telegram_send_retries: int = Field(default=2, ge=0, le=10)
    repeated_failure_alert_threshold: int = Field(default=3, ge=1, le=100)
    large_order_threshold: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))
    durable_outbox_enabled: bool = True
    telegram_deduplication_seconds: int = Field(default=900, ge=0)
    alert_retry_initial_seconds: float = Field(default=2, gt=0)
    alert_retry_max_seconds: float = Field(default=300, gt=0)
    delivered_outbox_retention_days: int = Field(default=30, ge=1)
    daily_summary_hour_utc: int = Field(default=0, ge=0, le=23)


class SecretsConfig(BaseModel):
    """Secrets and credentials loaded from environment variables."""

    model_config = ConfigDict(extra="forbid")

    private_key: SecretStr | None = None
    polymarket_proxy_address: str | None = None
    clob_api_key: SecretStr | None = None
    clob_secret: SecretStr | None = None
    clob_passphrase: SecretStr | None = None
    rpc_url: str | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None


class AppConfig(BaseModel):
    """Root config object for the app."""

    model_config = ConfigDict(extra="forbid")

    bot: BotConfig = Field(default_factory=BotConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    position_management: PositionManagementConfig = Field(
        default_factory=PositionManagementConfig
    )
    spike_strategy: SpikeStrategyConfig = Field(default_factory=SpikeStrategyConfig)
    market_maker: MarketMakerConfig = Field(default_factory=MarketMakerConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    @model_validator(mode="after")
    def validate_mode_guards(self) -> Self:
        if self.bot.mode == Mode.LIVE and not self.execution.allow_live_trading:
            raise ValueError("live mode requires execution.allow_live_trading=true")
        if self.bot.mode == Mode.LIVE and self.execution.dry_run_force:
            raise ValueError("live mode requires execution.dry_run_force=false")
        if self.execution.max_live_order_notional > self.notifications.large_order_threshold:
            raise ValueError("max_live_order_notional must be <= notifications.large_order_threshold")
        if (
            self.bot.mode == Mode.LIVE
            and self.execution.allow_live_trading
            and not self.execution.dry_run_force
            and self.risk.edge_gate_mode == "shadow"
        ):
            raise ValueError(
                "edge gate shadow mode is dry-run only: it routes trades the "
                "gate rejected, which in live mode is a fee-gate bypass"
            )
        return self
