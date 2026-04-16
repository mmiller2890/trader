"""Typed configuration schema for the bot runtime."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


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

    name: str = Field(default="polymarket-bot", min_length=1)
    mode: Mode = Mode.DRY_RUN
    log_level: LogLevel = LogLevel.INFO
    heartbeat_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    housekeeping_interval_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    snapshot_interval_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)
    shutdown_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    kill_switch_on_startup: bool = False


class MarketDataConfig(BaseModel):
    """Market data ingest config."""

    model_config = ConfigDict(extra="forbid")

    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    reconnect_initial_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    reconnect_max_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    stale_after_seconds: float = Field(default=10.0, ge=1.0, le=600.0)
    heartbeat_timeout_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    subscribed_market_ids: list[str] = Field(default_factory=list)
    subscribed_token_ids: list[str] = Field(default_factory=list)

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

    @model_validator(mode="after")
    def validate_order_size_bounds(self) -> Self:
        if self.min_order_size > self.default_order_size:
            raise ValueError("min_order_size must be <= default_order_size")
        if self.default_order_size > self.max_order_size:
            raise ValueError("default_order_size must be <= max_order_size")
        return self


class RiskConfig(BaseModel):
    """Risk configuration used by pre-trade and runtime guards."""

    model_config = ConfigDict(extra="forbid")

    max_single_position_size: Decimal = Field(default=Decimal("50"), gt=Decimal("0"))
    max_total_exposure: Decimal = Field(default=Decimal("150"), gt=Decimal("0"))
    max_open_orders: int = Field(default=5, ge=1, le=500)
    max_daily_loss: Decimal = Field(default=Decimal("50"), gt=Decimal("0"))
    max_data_staleness_seconds: float = Field(default=15.0, ge=1.0, le=600.0)
    min_top_of_book_liquidity: Decimal = Field(default=Decimal("20"), ge=Decimal("0"))
    max_slippage_bps: float = Field(default=25.0, ge=0.0, le=1000.0)
    duplicate_signal_window_seconds: float = Field(default=15.0, ge=0.0, le=3600.0)
    circuit_breaker_failures: int = Field(default=5, ge=1, le=1000)
    circuit_breaker_window_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    circuit_breaker_cooldown_seconds: float = Field(default=120.0, ge=1.0, le=86400.0)

    @model_validator(mode="after")
    def validate_position_vs_exposure(self) -> Self:
        if self.max_single_position_size > self.max_total_exposure:
            msg = "max_single_position_size must be <= max_total_exposure"
            raise ValueError(msg)
        return self


class SpikeStrategyConfig(BaseModel):
    """Single-strategy configuration for deterministic spike signals."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    lookback_ticks: int = Field(default=3, ge=2, le=50)
    spike_threshold_bps: float = Field(default=80.0, gt=0.0, le=5000.0)
    cooldown_seconds: float = Field(default=30.0, ge=0.0, le=3600.0)
    min_top_of_book_liquidity: Decimal = Field(default=Decimal("20"), ge=Decimal("0"))
    emit_on_upward_spike: bool = True
    emit_on_downward_spike: bool = True
    target_market_ids: list[str] = Field(default_factory=list)
    target_token_ids: list[str] = Field(default_factory=list)


class NotificationsConfig(BaseModel):
    """Alerting and operator notifications config."""

    model_config = ConfigDict(extra="forbid")

    telegram_enabled: bool = False
    telegram_send_retries: int = Field(default=2, ge=0, le=10)
    repeated_failure_alert_threshold: int = Field(default=3, ge=1, le=100)
    large_order_threshold: Decimal = Field(default=Decimal("100"), gt=Decimal("0"))


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
    telegram_chat_id: str | None = None


class AppConfig(BaseModel):
    """Root config object for the app."""

    model_config = ConfigDict(extra="forbid")

    bot: BotConfig = Field(default_factory=BotConfig)
    market_data: MarketDataConfig = Field(default_factory=MarketDataConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    spike_strategy: SpikeStrategyConfig = Field(default_factory=SpikeStrategyConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    @model_validator(mode="after")
    def validate_mode_guards(self) -> Self:
        if self.bot.mode == Mode.LIVE and not self.execution.allow_live_trading:
            raise ValueError("live mode requires execution.allow_live_trading=true")
        if self.bot.mode == Mode.LIVE and self.execution.dry_run_force:
            raise ValueError("live mode requires execution.dry_run_force=false")
        return self
