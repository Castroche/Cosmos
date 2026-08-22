from __future__ import annotations

import numpy as np
from cosmos_quant.strategy_dsl import (
    DSLNode,
    SignalDefinition,
    StrategyDSLV2,
    compile_strategy_dsl,
)
from cosmos_quant.strategy_integrity import IntegrityGateName, run_integrity_gates


def _valid_dsl() -> StrategyDSLV2:
    return StrategyDSLV2(
        hypothesis_id="hypothesis_test",
        family="trend",
        evaluation_clock="closed_bar",
        required_features=["close"],
        intended_regime=["trend"],
        holding_horizon="1-20 bars",
        nodes=[
            DSLNode(id="fast", operator="sma", inputs=["close"], parameters={"window": 5}),
            DSLNode(id="slow", operator="sma", inputs=["close"], parameters={"window": 12}),
            DSLNode(id="long", operator="crosses_above", inputs=["fast", "slow"]),
            DSLNode(id="short", operator="crosses_below", inputs=["fast", "slow"]),
            DSLNode(
                id="vol",
                operator="rolling_std",
                inputs=["close"],
                parameters={"window": 8},
            ),
        ],
        signals=SignalDefinition(long="long", short="short"),
        risk={
            "stop_distance": "vol",
            "risk_budget_fraction": "0.005",
            "maximum_holding_bars": 20,
        },
        invalidation=["trend reversal"],
    )


def test_six_integrity_gates_pass_before_return_evidence_is_available():
    ir = compile_strategy_dsl(_valid_dsl())
    close = np.linspace(100.0, 130.0, 160) + np.sin(np.arange(160))
    features = {
        "open": close.copy(),
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.ones(160),
    }
    event = np.arange(160, dtype=np.int64)
    report = run_integrity_gates(
        ir,
        features,
        event_time_ns=event,
        available_time_ns=event + 1,
        decision_time_ns=event + 1,
    )
    assert report.passed
    assert {gate.name for gate in report.gates} == set(IntegrityGateName)


def test_missing_availability_is_untestable_not_guessed():
    ir = compile_strategy_dsl(_valid_dsl())
    close = np.linspace(100.0, 130.0, 160)
    features = {
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": close,
    }
    report = run_integrity_gates(ir, features)
    availability = next(
        gate for gate in report.gates if gate.name == IntegrityGateName.TIME_AVAILABILITY
    )
    assert not availability.passed
    assert availability.reason_codes == ("availability_metadata_missing",)
