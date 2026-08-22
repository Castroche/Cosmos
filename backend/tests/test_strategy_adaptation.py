from decimal import Decimal

import pytest
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.market_evidence import build_market_evidence
from cosmos_quant.signals import score_market_state
from cosmos_quant.strategy_adaptation import build_adaptive_strategy_candidates
from cosmos_quant.strategy_spec import validate_strategy_spec


@pytest.mark.parametrize("kind", list(StrategyKind))
def test_adaptive_candidates_are_bounded_deterministic_and_not_backtest_tuned(kind):
    candles = demo_candles(300)
    market = snapshot_from_candles(candles)
    assessment = score_market_state(candles)
    evidence = build_market_evidence(candles, market, "1h")

    first = build_adaptive_strategy_candidates(kind, assessment, evidence)
    second = build_adaptive_strategy_candidates(kind, assessment, evidence)

    assert [item.label for item in first] == [
        "baseline",
        "responsive",
        "selective",
        "cost_resilient",
    ]
    assert [item.spec_fingerprint for item in first] == [item.spec_fingerprint for item in second]
    assert len({item.spec_fingerprint for item in first}) == 4
    assert all(item.selection_basis == "current_market_only" for item in first)
    assert all(item.backtest_tuned is False for item in first)
    assert all(validate_strategy_spec(item.spec, kind) for item in first)
    assert all(any("未读取回测结果" in reason for reason in item.rationale) for item in first)


def test_high_volatility_widens_protection_without_forcing_a_signal():
    candles = demo_candles(300)
    market = snapshot_from_candles(candles)
    assessment = score_market_state(candles)
    evidence = build_market_evidence(candles, market, "1h").model_copy(
        update={
            "volatility_regime": "high",
            "volatility_ratio": Decimal("1.8"),
        }
    )

    candidates = build_adaptive_strategy_candidates(
        StrategyKind.TREND,
        assessment,
        evidence,
    )

    responsive = validate_strategy_spec(
        candidates[1].spec,
        StrategyKind.TREND,
    )
    assert responsive.parameters.stop_atr > Decimal("1.45")
    assert "signal_direction" not in candidates[1].spec


@pytest.mark.parametrize("kind", [StrategyKind.TREND, StrategyKind.BREAKOUT])
def test_trailing_activation_candidates_are_declared_before_backtesting(kind):
    candles = demo_candles(300)
    market = snapshot_from_candles(candles)
    assessment = score_market_state(candles)
    evidence = build_market_evidence(candles, market, "1h")

    candidates = build_adaptive_strategy_candidates(kind, assessment, evidence)
    activations = [
        validate_strategy_spec(item.spec, kind).parameters.trailing_activation
        for item in candidates
    ]

    assert activations == [
        "first_target",
        "one_r",
        "first_target",
        "one_r",
    ]


@pytest.mark.parametrize("kind", list(StrategyKind))
def test_cost_resilient_candidate_is_lower_turnover_and_not_backtest_selected(kind):
    candles = demo_candles(300)
    market = snapshot_from_candles(candles)
    assessment = score_market_state(candles)
    evidence = build_market_evidence(candles, market, "1h")

    candidates = build_adaptive_strategy_candidates(
        kind,
        assessment,
        evidence,
    )
    cost_resilient = next(item for item in candidates if item.label == "cost_resilient")
    spec = validate_strategy_spec(cost_resilient.spec, kind)

    assert cost_resilient.backtest_tuned is False
    assert cost_resilient.selection_basis == "current_market_only"
    assert any("执行摩擦" in reason or "成本" in reason for reason in cost_resilient.rationale)
    if kind == StrategyKind.TREND:
        assert spec.parameters.ema_slow >= 90
        assert spec.parameters.signal_gap_atr >= Decimal("0.30")
        assert spec.parameters.minimum_efficiency_ratio == Decimal("0.35")
    elif kind == StrategyKind.MEAN_REVERSION:
        assert spec.parameters.mean_window >= 50
        assert spec.parameters.zscore_entry >= Decimal("1.90")
    else:
        assert spec.parameters.lookback >= 60
        assert spec.parameters.minimum_volume_ratio >= Decimal("1.40")
