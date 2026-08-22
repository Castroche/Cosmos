from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain import TradingMode
from .timeframes import TIMEFRAME_SECONDS

SUPPORTED_TIMEFRAMES = set(TIMEFRAME_SECONDS)


class MarketType(StrEnum):
    SPOT = "spot"
    SWAP = "swap"


class MarginMode(StrEnum):
    CROSS = "cross"
    ISOLATED = "isolated"


class StrategyKind(StrEnum):
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"


class RiskPreferences(BaseModel):
    risk_per_trade: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.03"))
    max_daily_drawdown: Decimal = Field(default=Decimal("0.02"), gt=0, le=Decimal("0.10"))
    max_gross_exposure: Decimal = Field(default=Decimal("2.0"), gt=0, le=Decimal("5"))
    max_portfolio_risk: Decimal = Field(default=Decimal("0.05"), gt=0, le=Decimal("0.20"))
    existing_position_stress: Decimal = Field(default=Decimal("0.02"), gt=0, le=Decimal("0.20"))
    max_directional_exposure: Decimal = Field(default=Decimal("1.25"), gt=0, le=Decimal("5"))
    max_leverage: Decimal = Field(default=Decimal("3"), ge=1, le=Decimal("20"))
    max_symbol_exposure: Decimal = Field(default=Decimal("1.0"), gt=0, le=Decimal("2"))
    max_open_positions: int = Field(default=5, ge=1, le=50)
    minimum_reward_risk: Decimal = Field(default=Decimal("1.8"), ge=1, le=Decimal("10"))
    max_spread_bps: Decimal = Field(default=Decimal("20"), gt=0, le=Decimal("200"))
    max_abs_funding_rate: Decimal = Field(default=Decimal("0.001"), gt=0, le=Decimal("0.01"))


class ResearchValidationPreferences(BaseModel):
    minimum_trades: int = Field(default=100, ge=10, le=10000)
    minimum_trade_evidence_ratio: Decimal = Field(default=Decimal("0.80"), ge=Decimal("0.50"), le=1)
    minimum_recent_trades: int = Field(default=20, ge=5, le=1000)
    minimum_sample_bars: int = Field(default=1000, ge=100, le=1000000)
    minimum_profit_factor: Decimal = Field(default=Decimal("1.15"), ge=1, le=5)
    minimum_win_rate_percent: Decimal = Field(
        default=Decimal("45"), ge=Decimal("20"), le=Decimal("90")
    )
    minimum_payoff_ratio: Decimal = Field(
        default=Decimal("1.20"), ge=Decimal("0.50"), le=Decimal("10")
    )
    maximum_drawdown_percent: Decimal = Field(default=Decimal("20"), gt=0, le=80)
    stress_fee_multiplier: Decimal = Field(default=Decimal("1.5"), ge=1, le=5)
    stress_slippage_multiplier: Decimal = Field(default=Decimal("2"), ge=1, le=10)
    require_higher_timeframe_confirmation: bool = True


class AgentForwardValidationPreferences(BaseModel):
    """Evidence required before sandbox performance may support a live review."""

    minimum_closed_episodes: int = Field(default=30, ge=10, le=10000)
    minimum_independent_runs: int = Field(default=20, ge=5, le=10000)
    minimum_recent_episodes: int = Field(default=10, ge=5, le=1000)
    minimum_profit_factor: Decimal = Field(default=Decimal("1.10"), ge=1, le=5)
    minimum_expectancy_r: Decimal = Field(
        default=Decimal("0.05"), ge=Decimal("-1"), le=Decimal("5")
    )
    maximum_drawdown_r: Decimal = Field(default=Decimal("6"), gt=0, le=100)
    maximum_consecutive_losses: int = Field(default=8, ge=1, le=100)


class StrategyPreferences(BaseModel):
    default_kind: StrategyKind = StrategyKind.TREND
    automatic_regime_selection: bool = True
    default_prompt: str = "顺应主要趋势，等待回踩确认后入场，避免追涨杀跌。"
    watchlist: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
    timeframes: list[str] = Field(
        default_factory=lambda: [
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "12h",
            "1d",
            "1w",
        ]
    )
    research_bars: int = Field(default=2000, ge=500, le=20000)
    validation: ResearchValidationPreferences = Field(default_factory=ResearchValidationPreferences)

    @field_validator("watchlist")
    @classmethod
    def normalize_watchlist(cls, symbols: list[str]) -> list[str]:
        normalized = [symbol.strip().upper().replace("-", "/") for symbol in symbols]
        if not normalized or any("/" not in symbol for symbol in normalized):
            raise ValueError("watchlist must contain CCXT symbols such as BTC/USDT")
        return list(dict.fromkeys(normalized))

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip().lower() for value in values))
        if not normalized or any(value not in SUPPORTED_TIMEFRAMES for value in normalized):
            raise ValueError("unsupported strategy timeframe")
        return normalized


