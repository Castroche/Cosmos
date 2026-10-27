from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .strategy_spec import (
    BreakoutStrategySpec,
    MeanReversionStrategySpec,
    StrategySpec,
    TrendStrategySpec,
)

DSL_VERSION = 2
OPERATOR_POLICY_VERSION = "cosmos-operators-v2"
MAX_DSL_NODES = 64
MAX_DSL_LOOKBACK = 4_096


class ValueType(StrEnum):
    SERIES = "series"
    BOOLEAN_SERIES = "boolean_series"
    SCALAR = "scalar"
    CROSS_SECTION = "cross_section"


class OperatorDefinition(BaseModel, frozen=True):
    name: str
    version: str = "1"
    input_types: tuple[tuple[ValueType, ...], ...]
    output_type: ValueType
    minimum_inputs: int
    maximum_inputs: int
    requires_window: bool = False
    maximum_window: int = MAX_DSL_LOOKBACK
    lookback: int = 0
    warmup: int = 0
    point_in_time_policy: Literal["asof", "closed_bar", "static"] = "closed_bar"
    available_time_policy: Literal["available_at_or_before_decision"] = (
        "available_at_or_before_decision"
    )
    revision_policy: Literal["immutable", "latest_available"] = "immutable"
    deterministic: bool = True
    fast_backend: bool = True
    exact_backend: bool = True


def _definition(
    name: str,
    inputs: tuple[tuple[ValueType, ...], ...],
    output: ValueType,
    *,
    minimum_inputs: int | None = None,
    maximum_inputs: int | None = None,
    requires_window: bool = False,
    lookback: int = 0,
    point_in_time_policy: Literal["asof", "closed_bar", "static"] = "closed_bar",
) -> OperatorDefinition:
    return OperatorDefinition(
        name=name,
        input_types=inputs,
        output_type=output,
        minimum_inputs=minimum_inputs if minimum_inputs is not None else len(inputs),
        maximum_inputs=maximum_inputs if maximum_inputs is not None else len(inputs),
        requires_window=requires_window,
        lookback=lookback,
        warmup=lookback,
        point_in_time_policy=point_in_time_policy,
    )


SERIES = (ValueType.SERIES,)
BOOL = (ValueType.BOOLEAN_SERIES,)
SCALAR_OR_SERIES = (ValueType.SCALAR, ValueType.SERIES)
SERIES_OR_CROSS = (ValueType.SERIES, ValueType.CROSS_SECTION)


