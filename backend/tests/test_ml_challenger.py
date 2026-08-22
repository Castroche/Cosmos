from decimal import Decimal

from cosmos_quant.demo import demo_candles
from cosmos_quant.ml_challenger import detect_return_drift, train_ml_challenger


def test_ml_challenger_uses_chronological_holdout_and_bounded_output():
    report = train_ml_challenger(demo_candles(600))

    assert report.training_samples > report.validation_samples
    assert Decimal("0") <= report.training_accuracy <= Decimal("1")
    assert Decimal("0") <= report.validation_accuracy <= Decimal("1")
    assert Decimal("0") <= report.validation_brier_score <= Decimal("1")
    if report.accepted:
        assert report.candidate_spec is not None
        assert report.candidate_spec["source"] == "ml"
    else:
        assert report.rejection_reasons


def test_drift_monitor_returns_a_governance_status():
    report = detect_return_drift(demo_candles(600))

    assert report.status in {"stable", "warning", "critical"}
    assert report.baseline_samples > report.recent_samples
    assert report.population_stability_index >= 0
    assert report.volatility_ratio > 0
