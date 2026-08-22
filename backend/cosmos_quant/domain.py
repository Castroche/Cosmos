from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

Money = Annotated[Decimal, Field(decimal_places=8)]


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class MarketRegime(StrEnum):
    TREND = "trend"
    RANGE = "range"
    BREAKOUT = "breakout"
    UNCERTAIN = "uncertain"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TradingMode(StrEnum):
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE = "live"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class TrailingActivation(StrEnum):
    IMMEDIATE = "immediate"
    FIRST_TARGET = "first_target"
    ONE_R = "one_r"


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    open: Money
    high: Money
    low: Money
    close: Money
    volume: Money


class FundingRatePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    rate: Decimal
    mark_price: Money | None = None


class OpenInterestPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    open_interest_amount: Decimal | None = Field(default=None, ge=0)
    open_interest_value: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_measurement(self):
        if self.open_interest_amount is None and self.open_interest_value is None:
            raise ValueError("open-interest point requires amount or value")
        return self


class PositioningPoint(BaseModel):
    """Causal derivatives positioning observation published by an exchange."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    long_short_ratio: Decimal = Field(gt=0)
    long_account_fraction: Decimal | None = Field(default=None, ge=0, le=1)
    short_account_fraction: Decimal | None = Field(default=None, ge=0, le=1)
    source_variant: str = "global_account"

    @model_validator(mode="after")
    def validate_account_fractions(self):
        if (self.long_account_fraction is None) != (self.short_account_fraction is None):
            raise ValueError("positioning fractions must be supplied together")
        if (
            self.long_account_fraction is not None
            and self.short_account_fraction is not None
            and abs(self.long_account_fraction + self.short_account_fraction - Decimal("1"))
            > Decimal("0.001")
        ):
            raise ValueError("positioning account fractions must sum to one")
        return self


class BasisPoint(BaseModel):
    """Mark/index dislocation observed at one closed derivatives bar."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    mark_price: Money = Field(gt=0)
    index_price: Money = Field(gt=0)
    basis_rate: Decimal

    @model_validator(mode="after")
    def validate_basis_rate(self):
        expected = (self.mark_price - self.index_price) / self.index_price
        if abs(expected - self.basis_rate) > Decimal("0.00000001"):
            raise ValueError("basis rate does not match mark and index prices")
        return self


