from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import IntEnum
from hashlib import sha256
from math import log

from pydantic import BaseModel, ConfigDict, Field

from .domain import Candle
from .indicators import atr
from .trading_intelligence import V2_SYMBOLS, QuantPrediction


class TripleBarrierClass(IntEnum):
    LONG_TP_FIRST = 0
    SHORT_TP_FIRST = 1
    TIMEOUT = 2


class TripleBarrierObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_index: int = Field(ge=0)
    outcome_index: int = Field(ge=0)
    label: TripleBarrierClass
    entry: Decimal
    atr_at_entry: Decimal = Field(gt=0)
    expected_return_before_cost: Decimal
    expected_return_after_cost: Decimal
    mfe: Decimal
    mae: Decimal


def atr_triple_barrier_labels(
    candles: list[Candle],
    *,
    atr_period: int = 14,
    target_atr: Decimal = Decimal("2"),
    stop_atr: Decimal = Decimal("1"),
    horizon_bars: int = 24,
    round_trip_cost_bps: Decimal = Decimal("8"),
) -> list[TripleBarrierObservation]:
    """Create causal 5m labels using ATR known at the signal close."""

    if horizon_bars < 1:
        raise ValueError("horizon_bars must be positive")
    observations: list[TripleBarrierObservation] = []
    cost_fraction = round_trip_cost_bps / Decimal("10000")
    for index in range(atr_period + 1, len(candles) - horizon_bars):
        historical = candles[max(0, index - atr_period - 1) : index + 1]
        volatility = atr(historical, atr_period)
        entry = candles[index].close
        long_target = entry + target_atr * volatility
        long_stop = entry - stop_atr * volatility
        short_target = entry - target_atr * volatility
        short_stop = entry + stop_atr * volatility
        label = TripleBarrierClass.TIMEOUT
        outcome_index = index + horizon_bars
        outcome_price = candles[outcome_index].close
        long_alive = True
        short_alive = True
        future = candles[index + 1 : index + horizon_bars + 1]
        for offset, candle in enumerate(future, start=1):
            # Same-bar target/stop ambiguity is resolved conservatively: that
            # direction is stopped, never awarded a target label.
            long_hit_target = long_alive and candle.high >= long_target
            long_hit_stop = long_alive and candle.low <= long_stop
            short_hit_target = short_alive and candle.low <= short_target
            short_hit_stop = short_alive and candle.high >= short_stop
            if long_hit_stop:
                long_alive = False
            if short_hit_stop:
                short_alive = False
            if long_hit_target and not long_hit_stop and long_alive:
                label = TripleBarrierClass.LONG_TP_FIRST
                outcome_index = index + offset
                outcome_price = long_target
                break
            if short_hit_target and not short_hit_stop and short_alive:
                label = TripleBarrierClass.SHORT_TP_FIRST
                outcome_index = index + offset
                outcome_price = short_target
                break
        highs = [item.high for item in future]
        lows = [item.low for item in future]
        if label == TripleBarrierClass.LONG_TP_FIRST:
            raw_return = (outcome_price - entry) / entry if entry else Decimal("0")
            mfe = (max(highs) - entry) / entry if entry else Decimal("0")
            mae = (entry - min(lows)) / entry if entry else Decimal("0")
        elif label == TripleBarrierClass.SHORT_TP_FIRST:
            raw_return = (entry - outcome_price) / entry if entry else Decimal("0")
            mfe = (entry - min(lows)) / entry if entry else Decimal("0")
            mae = (max(highs) - entry) / entry if entry else Decimal("0")
        else:
            raw_return = Decimal("0")
            mfe = (
                max(
                    (max(highs) - entry) / entry,
                    (entry - min(lows)) / entry,
                )
                if entry
                else Decimal("0")
            )
            mae = Decimal("0")
        observations.append(
            TripleBarrierObservation(
                signal_index=index,
                outcome_index=outcome_index,
                label=label,
                entry=entry,
                atr_at_entry=volatility,
                expected_return_before_cost=raw_return,
                expected_return_after_cost=raw_return - cost_fraction,
                mfe=mfe,
                mae=mae,
            )
        )
    return observations


@dataclass(frozen=True)
class PurgedFold:
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]