def _build_operator_registry() -> dict[str, OperatorDefinition]:
    registry: dict[str, OperatorDefinition] = {}
    for name in ("lag", "difference", "returns", "log_returns"):
        registry[name] = _definition(name, (SERIES,), ValueType.SERIES)
    for name in (
        "sma",
        "ema",
        "rolling_min",
        "rolling_max",
        "rolling_std",
        "rolling_sum",
        "rolling_median",
        "rolling_rank",
        "zscore",
        "robust_zscore",
        "percentile_rank",
        "rsi",
        "atr",
        "roc",
        "realized_volatility",
    ):
        registry[name] = _definition(
            name,
            (SERIES,),
            ValueType.SERIES,
            requires_window=True,
            lookback=1,
        )
    for name in ("add", "subtract", "multiply", "divide", "minimum", "maximum"):
        registry[name] = _definition(
            name,
            (SCALAR_OR_SERIES, SCALAR_OR_SERIES),
            ValueType.SERIES,
            minimum_inputs=1,
        )
    for name in ("greater_than", "greater_equal", "less_than", "less_equal", "equal"):
        registry[name] = _definition(
            name,
            (SCALAR_OR_SERIES, SCALAR_OR_SERIES),
            ValueType.BOOLEAN_SERIES,
            minimum_inputs=1,
        )
    for name in ("and", "or"):
        registry[name] = _definition(
            name,
            (BOOL, BOOL),
            ValueType.BOOLEAN_SERIES,
            minimum_inputs=2,
            maximum_inputs=8,
        )
    registry["not"] = _definition("not", (BOOL,), ValueType.BOOLEAN_SERIES)
    registry["crosses_above"] = _definition(
        "crosses_above", (SCALAR_OR_SERIES, SCALAR_OR_SERIES), ValueType.BOOLEAN_SERIES
    )
    registry["crosses_below"] = _definition(
        "crosses_below", (SCALAR_OR_SERIES, SCALAR_OR_SERIES), ValueType.BOOLEAN_SERIES
    )
    for name in ("clip", "winsorize"):
        registry[name] = _definition(name, (SERIES,), ValueType.SERIES)
    for name in ("correlation", "covariance", "beta"):
        registry[name] = _definition(
            name,
            (SERIES, SERIES),
            ValueType.SERIES,
            requires_window=True,
            lookback=1,
        )
    for name in ("cross_section_rank", "cross_section_zscore", "neutralize"):
        registry[name] = _definition(
            name,
            (SERIES_OR_CROSS,),
            ValueType.SERIES,
            point_in_time_policy="asof",
        )
    for name in (
        "funding_rate",
        "open_interest_change",
        "basis_rate",
        "order_flow_imbalance",
        "liquidation_imbalance",
        "long_short_ratio",
    ):
        registry[name] = _definition(
            name,
            (),
            ValueType.SERIES,
            minimum_inputs=0,
            maximum_inputs=0,
            point_in_time_policy="asof",
        )
    for name in ("stop_distance", "risk_budget", "position_size", "time_stop"):
        registry[name] = _definition(
            name,
            (SCALAR_OR_SERIES,),
            ValueType.SERIES,
            minimum_inputs=0 if name == "risk_budget" else 1,
            maximum_inputs=2,
        )
    aliases = {
        "diff": "difference",
        "pct_change": "returns",
        "rolling_mean": "sma",
        "rolling_quantile": "rolling_median",
        "rank": "rolling_rank",
        "percentile": "percentile_rank",
        "corr": "correlation",
        "residual": "beta",
        "cs_rank": "cross_section_rank",
        "demean": "cross_section_zscore",
        "funding": "funding_rate",
        "open_interest": "open_interest_change",
        "basis": "basis_rate",
        "spread": "basis_rate",
        "orderflow_imbalance": "order_flow_imbalance",
        "trade_imbalance": "order_flow_imbalance",
        "AND": "and",
        "OR": "or",
        "NOT": "not",
        "GT": "greater_than",
        "LT": "less_than",
        "CROSS_ABOVE": "crosses_above",
        "CROSS_BELOW": "crosses_below",
        "price_stop": "stop_distance",
        "volatility_target": "position_size",
        "max_position": "position_size",
        "max_holding": "time_stop",
    }
    for alias, target in aliases.items():
        registry[alias] = registry[target].model_copy(update={"name": alias})
    return registry


OPERATOR_REGISTRY = _build_operator_registry()