class UserConfiguration(BaseModel):
    exchange_id: str = "okx"
    trading_mode: TradingMode = TradingMode.PAPER
    market_type: MarketType = MarketType.SWAP
    margin_mode: MarginMode = MarginMode.CROSS
    contract_leverage: Decimal = Field(default=Decimal("3"), ge=1, le=Decimal("20"))
    default_symbol: str = "BTC/USDT"
    default_timeframe: str = "1h"
    network_proxy_url: str = ""
    paper_equity: Decimal = Field(default=Decimal("100000"), gt=0)
    fee_bps: Decimal = Field(default=Decimal("6"), ge=0, le=Decimal("100"))
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0, le=Decimal("100"))
    live_trading_enabled: bool = False
    automatic_execution_enabled: bool = False
    strategy_poll_seconds: int = Field(default=5, ge=2, le=60)
    risk: RiskPreferences = Field(default_factory=RiskPreferences)
    strategy: StrategyPreferences = Field(default_factory=StrategyPreferences)
    agent_forward_validation: AgentForwardValidationPreferences = Field(
        default_factory=AgentForwardValidationPreferences
    )

    @model_validator(mode="after")
    def validate_contract_risk(self):
        if self.market_type == MarketType.SWAP:
            if self.contract_leverage > self.risk.max_leverage:
                raise ValueError("执行杠杆不能高于风控最大杠杆")
            if self.margin_mode not in {MarginMode.CROSS, MarginMode.ISOLATED}:
                raise ValueError("永续合约保证金模式必须为 cross 或 isolated")
        return self

    @field_validator("exchange_id")
    @classmethod
    def validate_exchange(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"okx", "binance"}:
            raise ValueError("supported exchanges are okx and binance")
        return normalized

    @field_validator("default_symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper().replace("-", "/")
        if "/" not in normalized:
            raise ValueError("symbol must look like BTC/USDT")
        return normalized

    @field_validator("default_timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("unsupported timeframe")
        return value

    @field_validator("contract_leverage")
    @classmethod
    def validate_contract_leverage(cls, value: Decimal) -> Decimal:
        if value != value.to_integral_value():
            raise ValueError("contract leverage must be a whole number")
        return value

    @field_validator("network_proxy_url")
    @classmethod
    def validate_proxy_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("proxy must be an http(s) URL such as http://127.0.0.1:7890")
        if parsed.username or parsed.password:
            raise ValueError("proxy credentials must not be embedded in the URL")
        return normalized.rstrip("/")


class CredentialInput(BaseModel):
    api_key: str | None = None
    secret: str | None = None
    passphrase: str | None = None
    clear_existing: bool = False


class ConfigurationUpdate(BaseModel):
    configuration: UserConfiguration
    credentials: CredentialInput | None = None
    live_unlock_phrase: str | None = None
    promote_agent_goal_id: str | None = None


class CredentialState(BaseModel):
    api_key_configured: bool = False
    secret_configured: bool = False
    passphrase_configured: bool = False


class CommissioningResult(BaseModel):
    exchange: str
    market_type: MarketType
    symbol: str
    credential_fingerprint: str
    protection_verified: bool
    cancellation_confirmed: bool
    agent_reduction_verified: bool = False
    agent_exit_verified: bool = False
    position_flat_confirmed: bool = False
    order_ids: list[str] = Field(default_factory=list)
    message: str
    tested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> bool:
        return self.protection_verified and self.cancellation_confirmed

    @property
    def agent_position_management_passed(self) -> bool:
        return (
            self.passed
            and self.agent_reduction_verified
            and self.agent_exit_verified
            and self.position_flat_confirmed
        )


class ConfigurationView(BaseModel):
    configuration: UserConfiguration
    credentials: CredentialState
    credential_scopes: dict[str, CredentialState] = Field(default_factory=dict)
    commissioning: CommissioningResult | None = None
    execution_ready: bool = False
    promoted_agent_goal_id: str | None = None
    promoted_agent_policy_id: str | None = None
    updated_at: datetime | None = None


class ConnectionTestResult(BaseModel):
    ok: bool
    exchange: str
    mode: TradingMode
    public_market_ok: bool
    private_account_ok: bool | None = None
    protected_order_ok: bool = False
    latency_ms: int
    symbol: str
    last_price: Decimal | None = None
    candle_count: int = 0
    failure_code: str | None = None
    required_action: str | None = None
    message: str
    tested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