class OrderFlowBar(BaseModel):
    """Causal closed-bar taker flow from exchange-published trades or kline totals."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    aggregate_count: int = Field(ge=1)
    underlying_trade_count: int = Field(ge=1)
    base_volume: Decimal = Field(gt=0)
    quote_volume: Decimal = Field(gt=0)
    taker_buy_base_volume: Decimal = Field(ge=0)
    taker_sell_base_volume: Decimal = Field(ge=0)
    delta_base_volume: Decimal
    maximum_trade_quote: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_flow_accounting(self):
        if abs(
            self.taker_buy_base_volume + self.taker_sell_base_volume - self.base_volume
        ) > Decimal("0.00000001"):
            raise ValueError("taker buy and sell volume must equal total base volume")
        if abs(
            self.taker_buy_base_volume - self.taker_sell_base_volume - self.delta_base_volume
        ) > Decimal("0.00000001"):
            raise ValueError("order-flow delta does not match taker volumes")
        return self


class LiquidationEvent(BaseModel):
    """One exchange-published forced-liquidation snapshot."""

    model_config = ConfigDict(frozen=True)

    id: str
    exchange: str
    symbol: str
    event_time: datetime
    trade_time: datetime
    order_side: OrderSide
    liquidated_side: Side
    original_quantity: Decimal = Field(gt=0)
    last_filled_quantity: Decimal = Field(ge=0)
    accumulated_quantity: Decimal = Field(ge=0)
    price: Money = Field(ge=0)
    average_price: Money = Field(ge=0)
    quote_value: Decimal = Field(ge=0)
    order_status: str
    source_variant: str = "all_market_1000ms_snapshot"


class LiquidationFlowPoint(BaseModel):
    """Closed-hour forced-flow context with explicit cross-contract provenance."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    interval_start: datetime
    long_liquidated_contracts: Decimal = Field(ge=0)
    short_liquidated_contracts: Decimal = Field(ge=0)
    unique_event_count: int = Field(ge=0)
    source_market_type: str = "coin_m_inverse_perpetual"
    source_instrument_id: str
    evidence_role: str = "cross_contract_underlying_liquidation_context"

    @model_validator(mode="after")
    def validate_causal_availability(self):
        if self.timestamp != self.interval_start + timedelta(hours=1):
            raise ValueError("liquidation flow is available only after its hour closes")
        if self.source_market_type != "coin_m_inverse_perpetual":
            raise ValueError("historical liquidation flow source market type is unsupported")
        if self.evidence_role != "cross_contract_underlying_liquidation_context":
            raise ValueError("historical liquidation flow evidence role is unsupported")
        return self

    @computed_field
    @property
    def short_minus_long_imbalance(self) -> Decimal:
        total = self.long_liquidated_contracts + self.short_liquidated_contracts
        return (
            (self.short_liquidated_contracts - self.long_liquidated_contracts) / total
            if total > 0
            else Decimal("0")
        )


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    timestamp: datetime
    bid: Money
    ask: Money
    last: Money
    source: str = "paper"
    funding_rate: Decimal | None = None
    funding_time: datetime | None = None
    funding_interval: str | None = None
    liquidation_flow_imbalance: Decimal | None = Field(default=None, ge=-1, le=1)
    liquidation_long_contracts: Decimal | None = Field(default=None, ge=0)
    liquidation_short_contracts: Decimal | None = Field(default=None, ge=0)
    liquidation_flow_time: datetime | None = None
    liquidation_source_market_type: str | None = None
    liquidation_source_instrument_id: str | None = None
    liquidation_evidence_role: str | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None
    open_interest_change_24h: Decimal | None = None
    open_interest_time: datetime | None = None
    long_short_ratio: Decimal | None = None
    long_account_fraction: Decimal | None = None
    short_account_fraction: Decimal | None = None
    positioning_time: datetime | None = None
    basis_rate: Decimal | None = None
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    basis_time: datetime | None = None
    order_flow_delta: Decimal | None = None
    order_flow_imbalance: Decimal | None = None
    order_flow_quote_volume: Decimal | None = None
    order_flow_time: datetime | None = None

    @computed_field
    @property
    def spread_bps(self) -> Decimal:
        if self.last == 0:
            return Decimal("0")
        return ((self.ask - self.bid) / self.last * Decimal("10000")).quantize(Decimal("0.01"))


class PriceZone(BaseModel):
    lower: Money
    upper: Money

    @field_validator("upper")
    @classmethod
    def validate_upper(cls, value: Decimal, info):
        lower = info.data.get("lower")
        if lower is not None and value < lower:
            raise ValueError("upper must be greater than or equal to lower")
        return value


class EntryRule(BaseModel):
    order_type: OrderType = OrderType.LIMIT
    zone: PriceZone
    trigger: str
    expires_at: datetime | None = None
    max_slippage_bps: Decimal = Decimal("8")


class ProfitTarget(BaseModel):
    price: Money
    close_fraction: Decimal = Field(gt=0, le=1)
    rationale: str