def purged_walk_forward_folds(
    sample_count: int,
    *,
    folds: int = 5,
    minimum_train: int = 500,
    purge_bars: int = 24,
    embargo_bars: int = 24,
) -> list[PurgedFold]:
    if folds < 2 or sample_count <= minimum_train + folds:
        raise ValueError("not enough samples for purged walk-forward")
    available = sample_count - minimum_train - purge_bars - embargo_bars
    validation_size = max(available // folds, 1)
    result: list[PurgedFold] = []
    for fold in range(folds):
        validation_start = minimum_train + purge_bars + embargo_bars + fold * validation_size
        validation_end = (
            sample_count
            if fold == folds - 1
            else min(validation_start + validation_size, sample_count)
        )
        train_end = max(validation_start - purge_bars - embargo_bars, 0)
        train_indices = tuple(range(train_end))
        validation_indices = tuple(range(validation_start, validation_end))
        if train_indices and validation_indices:
            result.append(PurgedFold(train_indices, validation_indices))
    return result


class CalibrationMetrics(BaseModel):
    brier_score: Decimal = Field(ge=0)
    log_loss: Decimal = Field(ge=0)
    sample_count: int = Field(ge=1)


def calibration_metrics(
    probabilities: Sequence[Sequence[float]], labels: Sequence[int]
) -> CalibrationMetrics:
    if len(probabilities) != len(labels) or not labels:
        raise ValueError("probabilities and labels must have equal non-zero length")
    brier = 0.0
    loss = 0.0
    epsilon = 1e-15
    for prediction, label in zip(probabilities, labels, strict=True):
        if len(prediction) != 3 or label not in (0, 1, 2):
            raise ValueError("three-class probabilities and labels are required")
        total = sum(prediction)
        if abs(total - 1.0) > 1e-6:
            raise ValueError("probabilities must sum to one")
        brier += (
            sum(
                (value - (1.0 if index == label else 0.0)) ** 2
                for index, value in enumerate(prediction)
            )
            / 3
        )
        loss -= log(max(min(prediction[label], 1 - epsilon), epsilon))
    count = len(labels)
    return CalibrationMetrics(
        brier_score=Decimal(str(brier / count)),
        log_loss=Decimal(str(loss / count)),
        sample_count=count,
    )


class QuantIntelligenceV1:
    """Read-only inference adapter; it cannot create plans, orders, or positions."""

    def __init__(
        self,
        *,
        model_bytes: bytes,
        predict_proba: Callable[[Sequence[float]], Sequence[float]],
        calibrated: bool,
    ):
        self.model_fingerprint = sha256(model_bytes).hexdigest()
        self.predict_proba = predict_proba
        self.calibrated = calibrated

    def infer(
        self,
        *,
        symbol: str,
        data_as_of,
        features: Sequence[float],
        expected_return_after_cost: Decimal,
        expected_mfe: Decimal,
        expected_mae: Decimal,
    ) -> QuantPrediction:
        if symbol not in V2_SYMBOLS:
            raise ValueError("Quant Intelligence v1 only supports BTC/ETH/SOL OKX USDT swaps")
        probabilities = [Decimal(str(item)) for item in self.predict_proba(features)]
        if len(probabilities) != 3:
            raise ValueError("model must return Long TP, Short TP, Timeout probabilities")
        total = sum(probabilities, Decimal("0"))
        if total <= 0:
            raise ValueError("model probabilities must have positive mass")
        normalized = [item / total for item in probabilities]
        return QuantPrediction(
            model_fingerprint=self.model_fingerprint,
            symbol=symbol,
            data_as_of=data_as_of,
            expires_at=data_as_of + timedelta(minutes=10),
            probability_long_tp_first=normalized[0],
            probability_short_tp_first=normalized[1],
            probability_timeout=normalized[2],
            expected_return_after_cost=expected_return_after_cost,
            expected_mfe=expected_mfe,
            expected_mae=expected_mae,
            calibrated=self.calibrated,
        )


class QuantModelArtifactV1(BaseModel):
    """Portable LightGBM bundle for the three-class v1 contract."""

    model_config = ConfigDict(extra="forbid")

    version: str = "quant_intelligence_v1"
    symbol: str
    timeframe: str = "5m"
    feature_names: list[str] = Field(min_length=1)
    source_dataset_id: str | None = None
    source_data_fingerprint: str | None = None
    classifier_model: str
    expected_return_model: str
    mfe_model: str
    mae_model: str
    probability_temperature: Decimal = Field(gt=0)
    validation: CalibrationMetrics
    training_rows: int = Field(gt=0)
    data_as_of: datetime
    trained_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_fingerprint: str


UNIFORM_THREE_CLASS_BRIER = Decimal("0.2222222222222222")
UNIFORM_THREE_CLASS_LOG_LOSS = Decimal("1.0986122886681098")


def quant_model_is_eligible(artifact: QuantModelArtifactV1) -> bool:
    """Require purged OOF calibration to beat an uninformative 3-class model."""

    return (
        artifact.validation.sample_count >= 1000
        and artifact.validation.brier_score < UNIFORM_THREE_CLASS_BRIER
        and artifact.validation.log_loss < UNIFORM_THREE_CLASS_LOG_LOSS
    )


def _temperature_scale(probabilities, temperature: float):
    import numpy as np

    clipped = np.clip(probabilities, 1e-12, 1.0)
    powered = np.power(clipped, 1.0 / temperature)
    return powered / powered.sum(axis=1, keepdims=True)


def _best_temperature(probabilities, labels) -> float:
    best_temperature = 1.0
    best_loss = float("inf")
    for step in range(10, 41):
        temperature = step / 20
        scaled = _temperature_scale(probabilities, temperature)
        metric = calibration_metrics(scaled.tolist(), labels)
        if float(metric.log_loss) < best_loss:
            best_loss = float(metric.log_loss)
            best_temperature = temperature
    return best_temperature


def train_quant_intelligence_v1(
    *,
    symbol: str,
    candles: list[Candle],
    features_by_candle: Sequence[Sequence[float]],
    feature_names: Sequence[str],
    round_trip_cost_bps: Decimal = Decimal("8"),
    folds: int = 5,
    minimum_train: int = 500,
    source_dataset_id: str | None = None,
    source_data_fingerprint: str | None = None,
) -> QuantModelArtifactV1:
    """Train the BTC/ETH/SOL 5m LightGBM bundle with purged OOF calibration."""

    if symbol not in V2_SYMBOLS:
        raise ValueError("Quant Intelligence v1 supports only BTC/ETH/SOL OKX swaps")
    if len(features_by_candle) != len(candles):
        raise ValueError("one causal feature row is required for every candle")
    if not candles or any(
        candle.timestamp - candles[index - 1].timestamp != timedelta(minutes=5)
        for index, candle in enumerate(candles[1:], start=1)
    ):
        raise ValueError("training data must be a continuous 5m series")
    try:
        import numpy as np
        from lightgbm import LGBMClassifier, LGBMRegressor
    except ImportError as exc:
        raise RuntimeError("numpy and lightgbm are required for Quant training") from exc

    observations = atr_triple_barrier_labels(
        candles,
        target_atr=Decimal("2"),
        stop_atr=Decimal("1"),
        horizon_bars=24,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    valid_observations = []
    valid_features = []
    for item in observations:
        row = np.asarray(features_by_candle[item.signal_index], dtype=float)
        if row.ndim == 1 and len(row) == len(feature_names) and np.isfinite(row).all():
            valid_observations.append(item)
            valid_features.append(row)
    observations = valid_observations
    matrix = np.asarray(valid_features, dtype=float)
    labels = np.asarray([int(item.label) for item in observations], dtype=int)
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_names):
        raise ValueError("feature rows must match feature_names")
    if not np.isfinite(matrix).all():
        raise ValueError("training features must be finite; missing values require explicit health")
    split = purged_walk_forward_folds(
        len(observations),
        folds=folds,
        minimum_train=minimum_train,
        purge_bars=24,
        embargo_bars=24,
    )
    oof_probabilities: list[list[float]] = []
    oof_labels: list[int] = []
    classifier_parameters = {
        "objective": "multiclass",
        "num_class": 3,
        "n_estimators": 250,
        "learning_rate": 0.035,
        "num_leaves": 31,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "random_state": 20260829,
        "verbosity": -1,
    }
    for fold in split:
        classifier = LGBMClassifier(**classifier_parameters)
        classifier.fit(matrix[list(fold.train_indices)], labels[list(fold.train_indices)])
        probabilities = classifier.predict_proba(matrix[list(fold.validation_indices)])
        oof_probabilities.extend(probabilities.tolist())
        oof_labels.extend(labels[list(fold.validation_indices)].tolist())
    temperature = _best_temperature(np.asarray(oof_probabilities), oof_labels)
    scaled = _temperature_scale(np.asarray(oof_probabilities), temperature)
    validation = calibration_metrics(scaled.tolist(), oof_labels)

    classifier = LGBMClassifier(**classifier_parameters)
    classifier.fit(matrix, labels)
    regression_parameters = {
        "objective": "regression_l1",
        "n_estimators": 250,
        "learning_rate": 0.035,
        "num_leaves": 31,
        "random_state": 20260829,
        "verbosity": -1,
    }
    targets = (
        [float(item.expected_return_after_cost) for item in observations],
        [float(item.mfe) for item in observations],
        [float(item.mae) for item in observations],
    )
    regressors = []
    for values in targets:
        regressor = LGBMRegressor(**regression_parameters)
        regressor.fit(matrix, np.asarray(values, dtype=float))
        regressors.append(regressor)
    model_strings = [
        classifier.booster_.model_to_string(),
        *(item.booster_.model_to_string() for item in regressors),
    ]
    fingerprint_payload = json.dumps(
        {
            "version": "quant_intelligence_v1",
            "symbol": symbol,
            "features": list(feature_names),
            "source_dataset_id": source_dataset_id,
            "source_data_fingerprint": source_data_fingerprint,
            "models": [sha256(item.encode()).hexdigest() for item in model_strings],
            "temperature": temperature,
            "data_as_of": candles[-1].timestamp.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return QuantModelArtifactV1(
        symbol=symbol,
        feature_names=list(feature_names),
        source_dataset_id=source_dataset_id,
        source_data_fingerprint=source_data_fingerprint,
        classifier_model=model_strings[0],
        expected_return_model=model_strings[1],
        mfe_model=model_strings[2],
        mae_model=model_strings[3],
        probability_temperature=Decimal(str(temperature)),
        validation=validation,
        training_rows=len(observations),
        data_as_of=candles[-1].timestamp,
        model_fingerprint=sha256(fingerprint_payload.encode()).hexdigest(),
    )


class LightGBMQuantIntelligenceV1:
    """Inference-only artifact loader; no planning or execution capabilities exist here."""

    def __init__(self, artifact: QuantModelArtifactV1):
        try:
            from lightgbm import Booster
        except ImportError as exc:
            raise RuntimeError("lightgbm is required for Quant inference") from exc
        from threadpoolctl import threadpool_limits

        self.artifact = artifact
        # Loading model strings also enters OpenMP, before predict's explicit
        # num_threads option can apply. Bound that work on the loading thread
        # as well, so Windows does not retain a CPU-sized native worker team
        # for every asyncio executor thread that first loads a model.
        with threadpool_limits(limits=1, user_api="openmp"):
            self.classifier = Booster(model_str=artifact.classifier_model)
            self.expected_return = Booster(model_str=artifact.expected_return_model)
            self.mfe = Booster(model_str=artifact.mfe_model)
            self.mae = Booster(model_str=artifact.mae_model)

    def infer(
        self,
        *,
        data_as_of: datetime,
        features: Sequence[float],
    ) -> QuantPrediction:
        import numpy as np

        if len(features) != len(self.artifact.feature_names):
            raise ValueError("inference feature vector does not match model contract")
        matrix = np.asarray([features], dtype=float)
        if not np.isfinite(matrix).all():
            raise ValueError("missing features must not be zero-filled for Quant inference")
        # LightGBM's Windows OpenMP runtime keeps one native worker team for
        # every long-lived Python executor thread that calls ``predict``.  This
        # inference path is dispatched through ``asyncio.to_thread``; leaving
        # the default thread count in place therefore grows the API process to
        # roughly executor_workers * cpu_count threads over time.  A single-row
        # online prediction is faster and, crucially, bounded with one worker.
        predict_options = {"num_threads": 1}
        raw = np.asarray(
            self.classifier.predict(matrix, **predict_options),
            dtype=float,
        ).reshape(1, 3)
        probabilities = _temperature_scale(raw, float(self.artifact.probability_temperature))[0]
        return QuantPrediction(
            model_fingerprint=self.artifact.model_fingerprint,
            symbol=self.artifact.symbol,
            data_as_of=data_as_of,
            expires_at=data_as_of + timedelta(minutes=10),
            probability_long_tp_first=Decimal(str(probabilities[0])),
            probability_short_tp_first=Decimal(str(probabilities[1])),
            probability_timeout=Decimal(str(probabilities[2])),
            expected_return_after_cost=Decimal(
                str(self.expected_return.predict(matrix, **predict_options)[0])
            ),
            expected_mfe=Decimal(str(self.mfe.predict(matrix, **predict_options)[0])),
            expected_mae=Decimal(str(self.mae.predict(matrix, **predict_options)[0])),
            calibrated=True,
        )


def market_state_feature_vector(
    state_payload: dict,
    feature_names: Sequence[str],
) -> list[float]:
    """Resolve explicit dotted feature paths; absent/unhealthy data rejects inference."""

    values: list[float] = []
    for feature_name in feature_names:
        current = state_payload
        for component in feature_name.split("."):
            if not isinstance(current, dict) or component not in current:
                raise ValueError(f"Quant feature unavailable: {feature_name}")
            current = current[component]
        if current is None or isinstance(current, bool):
            raise ValueError(f"Quant feature unavailable: {feature_name}")
        try:
            values.append(float(current))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Quant feature is not numeric: {feature_name}") from exc
    return values
