from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

V2_SYMBOLS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")
V2_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d")
NO_TRADE_MAX_SILENCE = timedelta(minutes=60)
WAIT_MAX_SILENCE = timedelta(hours=2)


def utc_now() -> datetime:
    return datetime.now(UTC)


def content_hash(value: BaseModel | dict[str, Any]) -> str:
    payload = (
        value.model_dump(
            mode="json",
            exclude_none=True,
            exclude_computed_fields=True,
        )
        if isinstance(value, BaseModel)
        else value
    )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode()).hexdigest()


class FeatureHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class MarketRegimeV2(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    STRONG_TREND_UP = "STRONG_TREND_UP"
    STRONG_TREND_DOWN = "STRONG_TREND_DOWN"
    RANGE = "RANGE"
    COMPRESSION = "COMPRESSION"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    PANIC = "PANIC"
    REVERSAL = "REVERSAL"
    TRANSITION = "TRANSITION"


class DecisionAction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    MANAGE_EXISTING = "MANAGE_EXISTING"
    EXIT = "EXIT"


class NoTradeBlocker(StrEnum):
    """The bounded reasons that may justify declining to create a trade plan."""

    DATA_UNHEALTHY = "DATA_UNHEALTHY"
    OFFICIAL_RISK_WINDOW = "OFFICIAL_RISK_WINDOW"
    LIQUIDITY_UNSAFE = "LIQUIDITY_UNSAFE"
    NO_VALID_STOP = "NO_VALID_STOP"
    NO_MINIMUM_REWARD_RISK = "NO_MINIMUM_REWARD_RISK"
    ORDER_POSITION_UNCERTAIN = "ORDER_POSITION_UNCERTAIN"
    QUANT_LIVE_GATE = "QUANT_LIVE_GATE"
    NON_DIRECTIONAL_STATE = "NON_DIRECTIONAL_STATE"


class ThesisState(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class PlanState(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    WAITING_TRIGGER = "WAITING_TRIGGER"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"
    CANCELLED = "CANCELLED"


class PositionStateV2(StrEnum):
    FLAT = "FLAT"
    OPENING = "OPENING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    OPEN = "OPEN"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class WaitRuntimeStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FIRED = "FIRED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class AutomationRuntimeStatus(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class WakeAction(StrEnum):
    RECALL_LLM = "RECALL_LLM"
    EXECUTE_ENTRY = "EXECUTE_ENTRY"
    CANCEL_PLAN = "CANCEL_PLAN"
    EXIT_POSITION = "EXIT_POSITION"


class WakeKind(StrEnum):
    PRICE_CROSSES_ABOVE = "PRICE_CROSSES_ABOVE"
    PRICE_CROSSES_BELOW = "PRICE_CROSSES_BELOW"
    PRICE_IN_ZONE = "PRICE_IN_ZONE"
    CANDLE_CLOSE_ABOVE = "CANDLE_CLOSE_ABOVE"
    CANDLE_CLOSE_BELOW = "CANDLE_CLOSE_BELOW"
    METRIC_GT = "METRIC_GT"
    METRIC_LT = "METRIC_LT"
    METRIC_CROSSES_ABOVE = "METRIC_CROSSES_ABOVE"
    METRIC_CROSSES_BELOW = "METRIC_CROSSES_BELOW"
    METRIC_IN_RANGE = "METRIC_IN_RANGE"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
    REGIME_CHANGED = "REGIME_CHANGED"
    EXPIRES_AT = "EXPIRES_AT"
    ORDER_EVENT = "ORDER_EVENT"
    POSITION_EVENT = "POSITION_EVENT"
    EXTERNAL_EVENT = "EXTERNAL_EVENT"
    ALL = "ALL"
    ANY = "ANY"


REGISTERED_EXTERNAL_WAKE_EVENTS = frozenset(
    {
        # This event is emitted after each persisted 5m Quant v1 inference.
        # Official macro/exchange events use the dedicated MAJOR_MARKET_EVENT
        # dispatcher and therefore are deliberately not accepted as arbitrary
        # EXTERNAL_EVENT values here.
        "QUANT_PREDICTION_UPDATED",
    }
)

MARKET_DERIVED_WAKE_KINDS = frozenset(
    {
        WakeKind.PRICE_CROSSES_ABOVE,
        WakeKind.PRICE_CROSSES_BELOW,
        WakeKind.PRICE_IN_ZONE,
        WakeKind.CANDLE_CLOSE_ABOVE,
        WakeKind.CANDLE_CLOSE_BELOW,
        WakeKind.METRIC_GT,
        WakeKind.METRIC_LT,
        WakeKind.METRIC_CROSSES_ABOVE,
        WakeKind.METRIC_CROSSES_BELOW,
        WakeKind.METRIC_IN_RANGE,
        WakeKind.STRUCTURE_CHANGED,
        WakeKind.REGIME_CHANGED,
    }
)


class TradingModeV2(StrEnum):
    SHADOW = "SHADOW"
    OKX_DEMO = "OKX_DEMO"
    LIVE_AUTHORIZED = "LIVE_AUTHORIZED"


class FeatureGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    health: FeatureHealth
    values: dict[str, Decimal | str | bool | int | None] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def unavailable_has_no_values(self) -> Self:
        if self.health == FeatureHealth.UNAVAILABLE and any(
            value is not None for value in self.values.values()
        ):
            raise ValueError("unavailable feature groups cannot contain synthetic values")
        if self.health != FeatureHealth.HEALTHY and not self.reasons:
            raise ValueError("non-healthy feature groups require a reason")
        return self


class MarketStateV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[2] = 2
    state_id: str = Field(default_factory=lambda: f"ms_{uuid4().hex}")
    exchange: Literal["okx"] = "okx"
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    data_as_of: datetime
    created_at: datetime = Field(default_factory=utc_now)
    regime: MarketRegimeV2
    market: FeatureGroup
    structure: FeatureGroup
    trend: FeatureGroup
    momentum: FeatureGroup
    volatility: FeatureGroup
    volume: FeatureGroup
    microstructure: FeatureGroup
    derivatives: FeatureGroup
    cross_market: FeatureGroup
    events: FeatureGroup
    timeframe_features: dict[
        Literal["1m", "5m", "15m", "1h", "4h", "1d"], dict[str, Decimal | str | bool | int | None]
    ] = Field(default_factory=dict)

    @field_validator("data_as_of")
    @classmethod
    def canonical_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("MarketState timestamp must include a timezone")
        return value.astimezone(UTC)

    @computed_field
    @property
    def state_hash(self) -> str:
        return content_hash(
            self.model_dump(
                mode="json",
                exclude={"created_at", "state_id"},
                exclude_none=True,
                exclude_computed_fields=True,
            )
        )

    @computed_field
    @property
    def healthy_for_new_risk(self) -> bool:
        required = (self.market, self.structure, self.trend, self.volatility, self.microstructure)
        return all(group.health == FeatureHealth.HEALTHY for group in required)


def validate_market_state_time(
    state: MarketStateV2, *, max_age: timedelta | None = timedelta(minutes=10),
) -> None:
    if state.data_as_of.tzinfo is None or state.data_as_of.utcoffset() is None:
        raise ValueError("MarketState timestamp must include a timezone")
    age = datetime.now(UTC) - state.data_as_of
    if age < -timedelta(seconds=30):
        raise ValueError("MarketState timestamp is in the future")
    if max_age is not None and age > max_age:
        raise ValueError("MarketState is stale")


class DeltaContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior_state_hash: str | None = None
    current_state_hash: str
    changed_paths: list[str] = Field(default_factory=list)
    material_changes: list[str] = Field(default_factory=list)
    trigger_type: str | None = None
    trigger_evidence: dict[str, Any] | None = None


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    reference: str
    data_as_of: datetime | None = None
    hash: str | None = None


class MarketAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(default_factory=lambda: f"analysis_{uuid4().hex}")
    market_state_hash: str
    data_as_of: datetime
    regime: MarketRegimeV2
    summary: str = Field(min_length=1, max_length=4000)
    bullish_evidence: list[str] = Field(default_factory=list)
    bearish_evidence: list[str] = Field(default_factory=list)
    invalidation_evidence: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class ActiveThesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis_id: str = Field(default_factory=lambda: f"thesis_{uuid4().hex}")
    version: int = Field(default=1, ge=1)
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    direction: Literal["LONG", "SHORT", "NEUTRAL"]
    statement: str = Field(min_length=1, max_length=4000)
    supporting_evidence: list[EvidenceRef] = Field(min_length=1)
    invalidation_rules: list[str] = Field(min_length=1)
    state: ThesisState = ThesisState.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime

    @model_validator(mode="after")
    def valid_window(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("thesis expiry must be after creation")
        return self


class WakeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition_id: str = Field(default_factory=lambda: f"wake_{uuid4().hex}")
    kind: WakeKind
    action: WakeAction
    path: str | None = None
    value: Decimal | str | bool | datetime | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    timeframe: Literal["1m", "5m", "15m", "1h", "4h", "1d"] | None = None
    children: list[WakeCondition] = Field(default_factory=list)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.kind in {WakeKind.ALL, WakeKind.ANY}:
            if len(self.children) < 2:
                raise ValueError("ALL/ANY requires at least two child conditions")
            if any(child.action != self.action for child in self.children):
                raise ValueError("composite child actions must match the parent action")
            return self
        if self.children:
            raise ValueError("only ALL/ANY can contain child conditions")
        if (
            self.kind
            in {
                WakeKind.PRICE_CROSSES_ABOVE,
                WakeKind.PRICE_CROSSES_BELOW,
                WakeKind.CANDLE_CLOSE_ABOVE,
                WakeKind.CANDLE_CLOSE_BELOW,
                WakeKind.METRIC_GT,
                WakeKind.METRIC_LT,
                WakeKind.METRIC_CROSSES_ABOVE,
                WakeKind.METRIC_CROSSES_BELOW,
            }
            and self.value is None
        ):
            raise ValueError(f"{self.kind} requires value")
        if self.kind in {WakeKind.PRICE_IN_ZONE, WakeKind.METRIC_IN_RANGE} and (
            self.lower is None or self.upper is None or self.lower > self.upper
        ):
            raise ValueError(f"{self.kind} requires an ordered lower/upper range")
        if (
            self.kind
            in {
                WakeKind.METRIC_GT,
                WakeKind.METRIC_LT,
                WakeKind.METRIC_CROSSES_ABOVE,
                WakeKind.METRIC_CROSSES_BELOW,
                WakeKind.METRIC_IN_RANGE,
            }
            and not self.path
        ):
            raise ValueError(f"{self.kind} requires a metric path")
        if (
            self.kind in {WakeKind.CANDLE_CLOSE_ABOVE, WakeKind.CANDLE_CLOSE_BELOW}
            and not self.timeframe
        ):
            raise ValueError("candle close conditions require a timeframe")
        return self


class PriceZone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lower: Decimal = Field(gt=0)
    upper: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.lower > self.upper:
            raise ValueError("price zone lower must not exceed upper")
        return self


class TakeProfitRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(gt=0)
    close_fraction: Decimal = Field(gt=0, le=1)
    rationale: str = Field(min_length=1, max_length=500)


class PositionManagementPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hard_stop: Decimal = Field(gt=0)
    take_profits: list[TakeProfitRule] = Field(min_length=1)
    break_even_after_target: int | None = Field(default=None, ge=1)
    atr_trailing_multiple: Decimal | None = Field(default=None, gt=0)
    maximum_holding_minutes: int | None = Field(default=None, ge=1)
    exit_wake_conditions: list[WakeCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def fractions_are_bounded(self) -> Self:
        if sum((item.close_fraction for item in self.take_profits), Decimal("0")) > 1:
            raise ValueError("take-profit fractions cannot exceed one")
        if any(item.action != WakeAction.EXIT_POSITION for item in self.exit_wake_conditions):
            raise ValueError("management exit conditions must use EXIT_POSITION")
        return self


class TradePlan(BaseModel):
    """LLM-authored intent. Position size, risk fraction and leverage are deliberately absent."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: f"plan_{uuid4().hex}")
    version: int = Field(default=1, ge=1)
    thesis_id: str
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    side: Literal["LONG", "SHORT"]
    setup: str = "unspecified"
    entry_zone: PriceZone
    order_type: Literal["MARKET", "LIMIT"] | None = None
    limit_price: Decimal | None = Field(default=None, gt=0)
    reference_price: Decimal | None = Field(default=None, gt=0)
    valid_price_range: PriceZone | None = None
    position_id: str | None = Field(default=None, min_length=1)
    position_version: int | None = Field(default=None, ge=1)
    entry_conditions: list[WakeCondition] = Field(default_factory=list)
    management: PositionManagementPlan
    minimum_reward_risk: Decimal = Field(ge=1)
    rationale: list[str] = Field(min_length=1)
    state: PlanState = PlanState.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime

    @property
    def execution_price(self) -> Decimal:
        if self.order_type == "LIMIT" and self.limit_price is not None:
            return self.limit_price
        if self.order_type == "MARKET" and self.reference_price is not None:
            return self.reference_price
        return (self.entry_zone.lower + self.entry_zone.upper) / Decimal("2")

    @model_validator(mode="after")
    def entry_actions(self) -> Self:
        if (self.position_id is None) != (self.position_version is None):
            raise ValueError("position_id and position_version must be supplied together")
        if self.order_type == "LIMIT":
            if self.limit_price is None or self.valid_price_range is None:
                raise ValueError("LIMIT requires limit_price and valid_price_range")
            if not self.valid_price_range.lower <= self.limit_price <= self.valid_price_range.upper:
                raise ValueError("LIMIT price must be inside valid_price_range")
        elif self.order_type == "MARKET":
            if self.reference_price is None:
                raise ValueError("MARKET requires reference_price")
            if self.limit_price is not None:
                raise ValueError("MARKET cannot specify limit_price")
        elif any(value is not None for value in (
            self.limit_price, self.reference_price, self.valid_price_range,
        )):
            raise ValueError("explicit order prices require order_type")
        if self.order_type is None and not self.entry_conditions:
            raise ValueError("legacy trade plans require entry_conditions")
        if self.order_type is not None and self.entry_conditions:
            raise ValueError("immediate trade plans cannot have entry wake conditions")
        if any(item.action != WakeAction.EXECUTE_ENTRY for item in self.entry_conditions):
            raise ValueError("trade entry conditions must use EXECUTE_ENTRY")
        if self.expires_at <= self.created_at:
            raise ValueError("trade plan expiry must be after creation")
        stop = self.management.hard_stop
        targets = [item.price for item in self.management.take_profits]
        lower = self.execution_price if self.order_type else self.entry_zone.lower
        upper = self.execution_price if self.order_type else self.entry_zone.upper
        if self.side == "LONG" and (
            stop >= lower or min(targets) <= upper
        ):
            raise ValueError("LONG stop must be below entry and targets above entry")
        if self.side == "SHORT" and (
            stop <= upper or max(targets) >= lower
        ):
            raise ValueError("SHORT stop must be above entry and targets below entry")

        # A TradePlan is the single source of truth for entry validity.  Older
        # LLM outputs could give a short-lived WakeCondition inside a longer
        # lived plan, leaving a plan displayed as WAITING_TRIGGER even though
        # its entry could never fire.  Entry rules therefore inherit the plan
        # expiry, including nested rules, whenever a plan is created or loaded.
        def align_expiry(condition: WakeCondition) -> WakeCondition:
            children = [align_expiry(child) for child in condition.children]
            return condition.model_copy(
                update={"children": children, "expires_at": self.expires_at}
            )

        self.entry_conditions = [align_expiry(item) for item in self.entry_conditions]
        return self


class WaitPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thesis_id: str | None = None
    reason: str = Field(min_length=1, max_length=2000)
    wake_conditions: list[WakeCondition] = Field(min_length=1)
    expires_at: datetime

    @model_validator(mode="after")
    def recall_only(self) -> Self:
        if any(item.action != WakeAction.RECALL_LLM for item in self.wake_conditions):
            raise ValueError("wait conditions must use RECALL_LLM")
        return self


class WaitRuntimeV2(BaseModel):
    """Durable WAIT/NO_TRADE wake state restored independently of an LLM process."""

    model_config = ConfigDict(extra="forbid")

    wait_runtime_id: str = Field(default_factory=lambda: f"waitrt_{uuid4().hex}")
    version: int = Field(default=1, ge=1)
    episode_id: str
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    wait_plan: WaitPlan
    status: WaitRuntimeStatus = WaitRuntimeStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AutomationRuntimeV2(BaseModel):
    """Persisted operator boundary for autonomous Shadow/OKX Demo execution."""

    model_config = ConfigDict(extra="forbid")

    runtime_id: Literal["trading-intelligence-v2"] = "trading-intelligence-v2"
    enabled: bool = False
    requested_mode: Literal[TradingModeV2.SHADOW, TradingModeV2.OKX_DEMO] = (
        TradingModeV2.OKX_DEMO
    )
    symbols: list[
        Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    ] = Field(default_factory=lambda: list(V2_SYMBOLS), min_length=1)
    status: AutomationRuntimeStatus = AutomationRuntimeStatus.STOPPED
    consecutive_failures: int = Field(default=0, ge=0)
    last_cycle_at: datetime | None = None
    last_decision_at: datetime | None = None
    last_execution_at: datetime | None = None
    last_reconciliation_at: datetime | None = None
    last_error_code: str | None = None
    last_error_hash: str | None = None
    next_retry_at: datetime | None = None
    symbol_failures: dict[str, int] = Field(default_factory=dict)
    symbol_next_retry_at: dict[str, datetime] = Field(default_factory=dict)
    symbol_error_codes: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class PortfolioTraderDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(default_factory=lambda: f"decision_{uuid4().hex}")
    action: DecisionAction
    rationale: list[str] = Field(min_length=1)
    thesis: ActiveThesis | None = None
    trade_plan: TradePlan | None = None
    wait_plan: WaitPlan | None = None
    management_plan: PositionManagementPlan | None = None
    position_id: str | None = Field(default=None, min_length=1)
    position_version: int | None = Field(default=None, ge=1)
    invalidation_evidence: list[EvidenceRef] = Field(default_factory=list)
    hard_blockers: list[NoTradeBlocker] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if (self.position_id is None) != (self.position_version is None):
            raise ValueError("position_id and position_version must be supplied together")
        if self.trade_plan and self.position_id is not None and (
            self.position_id != self.trade_plan.position_id
            or self.position_version != self.trade_plan.position_version
        ):
            raise ValueError("decision and trade plan position targets disagree")
        if self.action in {DecisionAction.LONG, DecisionAction.SHORT}:
            if not self.thesis or not self.trade_plan:
                raise ValueError("LONG/SHORT requires both thesis and complete trade plan")
            if self.trade_plan.side != self.action:
                raise ValueError("decision action and trade plan side disagree")
            if self.trade_plan.thesis_id != self.thesis.thesis_id:
                raise ValueError("trade plan must reference the returned thesis")
        elif self.action == DecisionAction.WAIT:
            if not self.wait_plan:
                raise ValueError("WAIT requires a wait plan")
            if self.thesis and self.wait_plan.thesis_id != self.thesis.thesis_id:
                raise ValueError("WAIT references a different thesis")
        elif self.action == DecisionAction.NO_TRADE:
            if not self.wait_plan or not self.wait_plan.wake_conditions:
                raise ValueError("NO_TRADE requires at least one executable wake condition")
            if self.thesis or self.wait_plan.thesis_id:
                raise ValueError("NO_TRADE cannot retain or reference an active thesis")
        elif self.action == DecisionAction.MANAGE_EXISTING and not self.management_plan:
            raise ValueError("MANAGE_EXISTING requires machine-executable management rules")
        elif self.action == DecisionAction.EXIT and not self.invalidation_evidence:
            raise ValueError("EXIT requires explicit invalidation evidence")
        if self.action != DecisionAction.NO_TRADE and self.hard_blockers:
            raise ValueError("hard blockers are only valid for NO_TRADE")
        return self


def iter_wake_condition_leaves(condition: WakeCondition):
    """Yield the executable leaves of a bounded WakeCondition tree."""

    if condition.kind in {WakeKind.ALL, WakeKind.ANY}:
        for child in condition.children:
            yield from iter_wake_condition_leaves(child)
        return
    yield condition


def wait_plan_liveness_errors(
    wait_plan: WaitPlan,
    *,
    action: DecisionAction | str,
    data_as_of: datetime,
) -> list[str]:
    """Return semantic errors that would make a new wait unreachable or stale.

    Persisted legacy waits remain parseable so the runtime can invalidate and
    recover them safely. New LLM decisions must pass this function before they
    can replace the active durable wait.
    """

    action_value = DecisionAction(action)
    leaves = [
        leaf
        for condition in wait_plan.wake_conditions
        for leaf in iter_wake_condition_leaves(condition)
    ]
    errors: list[str] = []
    unreachable_events = sorted(
        {
            str(leaf.value or "").upper()
            for leaf in leaves
            if leaf.kind == WakeKind.EXTERNAL_EVENT
            and str(leaf.value or "").upper() not in REGISTERED_EXTERNAL_WAKE_EVENTS
        }
    )
    if unreachable_events:
        errors.append(
            "UNREACHABLE_EXTERNAL_EVENT:" + ",".join(unreachable_events)
        )
    if wait_plan.expires_at <= data_as_of:
        errors.append("WAIT_PLAN_ALREADY_EXPIRED")
    maximum_silence = (
        NO_TRADE_MAX_SILENCE
        if action_value == DecisionAction.NO_TRADE
        else WAIT_MAX_SILENCE
    )
    if wait_plan.expires_at > data_as_of + maximum_silence:
        errors.append(
            "WAIT_PLAN_MAX_SILENCE_EXCEEDED:"
            f"{int(maximum_silence.total_seconds() // 60)}m"
        )
    if action_value == DecisionAction.NO_TRADE and not any(
        leaf.kind in MARKET_DERIVED_WAKE_KINDS for leaf in leaves
    ):
        errors.append("NO_TRADE_REQUIRES_MARKET_DERIVED_WAKE")
    return errors


def normalize_wait_plan_liveness(
    wait_plan: WaitPlan,
    *,
    action: DecisionAction | str,
    data_as_of: datetime,
) -> tuple[WaitPlan, list[str]]:
    """Apply bounded, non-trading repairs to an LLM-authored wait plan.

    This function never invents an entry, direction, size, leverage, stop, or
    target. It only guarantees that a non-trading wait can be reached again.
    """

    action_value = DecisionAction(action)
    maximum_silence = (
        NO_TRADE_MAX_SILENCE
        if action_value == DecisionAction.NO_TRADE
        else WAIT_MAX_SILENCE
    )
    deadline = data_as_of + maximum_silence
    changes: list[str] = []

    def sanitize(condition: WakeCondition) -> WakeCondition | None:
        if condition.kind == WakeKind.EXTERNAL_EVENT:
            event_name = str(condition.value or "").upper()
            if event_name not in REGISTERED_EXTERNAL_WAKE_EVENTS:
                changes.append(f"REMOVED_UNREACHABLE_EXTERNAL_EVENT:{event_name}")
                return None
        if condition.expires_at is not None and condition.expires_at <= data_as_of:
            changes.append(f"REMOVED_EXPIRED_CONDITION:{condition.condition_id}")
            return None
        condition_expiry = (
            min(condition.expires_at, deadline)
            if condition.expires_at is not None
            else None
        )
        if condition_expiry != condition.expires_at:
            changes.append(f"CLAMPED_CONDITION_EXPIRY:{condition.condition_id}")
        if condition.kind not in {WakeKind.ALL, WakeKind.ANY}:
            return condition.model_copy(update={"expires_at": condition_expiry})
        children = [
            child
            for source in condition.children
            if (child := sanitize(source)) is not None
        ]
        if not children:
            changes.append(f"REMOVED_EMPTY_COMPOSITE:{condition.condition_id}")
            return None
        if len(children) == 1:
            changes.append(f"COLLAPSED_COMPOSITE:{condition.condition_id}")
            return children[0]
        return condition.model_copy(
            update={"children": children, "expires_at": condition_expiry}
        )

    conditions = [
        condition
        for source in wait_plan.wake_conditions
        if (condition := sanitize(source)) is not None
    ]
    leaves = [
        leaf for condition in conditions for leaf in iter_wake_condition_leaves(condition)
    ]
    if action_value == DecisionAction.NO_TRADE and not any(
        leaf.kind in MARKET_DERIVED_WAKE_KINDS for leaf in leaves
    ):
        conditions.append(
            WakeCondition(
                kind=WakeKind.REGIME_CHANGED,
                action=WakeAction.RECALL_LLM,
                path="regime",
                expires_at=deadline,
            )
        )
        changes.append("ADDED_NO_TRADE_REGIME_WAKE")
    elif not conditions:
        conditions.append(
            WakeCondition(
                kind=WakeKind.REGIME_CHANGED,
                action=WakeAction.RECALL_LLM,
                path="regime",
                expires_at=deadline,
            )
        )
        changes.append("ADDED_WAIT_REGIME_WAKE")
    normalized_expiry = min(max(wait_plan.expires_at, data_as_of), deadline)
    if normalized_expiry <= data_as_of:
        normalized_expiry = deadline
    if normalized_expiry != wait_plan.expires_at:
        changes.append(
            "CLAMPED_WAIT_EXPIRY:"
            f"{int(maximum_silence.total_seconds() // 60)}m"
        )
    reason = wait_plan.reason
    if changes and "[LIVENESS_NORMALIZED]" not in reason:
        reason = f"[LIVENESS_NORMALIZED] {reason}"[:2000]
    normalized = WaitPlan(
        thesis_id=wait_plan.thesis_id,
        reason=reason,
        wake_conditions=conditions,
        expires_at=normalized_expiry,
    )
    return normalized, changes


class QuantPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prediction_id: str = Field(default_factory=lambda: f"quant_{uuid4().hex}")
    model_fingerprint: str
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    timeframe: Literal["5m"] = "5m"
    data_as_of: datetime
    expires_at: datetime
    probability_long_tp_first: Decimal = Field(ge=0, le=1)
    probability_short_tp_first: Decimal = Field(ge=0, le=1)
    probability_timeout: Decimal = Field(ge=0, le=1)
    expected_return_after_cost: Decimal
    expected_mfe: Decimal
    expected_mae: Decimal
    calibrated: bool

    @model_validator(mode="after")
    def probabilities_and_ttl(self) -> Self:
        total = (
            self.probability_long_tp_first
            + self.probability_short_tp_first
            + self.probability_timeout
        )
        if abs(total - Decimal("1")) > Decimal("0.0001"):
            raise ValueError("quant class probabilities must sum to one")
        if self.expires_at <= self.data_as_of or self.expires_at > self.data_as_of + timedelta(
            minutes=10
        ):
            raise ValueError("quant prediction TTL must be positive and at most ten minutes")
        return self

    def supports(self, side: Literal["LONG", "SHORT"], *, now: datetime | None = None) -> bool:
        current = now or utc_now()
        directional = (
            self.probability_long_tp_first if side == "LONG" else self.probability_short_tp_first
        )
        opposing = (
            self.probability_short_tp_first if side == "LONG" else self.probability_long_tp_first
        )
        return (
            current <= self.expires_at
            and self.expected_return_after_cost > 0
            and directional > opposing
        )


class RiskAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allocation_id: str = Field(default_factory=lambda: f"risk_{uuid4().hex}")
    plan_id: str
    equity: Decimal = Field(gt=0)
    risk_amount: Decimal = Field(gt=0)
    # Extra stop risk consumed while an add changes the pooled position's
    # management rule.  It is reserved until the add is accounted or rejected.
    existing_stop_risk_increase: Decimal = Field(default=Decimal("0"), ge=0)
    quantity: Decimal = Field(gt=0)
    notional: Decimal = Field(gt=0)
    leverage: Decimal = Field(ge=1)
    constraints: list[str] = Field(min_length=1)
    configuration_fingerprint: str
    created_at: datetime = Field(default_factory=utc_now)


class PositionRuntimeV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str = Field(default_factory=lambda: f"position_{uuid4().hex}")
    plan_id: str
    episode_id: str | None = None
    plan_ids: list[str] = Field(default_factory=list)
    episode_ids: list[str] = Field(default_factory=list)
    entry_filled_quantities: dict[str, Decimal] = Field(default_factory=dict)
    management_quantity: Decimal | None = Field(default=None, gt=0)
    management_started_at: datetime | None = None
    management_version: int = Field(default=1, ge=1)
    entry_order_ids: list[str] = Field(default_factory=list)
    exit_order_ids: list[str] = Field(default_factory=list)
    version: int = Field(default=1, ge=1)
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    side: Literal["LONG", "SHORT"]
    state: PositionStateV2 = PositionStateV2.FLAT
    original_quantity: Decimal = Field(gt=0)
    remaining_quantity: Decimal = Field(ge=0)
    average_entry: Decimal = Field(gt=0)
    initial_stop: Decimal = Field(gt=0)
    current_stop: Decimal = Field(gt=0)
    targets_reached: int = Field(default=0, ge=0)
    highest_mark: Decimal | None = Field(default=None, gt=0)
    lowest_mark: Decimal | None = Field(default=None, gt=0)
    accumulated_fees: Decimal = Field(default=Decimal("0"), ge=0)
    exit_requested_at: datetime | None = None
    exit_reason: str | None = None
    opened_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def bounded_quantity(self) -> Self:
        if self.remaining_quantity > self.original_quantity:
            raise ValueError("remaining quantity cannot exceed original quantity")
        if any(quantity < 0 for quantity in self.entry_filled_quantities.values()):
            raise ValueError("entry filled quantities cannot be negative")
        return self


class HumanLiveGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(default_factory=lambda: f"grant_{uuid4().hex}")
    exchange: Literal["okx"] = "okx"
    symbols: list[Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]] = Field(min_length=1)
    max_risk_per_trade: Decimal = Field(gt=0, le=Decimal("0.03"))
    max_portfolio_risk: Decimal = Field(gt=0, le=Decimal("0.10"))
    max_leverage: Decimal = Field(ge=1, le=Decimal("20"))
    max_daily_drawdown: Decimal = Field(gt=0, le=Decimal("0.10"))
    max_open_positions: int = Field(ge=1, le=20)
    configuration_fingerprint: str
    issued_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(hours=24))
    risk_confirmation_one: bool
    risk_confirmation_two: bool
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_grant(self) -> Self:
        if not self.risk_confirmation_one or not self.risk_confirmation_two:
            raise ValueError("live grant requires two independent risk confirmations")
        if self.expires_at <= self.issued_at or self.expires_at > self.issued_at + timedelta(
            days=7
        ):
            raise ValueError("live grant duration must be positive and no more than seven days")
        return self

    def permits(
        self, *, symbol: str, configuration_fingerprint: str, now: datetime | None = None
    ) -> bool:
        current = now or utc_now()
        return (
            self.revoked_at is None
            and self.issued_at <= current < self.expires_at
            and symbol in self.symbols
            and configuration_fingerprint == self.configuration_fingerprint
        )


class DecisionEpisode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str = Field(default_factory=lambda: f"de_{uuid4().hex}")
    symbol: Literal["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
    status: str = "CREATED"
    market_state_hash: str
    delta: DeltaContext
    analysis_id: str | None = None
    quant_prediction_id: str | None = None
    decision_id: str | None = None
    thesis_id: str | None = None
    plan_id: str | None = None
    trigger_event_id: str | None = None
    risk_allocation_id: str | None = None
    order_ids: list[str] = Field(default_factory=list)
    position_id: str | None = None
    outcome: dict[str, Any] | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: dict[str, int] = Field(default_factory=dict)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


THESIS_TRANSITIONS: dict[ThesisState, set[ThesisState]] = {
    ThesisState.CREATED: {ThesisState.ACTIVE, ThesisState.CANCELLED},
    ThesisState.ACTIVE: {
        ThesisState.COMPLETED,
        ThesisState.INVALIDATED,
        ThesisState.EXPIRED,
        ThesisState.CANCELLED,
    },
    ThesisState.COMPLETED: set(),
    ThesisState.INVALIDATED: set(),
    ThesisState.EXPIRED: set(),
    ThesisState.CANCELLED: set(),
}

PLAN_TRANSITIONS: dict[PlanState, set[PlanState]] = {
    PlanState.CREATED: {PlanState.VALIDATED, PlanState.INVALIDATED, PlanState.CANCELLED},
    PlanState.VALIDATED: {PlanState.ACTIVE, PlanState.INVALIDATED, PlanState.CANCELLED},
    PlanState.ACTIVE: {
        PlanState.WAITING_TRIGGER,
        PlanState.ARMED,
        PlanState.EXPIRED,
        PlanState.INVALIDATED,
        PlanState.CANCELLED,
    },
    PlanState.WAITING_TRIGGER: {
        PlanState.ARMED,
        PlanState.TRIGGERED,
        PlanState.EXPIRED,
        PlanState.INVALIDATED,
        PlanState.CANCELLED,
    },
    PlanState.ARMED: {
        PlanState.TRIGGERED,
        PlanState.EXPIRED,
        PlanState.INVALIDATED,
        PlanState.CANCELLED,
    },
    PlanState.TRIGGERED: {PlanState.COMPLETED, PlanState.INVALIDATED, PlanState.CANCELLED},
    PlanState.COMPLETED: set(),
    PlanState.EXPIRED: set(),
    PlanState.INVALIDATED: set(),
    PlanState.CANCELLED: set(),
}

POSITION_TRANSITIONS: dict[PositionStateV2, set[PositionStateV2]] = {
    PositionStateV2.FLAT: {PositionStateV2.OPENING},
    PositionStateV2.OPENING: {
        PositionStateV2.PARTIALLY_FILLED,
        PositionStateV2.OPEN,
        PositionStateV2.FLAT,
    },
    PositionStateV2.PARTIALLY_FILLED: {
        PositionStateV2.OPEN,
        PositionStateV2.PARTIAL_EXIT,
        PositionStateV2.CLOSING,
    },
    PositionStateV2.OPEN: {PositionStateV2.PARTIAL_EXIT, PositionStateV2.CLOSING},
    PositionStateV2.PARTIAL_EXIT: {PositionStateV2.CLOSING, PositionStateV2.CLOSED},
    PositionStateV2.CLOSING: {PositionStateV2.CLOSED},
    PositionStateV2.CLOSED: set(),
}


def require_transition(current: StrEnum, target: StrEnum) -> None:
    transition_maps = {
        ThesisState: THESIS_TRANSITIONS,
        PlanState: PLAN_TRANSITIONS,
        PositionStateV2: POSITION_TRANSITIONS,
    }
    transition_map = transition_maps.get(type(current))
    if (
        transition_map is None
        or type(target) is not type(current)
        or target not in transition_map[current]
    ):
        raise ValueError(f"invalid state transition: {current} -> {target}")


def _read_path(payload: dict[str, Any], path: str | None) -> Any:
    current: Any = payload
    for part in (path or "").split("."):
        if not part:
            continue
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def metric_wake_path(condition: WakeCondition) -> str | None:
    aliases = {
        "price": "price",
        "mark_price": "price",
        "close": "price",
        "trade_flow": "microstructure.values.trade_flow_imbalance",
        "trade_flow_imbalance": "microstructure.values.trade_flow_imbalance",
        "order_book_imbalance": "microstructure.values.order_book_imbalance",
        "estimated_slippage_bps": "microstructure.values.estimated_slippage_bps",
        "spread_bps": "market.values.spread_bps",
        "adx": f"candles.{condition.timeframe or '1h'}.adx_14",
        "adx_14": f"candles.{condition.timeframe or '1h'}.adx_14",
        "relative_volume": (
            f"candles.{condition.timeframe or '5m'}.relative_volume_20"
        ),
        "relative_volume_20": (
            f"candles.{condition.timeframe or '5m'}.relative_volume_20"
        ),
        "macd_histogram": (
            f"candles.{condition.timeframe or '1h'}.macd_histogram"
        ),
        "momentum_1h": "candles.1h.roc_12",
        "momentum": f"candles.{condition.timeframe or '1h'}.roc_12",
        "rsi": f"candles.{condition.timeframe or '1h'}.rsi_14",
        "rsi_14": f"candles.{condition.timeframe or '1h'}.rsi_14",
        "atr": f"candles.{condition.timeframe or '1h'}.atr_14",
        "atr_14": f"candles.{condition.timeframe or '1h'}.atr_14",
        "realized_volatility_percentile": (
            "volatility.values.realized_volatility_percentile"
        ),
        "volatility_ratio": "volatility.values.volatility_ratio_20_100",
        "volatility_ratio_20_100": "volatility.values.volatility_ratio_20_100",
        "volume_acceleration": "volume.values.volume_acceleration",
        "funding_rate": "derivatives.values.funding_rate",
        "open_interest": "derivatives.values.open_interest",
        "open_interest_change_24h": "derivatives.values.open_interest_change_24h",
        "basis_rate": "derivatives.values.basis_rate",
        "long_short_ratio": "derivatives.values.long_short_ratio",
        "liquidation_flow_imbalance": (
            "derivatives.values.liquidation_flow_imbalance"
        ),
    }
    return aliases.get(condition.path or "", condition.path)


def evaluate_wake_condition(
    condition: WakeCondition,
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Evaluate the bounded wake DSL. Missing values never satisfy a rule."""

    observed_at = now or utc_now()
    if condition.expires_at and observed_at > condition.expires_at:
        return False
    if condition.kind == WakeKind.ALL:
        return all(
            evaluate_wake_condition(item, current, previous, now=observed_at)
            for item in condition.children
        )
    if condition.kind == WakeKind.ANY:
        return any(
            evaluate_wake_condition(item, current, previous, now=observed_at)
            for item in condition.children
        )
    if condition.kind == WakeKind.EXPIRES_AT:
        deadline = condition.value
        return isinstance(deadline, datetime) and observed_at >= deadline
    if condition.kind in {
        WakeKind.ORDER_EVENT,
        WakeKind.POSITION_EVENT,
        WakeKind.EXTERNAL_EVENT,
    }:
        actual = _read_path(current, condition.path) if condition.path else current.get("event")
        if condition.kind == WakeKind.EXTERNAL_EVENT:
            actual = current.get("event")
            expected = str(condition.value or "").upper()
            expected = {
                "NEW_PREDICTION": "QUANT_PREDICTION_UPDATED",
                "QUANT_UPDATED": "QUANT_PREDICTION_UPDATED",
                "QUANT_PREDICTION": "QUANT_PREDICTION_UPDATED",
            }.get(expected, expected)
            return actual is not None and str(actual).upper() == expected
        return actual is not None and str(actual) == str(condition.value)
    if condition.kind in {WakeKind.STRUCTURE_CHANGED, WakeKind.REGIME_CHANGED}:
        path = condition.path or (
            "regime"
            if condition.kind == WakeKind.REGIME_CHANGED
            else "structure.values.structure"
        )
        actual = _read_path(current, path)
        prior = _read_path(previous or {}, path)
        if actual is None or prior is None or actual == prior:
            return False
        return condition.value is None or str(actual) == str(condition.value)

    path = condition.path
    if condition.kind in {
        WakeKind.PRICE_CROSSES_ABOVE,
        WakeKind.PRICE_CROSSES_BELOW,
        WakeKind.PRICE_IN_ZONE,
    }:
        path = "price"
    elif condition.kind in {WakeKind.CANDLE_CLOSE_ABOVE, WakeKind.CANDLE_CLOSE_BELOW}:
        path = f"candles.{condition.timeframe}.close"
    else:
        path = metric_wake_path(condition)
    actual = _read_path(current, path)
    prior = _read_path(previous or {}, path)
    if actual is None:
        return False
    try:
        actual_decimal = Decimal(str(actual))
        value_decimal = Decimal(str(condition.value)) if condition.value is not None else None
        prior_decimal = Decimal(str(prior)) if prior is not None else None
    except Exception:
        return False
    if condition.kind in {WakeKind.PRICE_IN_ZONE, WakeKind.METRIC_IN_RANGE}:
        assert condition.lower is not None and condition.upper is not None
        if condition.lower <= actual_decimal <= condition.upper:
            return True
        # MarketState updates can jump across a narrow LLM entry zone between
        # two samples.  Treat the observed price segment as entering the zone
        # so a fast move is not silently missed.  Metric ranges remain point-in-
        # time checks because interpolation is not meaningful for every metric.
        if condition.kind == WakeKind.PRICE_IN_ZONE and prior_decimal is not None:
            observed_low = min(prior_decimal, actual_decimal)
            observed_high = max(prior_decimal, actual_decimal)
            return observed_low <= condition.upper and observed_high >= condition.lower
        return False
    if value_decimal is None:
        return False
    if condition.kind in {WakeKind.METRIC_GT, WakeKind.CANDLE_CLOSE_ABOVE}:
        return actual_decimal > value_decimal
    if condition.kind in {WakeKind.METRIC_LT, WakeKind.CANDLE_CLOSE_BELOW}:
        return actual_decimal < value_decimal
    if prior_decimal is None:
        return False
    if condition.kind in {WakeKind.PRICE_CROSSES_ABOVE, WakeKind.METRIC_CROSSES_ABOVE}:
        return prior_decimal <= value_decimal < actual_decimal
    if condition.kind in {WakeKind.PRICE_CROSSES_BELOW, WakeKind.METRIC_CROSSES_BELOW}:
        return prior_decimal >= value_decimal > actual_decimal
    return False