class ExitPlan(BaseModel):
    stop_loss: Money
    take_profits: list[ProfitTarget] = Field(min_length=1)
    trailing_stop_atr: Decimal | None = Field(default=None, gt=0)
    trailing_activation: TrailingActivation = TrailingActivation.FIRST_TARGET
    break_even_after_first_target: bool = False
    maximum_holding_bars: int | None = Field(default=None, ge=1, le=2000)
    invalidation: str

    @field_validator("take_profits")
    @classmethod
    def validate_fractions(cls, targets: list[ProfitTarget]):
        total = sum((target.close_fraction for target in targets), Decimal("0"))
        if total != Decimal("1"):
            raise ValueError("take-profit fractions must sum to 1")
        return targets

    def trailing_is_active(
        self,
        *,
        targets_reached: int,
        favorable_move: Decimal,
        initial_risk: Decimal,
    ) -> bool:
        if self.trailing_stop_atr is None:
            return False
        if self.trailing_activation == TrailingActivation.IMMEDIATE:
            return True
        if self.trailing_activation == TrailingActivation.FIRST_TARGET:
            return targets_reached >= 1
        return initial_risk > 0 and favorable_move >= initial_risk

    def time_exit_is_due(self, *, held_bars: int) -> bool:
        return bool(
            self.maximum_holding_bars is not None
            and held_bars >= self.maximum_holding_bars
        )


class PositionSizing(BaseModel):
    risk_fraction: Decimal = Field(gt=0, le=Decimal("0.05"))
    risk_amount: Money
    suggested_quantity: Money
    notional: Money


