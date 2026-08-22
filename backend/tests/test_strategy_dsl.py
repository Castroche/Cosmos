from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.strategy_backends import NumpyFastVectorBackend, assert_backend_intent_parity
from cosmos_quant.strategy_dsl import (
    OPERATOR_REGISTRY,
    DSLNode,
    SignalDefinition,
    StrategyDSLV2,
    StrategyDSLValidationError,
    compile_strategy_dsl,
    legacy_strategy_spec_to_dsl,
)
from cosmos_quant.strategy_spec import default_strategy_spec


def _valid_dsl() -> StrategyDSLV2:
    return StrategyDSLV2(
        hypothesis_id="hypothesis_test",
        family="trend",
        evaluation_clock="closed_bar",
        required_features=["open", "high", "low", "close", "volume"],
        intended_regime=["trend"],
        holding_horizon="1-20 bars",
        nodes=[
            DSLNode(id="fast", operator="sma", inputs=["close"], parameters={"window": 5}),
            DSLNode(id="slow", operator="sma", inputs=["close"], parameters={"window": 12}),
            DSLNode(id="long", operator="crosses_above", inputs=["fast", "slow"]),
            DSLNode(id="short", operator="crosses_below", inputs=["fast", "slow"]),
            DSLNode(id="vol", operator="rolling_std", inputs=["close"], parameters={"window": 8}),
        ],
        signals=SignalDefinition(long="long", short="short"),
        risk={
            "stop_distance": "vol",
            "risk_budget_fraction": Decimal("0.005"),
            "maximum_holding_bars": 20,
        },
        invalidation=["trend reversal"],
    )


def test_dsl_compiles_to_deterministic_canonical_ir():
    dsl = _valid_dsl()
    first = compile_strategy_dsl(dsl)
    second = compile_strategy_dsl(dsl.model_dump(mode="json"))
    assert first == second
    assert first.fingerprint == second.fingerprint
    assert first.risk_budget_fraction == "0.005"


def test_attachment_operator_whitelist_has_pit_metadata_and_dual_backend_support():
    required = {
        "lag",
        "diff",
        "pct_change",
        "rolling_mean",
        "rolling_std",
        "rolling_min",
        "rolling_max",
        "rolling_quantile",
        "zscore",
        "rank",
        "percentile",
        "corr",
        "beta",
        "residual",
        "cs_rank",
        "demean",
        "funding",
        "open_interest",
        "basis",
        "spread",
        "orderflow_imbalance",
        "trade_imbalance",
        "AND",
        "OR",
        "NOT",
        "GT",
        "LT",
        "CROSS_ABOVE",
        "CROSS_BELOW",
        "time_stop",
        "price_stop",
        "volatility_target",
        "max_position",
        "max_holding",
    }
    assert required.issubset(OPERATOR_REGISTRY)
    for name in required:
        definition = OPERATOR_REGISTRY[name]
        assert definition.deterministic
        assert definition.fast_backend and definition.exact_backend
        assert definition.available_time_policy == "available_at_or_before_decision"


def test_dsl_rejects_cycle_negative_lag_and_external_side_effects():
    dsl = _valid_dsl()
    cycle = dsl.model_copy(
        update={
            "nodes": [
                DSLNode(id="a", operator="sma", inputs=["b"], parameters={"window": 5}),
                DSLNode(id="b", operator="sma", inputs=["a"], parameters={"window": 5}),
            ]
        }
    )
    with pytest.raises(StrategyDSLValidationError, match="acyclic"):
        compile_strategy_dsl(cycle)
    negative_lag = dsl.model_copy(
        update={
            "nodes": [
                DSLNode(id="bad", operator="lag", inputs=["close"], parameters={"periods": -1})
            ],
            "signals": SignalDefinition(long="bad"),
        }
    )
    with pytest.raises(StrategyDSLValidationError, match="non-negative"):
        compile_strategy_dsl(negative_lag)
    side_effect = dsl.model_copy(
        update={
            "nodes": [
                DSLNode(
                    id="bad",
                    operator="sma",
                    inputs=["close"],
                    parameters={"window": 5, "url": "https://example.invalid"},
                )
            ],
            "signals": SignalDefinition(long="bad"),
        }
    )
    with pytest.raises(StrategyDSLValidationError, match="side effects"):
        compile_strategy_dsl(side_effect)


def test_fast_and_exact_intent_backends_are_pointwise_equal():
    ir = compile_strategy_dsl(_valid_dsl())
    close = np.linspace(100.0, 130.0, 120) + np.sin(np.arange(120)) * 3
    features = {
        "open": close.copy(),
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.ones(120),
    }
    assert_backend_intent_parity(ir, features)
    result = NumpyFastVectorBackend().evaluate(ir, features)
    assert len(result.position_intent) == 120
    assert not result.long_signal[: result.warmup].any()


def test_cross_section_rank_uses_same_time_asset_panel_without_future_rows():
    dsl = StrategyDSLV2(
        hypothesis_id="cross_section_test",
        family="cross_section",
        evaluation_clock="closed_bar",
        required_features=["panel", "close"],
        intended_regime=["broad_market"],
        holding_horizon="1-5 bars",
        nodes=[
            DSLNode(
                id="ranked",
                operator="cs_rank",
                inputs=["panel"],
                parameters={"asset_index": 1},
            ),
            DSLNode(
                id="long",
                operator="GT",
                inputs=["ranked"],
                parameters={"scalar": Decimal("0.5")},
            ),
            DSLNode(
                id="stop",
                operator="rolling_std",
                inputs=["close"],
                parameters={"window": 3},
            ),
        ],
        signals=SignalDefinition(long="long"),
        risk={
            "stop_distance": "stop",
            "risk_budget_fraction": "0.005",
            "maximum_holding_bars": 5,
        },
        invalidation=["cross-sectional rank decays"],
    )
    ir = compile_strategy_dsl(dsl)
    panel = np.asarray([[1.0, 3.0, 2.0], [4.0, 2.0, 1.0]] * 10)
    result = NumpyFastVectorBackend().evaluate(
        ir,
        {"panel": panel, "close": np.linspace(100, 120, 20)},
    )
    assert result.feature_values["ranked"][0] == 1.0
    assert result.feature_values["ranked"][1] == 0.5


@pytest.mark.parametrize("family", list(StrategyKind))
def test_legacy_certified_generators_compile(family: StrategyKind):
    dsl = legacy_strategy_spec_to_dsl(default_strategy_spec(family))
    ir = compile_strategy_dsl(dsl)
    assert ir.hypothesis_id.startswith("legacy:")
    assert ir.fingerprint
