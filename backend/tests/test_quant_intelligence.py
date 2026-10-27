from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from cosmos_quant.domain import Candle
from cosmos_quant.quant_intelligence import (
    CalibrationMetrics,
    LightGBMQuantIntelligenceV1,
    QuantIntelligenceV1,
    QuantModelArtifactV1,
    calibration_metrics,
    purged_walk_forward_folds,
    quant_model_is_eligible,
)


def test_purged_walk_forward_and_calibration() -> None:
    folds = purged_walk_forward_folds(1000, folds=4, minimum_train=500)
    assert len(folds) == 4
    assert all(max(fold.train_indices) < min(fold.validation_indices) - 23 for fold in folds)
    metrics = calibration_metrics([[0.8, 0.1, 0.1], [0.1, 0.7, 0.2]], [0, 1])
    assert metrics.brier_score < Decimal("0.1")
    assert metrics.log_loss > 0


def test_quant_inference_has_ten_minute_ttl() -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    quant = QuantIntelligenceV1(
        model_bytes=b"model",
        predict_proba=lambda _: [0.7, 0.2, 0.1],
        calibrated=True,
    )
    prediction = quant.infer(
        symbol="BTC/USDT:USDT",
        data_as_of=now,
        features=[1.0, 2.0],
        expected_return_after_cost=Decimal("0.001"),
        expected_mfe=Decimal("0.01"),
        expected_mae=Decimal("-0.005"),
    )
    assert prediction.expires_at == now + timedelta(minutes=10)
    assert prediction.supports("LONG", now=now + timedelta(minutes=9))
    assert not prediction.supports("LONG", now=now + timedelta(minutes=11))


def test_quant_model_must_beat_uniform_three_class_baseline() -> None:
    artifact = QuantModelArtifactV1(
        symbol="BTC/USDT:USDT",
        feature_names=["return_1"],
        classifier_model="classifier",
        expected_return_model="return",
        mfe_model="mfe",
        mae_model="mae",
        probability_temperature="1",
        validation=CalibrationMetrics(
            brier_score="0.23",
            log_loss="1.12",
            sample_count=2000,
        ),
        training_rows=5000,
        data_as_of=datetime(2026, 8, 29, tzinfo=UTC),
        model_fingerprint="fingerprint",
    )
    assert not quant_model_is_eligible(artifact)
    eligible = artifact.model_copy(
        update={
            "validation": CalibrationMetrics(
                brier_score="0.20",
                log_loss="1.0",
                sample_count=2000,
            )
        }
    )
    assert quant_model_is_eligible(eligible)


def test_lightgbm_online_inference_uses_one_native_thread(monkeypatch) -> None:
    from threadpoolctl import threadpool_info

    # Load the real runtime first: checking a fake Booster alone cannot detect
    # native worker teams being created before predict's num_threads is applied.
    __import__("lightgbm")
    before = [row for row in threadpool_info() if row["user_api"] == "openmp"]
    assert before
    calls: list[dict] = []

    class FakeBooster:
        def __init__(self, *, model_str: str):
            native = [row for row in threadpool_info() if row["user_api"] == "openmp"]
            assert native and all(row["num_threads"] == 1 for row in native)
            self.model_str = model_str

        def predict(self, _matrix, **options):
            calls.append(options)
            if self.model_str == "classifier":
                return [[0.7, 0.2, 0.1]]
            return [0.01]

    monkeypatch.setitem(sys.modules, "lightgbm", SimpleNamespace(Booster=FakeBooster))
    artifact = QuantModelArtifactV1(
        symbol="BTC/USDT:USDT",
        feature_names=["return_1"],
        classifier_model="classifier",
        expected_return_model="return",
        mfe_model="mfe",
        mae_model="mae",
        probability_temperature="1",
        validation=CalibrationMetrics(
            brier_score="0.20",
            log_loss="1.0",
            sample_count=2000,
        ),
        training_rows=5000,
        data_as_of=datetime(2026, 8, 29, tzinfo=UTC),
        model_fingerprint="fingerprint",
    )

    prediction = LightGBMQuantIntelligenceV1(artifact).infer(
        data_as_of=datetime(2026, 8, 29, tzinfo=UTC),
        features=[1.0],
    )

    assert abs(prediction.probability_long_tp_first - Decimal("0.7")) < Decimal("1e-12")
    assert len(calls) == 4
    assert all(call == {"num_threads": 1} for call in calls)
    after = [row for row in threadpool_info() if row["user_api"] == "openmp"]
    assert after == before  # Offline research retains its original thread budget.


def candle(index: int, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
        open=price,
        high=price + Decimal("0.5"),
        low=price - Decimal("0.5"),
        close=price,
        volume=Decimal("100"),
    )