class StrategySignal(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    algorithm: str
    symbol: str
    timeframe: str
    direction: SignalDirection
    regime: MarketRegime
    confidence: Decimal = Field(ge=0, le=1)
    signal_candle_at: datetime
    data_as_of: datetime
    reasons: list[str] = Field(min_length=1)
    features: dict[str, Decimal | str | bool] = Field(default_factory=dict)

    @computed_field
    @property
    def actionable(self) -> bool:
        return self.direction != SignalDirection.FLAT


class StrategyPlan(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    lineage_id: str | None = None
    supersedes_plan_id: str | None = None
    name: str
    version: int = 1
    algorithm: str = "trend"
    source_prompt: str = ""
    exchange: str
    trading_mode: TradingMode = TradingMode.PAPER
    market_type: str = "spot"
    margin_mode: str = "cash"
    leverage: Decimal = Field(default=Decimal("1"), ge=1, le=Decimal("100"))
    symbol: str
    timeframe: str
    side: Side
    status: PlanStatus = PlanStatus.DRAFT
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    signal_candle_at: datetime | None = None
    data_as_of: datetime | None = None
    regime: MarketRegime = MarketRegime.UNCERTAIN
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    strategy_spec: dict[str, Any] = Field(default_factory=dict)
    strategy_spec_fingerprint: str = ""
    research_candidate_id: str | None = None
    validated_strategy_artifact: dict[str, Any] | None = None
    research_campaign_id: str | None = None
    campaign_certificate_fingerprint: str | None = None
    causal_policy_fingerprint: str | None = None
    causal_policy_selected_library_entry_id: str | None = None
    causal_policy_selection_evidence: dict[str, Any] | None = None
    entry: EntryRule
    exit: ExitPlan
    sizing: PositionSizing
    minimum_reward_risk: Decimal = Field(ge=1)
    thesis: str
    reasons: list[str] = Field(min_length=1)
    pause_conditions: list[str] = Field(default_factory=list)
    snapshot: MarketSnapshot
    approved_at: datetime | None = None
    approved_fingerprint: str | None = None
    decision_origin: str = "operator"
    agent_goal_id: str | None = None
    agent_run_id: str | None = None
    agent_policy_id: str | None = None
    agent_policy_fingerprint: str | None = None

    @computed_field
    @property
    def fingerprint(self) -> str:
        excluded = {"status", "approved_at", "approved_fingerprint"}
        # Preserve fingerprints of plans created before research provenance existed.
        if self.research_candidate_id is None:
            excluded.add("research_candidate_id")
        if self.validated_strategy_artifact is None:
            excluded.add("validated_strategy_artifact")
        if self.research_campaign_id is None:
            excluded.add("research_campaign_id")
        if self.campaign_certificate_fingerprint is None:
            excluded.add("campaign_certificate_fingerprint")
        if self.causal_policy_fingerprint is None:
            excluded.add("causal_policy_fingerprint")
        if self.causal_policy_selected_library_entry_id is None:
            excluded.add("causal_policy_selected_library_entry_id")
        if self.causal_policy_selection_evidence is None:
            excluded.add("causal_policy_selection_evidence")
        exit_excluded: dict[str, bool] = {}
        if self.exit.maximum_holding_bars is None:
            exit_excluded["maximum_holding_bars"] = True
        if not self.exit.break_even_after_first_target:
            exit_excluded["break_even_after_first_target"] = True
        if exit_excluded:
            nested_excluded = {item: True for item in excluded}
            nested_excluded["exit"] = exit_excluded
            payload = self.model_dump_json(
                exclude=nested_excluded,
                exclude_computed_fields=True,
            )
        else:
            payload = self.model_dump_json(
                exclude=excluded,
                exclude_computed_fields=True,
            )
        return sha256(payload.encode()).hexdigest()[:16]

    @computed_field
    @property
    def human_summary(self) -> str:
        direction = "做多" if self.side == Side.LONG else "做空"
        targets = "、".join(f"{target.price:f}" for target in self.exit.take_profits)
        holding = (
            f"；最多持有 {self.exit.maximum_holding_bars} 根已收盘 K 线"
            if self.exit.maximum_holding_bars is not None
            else ""
        )
        break_even = "；首个止盈后剩余仓位保本" if self.exit.break_even_after_first_target else ""
        return (
            f"{self.symbol} 在 {self.entry.zone.lower:f}–{self.entry.zone.upper:f} {direction}；"
            f"止损 {self.exit.stop_loss:f}；止盈 {targets}；"
            f"建议数量 {self.sizing.suggested_quantity:f}，最大计划亏损 "
            f"{self.sizing.risk_amount:f} USDT{holding}{break_even}。"
        )


class OrderIntent(BaseModel):
    idempotency_key: str
    plan_id: str
    plan_fingerprint: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Money
    limit_price: Money | None = None
    reduce_only: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Order(BaseModel):
    id: str
    client_order_id: str
    plan_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    quantity: Money
    filled_quantity: Money = Decimal("0")
    limit_price: Money | None = None
    average_fill_price: Money | None = None
    fee: Money = Decimal("0")
    mode: TradingMode = TradingMode.PAPER
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Fill(BaseModel):
    id: str
    order_id: str
    client_order_id: str = ""
    plan_id: str | None = None
    symbol: str
    side: OrderSide
    quantity: Money
    price: Money
    fee: Money = Decimal("0")
    fee_currency: str = "USDT"
    timestamp: datetime


class Position(BaseModel):
    symbol: str
    side: Side
    quantity: Money
    average_entry: Money
    mark_price: Money
    unrealized_pnl: Money = Decimal("0")
    stop_loss: Money | None = None
    take_profit: Money | None = None
    leverage: Decimal | None = None
    liquidation_price: Money | None = None
    margin_mode: str | None = None
    margin_used: Money | None = None


class PortfolioState(BaseModel):
    equity: Money
    cash: Money
    daily_start_equity: Money
    gross_exposure: Money
    available_margin: Money | None = None
    used_margin: Money = Decimal("0")
    open_positions: list[Position] = Field(default_factory=list)

    @computed_field
    @property
    def margin_ratio(self) -> Decimal:
        if self.equity <= 0:
            return Decimal("0")
        return (self.used_margin / self.equity).quantize(Decimal("0.0001"))

    @computed_field
    @property
    def daily_drawdown_fraction(self) -> Decimal:
        if self.daily_start_equity <= 0:
            return Decimal("0")
        value = (self.daily_start_equity - self.equity) / self.daily_start_equity
        return max(value, Decimal("0"))


class AuditEvent(BaseModel):
    id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    category: str
    severity: str = "info"
    message: str
    data: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