class ResearchHypothesisV2(BaseModel):
    version: Literal[2] = 2
    id: str = Field(min_length=1, max_length=128)
    family_id: str = Field(min_length=1, max_length=128)
    observation: str = Field(min_length=12, max_length=4_000)
    economic_rationale: str = Field(min_length=12, max_length=4_000)
    causal_mechanism: str = Field(min_length=12, max_length=4_000)
    prediction: str = Field(min_length=12, max_length=4_000)
    expected_direction: Literal["long", "short", "both"]
    intended_regime: list[str] = Field(min_length=1, max_length=12)
    holding_horizon: str = Field(min_length=1, max_length=64)
    required_features: list[str] = Field(min_length=1, max_length=64)
    falsification_criteria: list[str] = Field(min_length=1, max_length=16)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=16)
    source: Literal["system", "operator", "ai", "ml"] = "ai"
    parent_hypothesis_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DSLNode(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    operator: str
    inputs: list[str] = Field(default_factory=list, max_length=8)
    parameters: dict[str, Decimal | int | str | bool] = Field(default_factory=dict)


class SignalDefinition(BaseModel):
    long: str | None = None
    short: str | None = None
    filter: str | None = None

    @model_validator(mode="after")
    def require_direction(self):
        if self.long is None and self.short is None:
            raise ValueError("at least one long or short signal is required")
        return self


class RiskDefinition(BaseModel):
    stop_distance: str
    risk_budget_fraction: Decimal = Field(gt=0, le=Decimal("0.02"))
    maximum_holding_bars: int = Field(ge=1, le=2_000)
    allow_scale_in: Literal[False] = False
    allow_pyramiding: Literal[False] = False
    allow_automatic_leverage_increase: Literal[False] = False


class StrategyDSLV2(BaseModel):
    version: Literal[2] = 2
    hypothesis_id: str
    family: str
    evaluation_clock: str
    required_features: list[str] = Field(min_length=1, max_length=64)
    intended_regime: list[str] = Field(min_length=1, max_length=12)
    holding_horizon: str
    nodes: list[DSLNode] = Field(min_length=1, max_length=MAX_DSL_NODES)
    signals: SignalDefinition
    exits: list[str] = Field(default_factory=list, max_length=8)
    risk: RiskDefinition
    invalidation: list[str] = Field(min_length=1, max_length=16)
    regime_gates: dict[str, str] = Field(default_factory=dict, max_length=7)

    @model_validator(mode="after")
    def validate_regime_gate_names(self):
        allowed = {
            "trend",
            "volatility",
            "liquidity",
            "funding",
            "open_interest",
            "basis",
            "order_flow",
        }
        unknown = set(self.regime_gates) - allowed
        if unknown:
            raise ValueError(f"unknown regime gates: {sorted(unknown)}")
        return self


class CanonicalNode(BaseModel, frozen=True):
    id: str
    operator: str
    operator_version: str
    inputs: tuple[str, ...]
    parameters: tuple[tuple[str, str | int | bool], ...]
    output_type: ValueType
    lookback: int
    warmup: int
    point_in_time_policy: str
    available_time_policy: str
    revision_policy: str


class CanonicalStrategyIR(BaseModel, frozen=True):
    version: Literal[2] = 2
    hypothesis_id: str
    family: str
    evaluation_clock: str
    required_features: tuple[str, ...]
    intended_regime: tuple[str, ...]
    holding_horizon: str
    nodes: tuple[CanonicalNode, ...]
    long_signal: str | None
    short_signal: str | None
    filter_signal: str | None
    exits: tuple[str, ...]
    stop_distance: str
    risk_budget_fraction: str
    maximum_holding_bars: int
    invalidation: tuple[str, ...]
    regime_gates: tuple[tuple[str, str], ...] = ()
    operator_policy_version: str = OPERATOR_POLICY_VERSION

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return sha256(encoded).hexdigest()


class StrategyDSLValidationError(ValueError):
    pass


SOURCE_FEATURE_TYPES: dict[str, ValueType] = {
    "open": ValueType.SERIES,
    "high": ValueType.SERIES,
    "low": ValueType.SERIES,
    "close": ValueType.SERIES,
    "volume": ValueType.SERIES,
}


def _parameter_text(value: Any) -> str | int | bool:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def compile_strategy_dsl(payload: StrategyDSLV2 | dict[str, Any]) -> CanonicalStrategyIR:
    try:
        dsl = (
            payload if isinstance(payload, StrategyDSLV2) else StrategyDSLV2.model_validate(payload)
        )
    except Exception as exc:
        raise StrategyDSLValidationError(str(exc)) from exc
    nodes = {node.id: node for node in dsl.nodes}
    if len(nodes) != len(dsl.nodes):
        raise StrategyDSLValidationError("node ids must be unique")
    indegree = dict.fromkeys(nodes, 0)
    dependants: dict[str, list[str]] = defaultdict(list)
    for node in dsl.nodes:
        for source in node.inputs:
            if source not in nodes and source not in dsl.required_features:
                raise StrategyDSLValidationError(f"unknown input {source!r} for node {node.id!r}")
            if source in nodes:
                indegree[node.id] += 1
                dependants[source].append(node.id)
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    ordered_ids: list[str] = []
    while queue:
        node_id = queue.popleft()
        ordered_ids.append(node_id)
        for dependant in sorted(dependants[node_id]):
            indegree[dependant] -= 1
            if indegree[dependant] == 0:
                queue.append(dependant)
    if len(ordered_ids) != len(nodes):
        raise StrategyDSLValidationError("feature graph must be acyclic")

    value_types = {
        feature: SOURCE_FEATURE_TYPES.get(feature, ValueType.SERIES)
        for feature in dsl.required_features
    }
    compiled: list[CanonicalNode] = []
    cumulative_warmup: dict[str, int] = dict.fromkeys(dsl.required_features, 0)
    for node_id in ordered_ids:
        node = nodes[node_id]
        definition = OPERATOR_REGISTRY.get(node.operator)
        if definition is None:
            raise StrategyDSLValidationError(f"operator {node.operator!r} is not registered")
        if not (definition.minimum_inputs <= len(node.inputs) <= definition.maximum_inputs):
            raise StrategyDSLValidationError(f"operator {node.operator!r} input count is invalid")
        for index, source in enumerate(node.inputs):
            allowed = definition.input_types[min(index, len(definition.input_types) - 1)]
            if value_types[source] not in allowed:
                raise StrategyDSLValidationError(
                    f"operator {node.operator!r} input {source!r} has type {value_types[source]}"
                )
        if (
            len(node.inputs) == 1
            and definition.maximum_inputs > 1
            and "scalar" not in node.parameters
        ):
            raise StrategyDSLValidationError(
                f"operator {node.operator!r} requires a second input or scalar parameter"
            )
        window = node.parameters.get("window")
        if definition.requires_window:
            if not isinstance(window, int) or isinstance(window, bool) or window < 2:
                raise StrategyDSLValidationError(f"operator {node.operator!r} requires window >= 2")
            if window > definition.maximum_window:
                raise StrategyDSLValidationError("rolling window exceeds certified envelope")
        if node.operator == "lag":
            periods = node.parameters.get("periods", 1)
            if not isinstance(periods, int) or isinstance(periods, bool) or periods < 0:
                raise StrategyDSLValidationError("lag periods must be a non-negative integer")
        disallowed = {
            "code",
            "python",
            "url",
            "path",
            "command",
            "callback",
            "import",
            "subprocess",
            "filesystem",
            "network",
            "database",
        }
        if disallowed.intersection(node.parameters):
            raise StrategyDSLValidationError("external side effects are forbidden")
        node_lookback = int(window or node.parameters.get("periods", definition.lookback) or 0)
        parent_warmup = max((cumulative_warmup[source] for source in node.inputs), default=0)
        cumulative_warmup[node_id] = parent_warmup + node_lookback
        value_types[node_id] = definition.output_type
        compiled.append(
            CanonicalNode(
                id=node.id,
                operator=definition.name,
                operator_version=definition.version,
                inputs=tuple(node.inputs),
                parameters=tuple(
                    (key, _parameter_text(value)) for key, value in sorted(node.parameters.items())
                ),
                output_type=definition.output_type,
                lookback=node_lookback,
                warmup=cumulative_warmup[node_id],
                point_in_time_policy=definition.point_in_time_policy,
                available_time_policy=definition.available_time_policy,
                revision_policy=definition.revision_policy,
            )
        )
    referenced = [
        dsl.signals.long,
        dsl.signals.short,
        dsl.signals.filter,
        dsl.risk.stop_distance,
        *dsl.exits,
        *dsl.regime_gates.values(),
    ]
    for reference in (item for item in referenced if item is not None):
        if reference not in value_types:
            raise StrategyDSLValidationError(f"unknown output reference {reference!r}")
    for reference in (dsl.signals.long, dsl.signals.short, dsl.signals.filter):
        if reference is not None and value_types[reference] != ValueType.BOOLEAN_SERIES:
            raise StrategyDSLValidationError(f"signal {reference!r} must be boolean")
    for gate, reference in dsl.regime_gates.items():
        if value_types[reference] != ValueType.BOOLEAN_SERIES:
            raise StrategyDSLValidationError(f"regime gate {gate!r} must be boolean")
    if value_types[dsl.risk.stop_distance] != ValueType.SERIES:
        raise StrategyDSLValidationError("stop distance must be a series")
    return CanonicalStrategyIR(
        hypothesis_id=dsl.hypothesis_id,
        family=dsl.family,
        evaluation_clock=dsl.evaluation_clock,
        required_features=tuple(sorted(set(dsl.required_features))),
        intended_regime=tuple(sorted(set(dsl.intended_regime))),
        holding_horizon=dsl.holding_horizon,
        nodes=tuple(compiled),
        long_signal=dsl.signals.long,
        short_signal=dsl.signals.short,
        filter_signal=dsl.signals.filter,
        exits=tuple(dsl.exits),
        stop_distance=dsl.risk.stop_distance,
        risk_budget_fraction=format(dsl.risk.risk_budget_fraction.normalize(), "f"),
        maximum_holding_bars=dsl.risk.maximum_holding_bars,
        invalidation=tuple(dsl.invalidation),
        regime_gates=tuple(sorted(dsl.regime_gates.items())),
    )


def legacy_strategy_spec_to_dsl(spec: StrategySpec) -> StrategyDSLV2:
    """Map existing certified families into DSL v2 without changing StrategySpec identity."""
    common = {
        "version": 2,
        "hypothesis_id": f"legacy:{spec.fingerprint}",
        "family": str(spec.family),
        "evaluation_clock": "closed_bar",
        "required_features": ["open", "high", "low", "close", "volume"],
        "intended_regime": ["legacy_certified_envelope"],
        "holding_horizon": f"1-{spec.parameters.maximum_holding_bars or 2000} bars",
        "risk": {
            "stop_distance": "stop",
            "risk_budget_fraction": "0.005",
            "maximum_holding_bars": spec.parameters.maximum_holding_bars or 2000,
        },
        "invalidation": ["legacy stop or maximum holding period"],
    }
    if isinstance(spec, TrendStrategySpec):
        nodes = [
            DSLNode(
                id="fast",
                operator="ema",
                inputs=["close"],
                parameters={"window": spec.parameters.ema_fast},
            ),
            DSLNode(
                id="slow",
                operator="ema",
                inputs=["close"],
                parameters={"window": spec.parameters.ema_slow},
            ),
            DSLNode(id="long", operator="crosses_above", inputs=["fast", "slow"]),
            DSLNode(id="short", operator="crosses_below", inputs=["fast", "slow"]),
            DSLNode(id="atr_value", operator="atr", inputs=["close"], parameters={"window": 14}),
            DSLNode(
                id="stop",
                operator="multiply",
                inputs=["atr_value"],
                parameters={"scalar": spec.parameters.stop_atr},
            ),
        ]
    elif isinstance(spec, MeanReversionStrategySpec):
        nodes = [
            DSLNode(
                id="z",
                operator="zscore",
                inputs=["close"],
                parameters={"window": spec.parameters.mean_window},
            ),
            DSLNode(
                id="long",
                operator="less_than",
                inputs=["z"],
                parameters={"scalar": -spec.parameters.zscore_entry},
            ),
            DSLNode(
                id="short",
                operator="greater_than",
                inputs=["z"],
                parameters={"scalar": spec.parameters.zscore_entry},
            ),
            DSLNode(id="atr_value", operator="atr", inputs=["close"], parameters={"window": 14}),
            DSLNode(
                id="stop",
                operator="multiply",
                inputs=["atr_value"],
                parameters={"scalar": spec.parameters.stop_atr},
            ),
        ]
    elif isinstance(spec, BreakoutStrategySpec):
        nodes = [
            DSLNode(
                id="upper",
                operator="rolling_max",
                inputs=["high"],
                parameters={"window": spec.parameters.lookback},
            ),
            DSLNode(
                id="lower",
                operator="rolling_min",
                inputs=["low"],
                parameters={"window": spec.parameters.lookback},
            ),
            DSLNode(id="long", operator="crosses_above", inputs=["close", "upper"]),
            DSLNode(id="short", operator="crosses_below", inputs=["close", "lower"]),
            DSLNode(id="atr_value", operator="atr", inputs=["close"], parameters={"window": 14}),
            DSLNode(
                id="stop",
                operator="multiply",
                inputs=["atr_value"],
                parameters={"scalar": spec.parameters.stop_atr},
            ),
        ]
    else:  # pragma: no cover - discriminated union protects this branch
        raise StrategyDSLValidationError("unsupported legacy StrategySpec")
    return StrategyDSLV2.model_validate(
        {
            **common,
            "nodes": nodes,
            "signals": {"long": "long", "short": "short"},
        }
    )
