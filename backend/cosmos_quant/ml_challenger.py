from __future__ import annotations

from decimal import Decimal
from math import exp, log

from pydantic import BaseModel, Field

from .domain import Candle
from .signals import score_market_state
from .strategy_spec import default_strategy_spec


class MLChallengerReport(BaseModel):
    model_kind: str = "regularized_logistic_direction_classifier"
    training_samples: int
    validation_samples: int
    training_accuracy: Decimal
    validation_accuracy: Decimal
    validation_brier_score: Decimal
    accepted: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    candidate_spec: dict | None = None


class DriftReport(BaseModel):
    baseline_samples: int
    recent_samples: int
    population_stability_index: Decimal
    volatility_ratio: Decimal
    mean_return_shift: Decimal
    status: str
    reasons: list[str]


def _feature_rows(candles: list[Candle]) -> tuple[list[list[float]], list[int]]:
    rows: list[list[float]] = []
    labels: list[int] = []
    for index in range(20, len(candles) - 1):
        close = float(candles[index].close)
        previous = float(candles[index - 1].close)
        five_back = float(candles[index - 5].close)
        recent = candles[index - 19 : index + 1]
        average_volume = sum(float(item.volume) for item in recent) / len(recent)
        rows.append(
            [
                (close / previous - 1) if previous else 0,
                (close / five_back - 1) if five_back else 0,
                (float(candles[index].high - candles[index].low) / close) if close else 0,
                (float(candles[index].volume) / average_volume - 1) if average_volume else 0,
            ]
        )
        labels.append(int(candles[index + 1].close > candles[index].close))
    return rows, labels


def _standardize(train: list[list[float]], validation: list[list[float]]):
    means = [sum(row[index] for row in train) / len(train) for index in range(4)]
    scales = []
    for index, mean in enumerate(means):
        variance = sum((row[index] - mean) ** 2 for row in train) / max(len(train) - 1, 1)
        scales.append(max(variance**0.5, 1e-9))

    def transform(rows):
        return [[(row[index] - means[index]) / scales[index] for index in range(4)] for row in rows]

    return transform(train), transform(validation)


def _fit_logistic(rows: list[list[float]], labels: list[int]) -> list[float]:
    weights = [0.0] * 5
    for _ in range(300):
        gradient = [0.0] * 5
        for row, label in zip(rows, labels, strict=True):
            vector = [1.0, *row]
            score = max(
                min(sum(w * x for w, x in zip(weights, vector, strict=True)), 30),
                -30,
            )
            error = 1 / (1 + exp(-score)) - label
            for index, value in enumerate(vector):
                gradient[index] += error * value
        for index in range(5):
            regularization = 0 if index == 0 else 0.01 * weights[index]
            weights[index] -= 0.08 * (gradient[index] / len(rows) + regularization)
    return weights


def _metrics(weights, rows, labels) -> tuple[Decimal, Decimal]:
    probabilities = []
    for row in rows:
        score = max(
            min(
                sum(w * x for w, x in zip(weights, [1.0, *row], strict=True)),
                30,
            ),
            -30,
        )
        probabilities.append(1 / (1 + exp(-score)))
    accuracy = sum(
        int((probability >= 0.5) == bool(label))
        for probability, label in zip(probabilities, labels, strict=True)
    ) / len(labels)
    brier = sum(
        (probability - label) ** 2 for probability, label in zip(probabilities, labels, strict=True)
    ) / len(labels)
    return Decimal(str(accuracy)), Decimal(str(brier))


def train_ml_challenger(candles: list[Candle]) -> MLChallengerReport:
    if len(candles) < 300:
        raise ValueError("ML Challenger 至少需要 300 根已收盘 K 线")
    rows, labels = _feature_rows(candles)
    split = int(len(rows) * 0.70)
    train_rows, validation_rows = rows[:split], rows[split:]
    train_labels, validation_labels = labels[:split], labels[split:]
    train_rows, validation_rows = _standardize(train_rows, validation_rows)
    weights = _fit_logistic(train_rows, train_labels)
    train_accuracy, _ = _metrics(weights, train_rows, train_labels)
    validation_accuracy, brier = _metrics(weights, validation_rows, validation_labels)
    reasons = []
    if len(validation_rows) < 80:
        reasons.append("验证集少于 80 个样本")
    if validation_accuracy < Decimal("0.52"):
        reasons.append("验证方向准确率低于 52%")
    if train_accuracy - validation_accuracy > Decimal("0.08"):
        reasons.append("训练与验证准确率差超过 8%，疑似过拟合")
    if brier > Decimal("0.25"):
        reasons.append("验证 Brier Score 高于 0.25")
    accepted = not reasons
    candidate_spec = None
    if accepted:
        kind = score_market_state(candles).selected_kind
        spec = default_strategy_spec(kind).model_copy(
            update={
                "source": "ml",
                "hypothesis": ("正则化方向分类器通过时间顺序验证；仅用于提出参数受限的研究候选"),
            }
        )
        candidate_spec = spec.model_dump(mode="json", exclude={"fingerprint"})
    return MLChallengerReport(
        training_samples=len(train_rows),
        validation_samples=len(validation_rows),
        training_accuracy=train_accuracy.quantize(Decimal("0.001")),
        validation_accuracy=validation_accuracy.quantize(Decimal("0.001")),
        validation_brier_score=brier.quantize(Decimal("0.001")),
        accepted=accepted,
        rejection_reasons=reasons,
        candidate_spec=candidate_spec,
    )


def detect_return_drift(candles: list[Candle]) -> DriftReport:
    if len(candles) < 200:
        raise ValueError("漂移监控至少需要 200 根 K 线")
    returns = [
        float(current.close / previous.close - 1)
        for previous, current in zip(candles, candles[1:], strict=False)
        if previous.close
    ]
    split = int(len(returns) * 0.70)
    baseline, recent = returns[:split], returns[split:]
    sorted_baseline = sorted(baseline)
    edges = [
        sorted_baseline[min(int(len(sorted_baseline) * q / 10), len(sorted_baseline) - 1)]
        for q in range(1, 10)
    ]

    def buckets(values):
        counts = [0] * 10
        for value in values:
            bucket = sum(value > edge for edge in edges)
            counts[bucket] += 1
        return [max(count / len(values), 1e-6) for count in counts]

    expected, actual = buckets(baseline), buckets(recent)
    psi = sum((actual[i] - expected[i]) * log(actual[i] / expected[i]) for i in range(10))

    def mean(values):
        return sum(values) / len(values)

    def volatility(values):
        center = mean(values)
        return (sum((value - center) ** 2 for value in values) / len(values)) ** 0.5

    baseline_volatility = max(volatility(baseline), 1e-12)
    volatility_ratio = volatility(recent) / baseline_volatility
    mean_shift = abs(mean(recent) - mean(baseline)) / baseline_volatility
    if psi >= 0.25 or volatility_ratio >= 2 or volatility_ratio <= 0.5:
        status = "critical"
    elif psi >= 0.10 or volatility_ratio >= 1.5 or volatility_ratio <= 0.67:
        status = "warning"
    else:
        status = "stable"
    return DriftReport(
        baseline_samples=len(baseline),
        recent_samples=len(recent),
        population_stability_index=Decimal(str(psi)).quantize(Decimal("0.001")),
        volatility_ratio=Decimal(str(volatility_ratio)).quantize(Decimal("0.001")),
        mean_return_shift=Decimal(str(mean_shift)).quantize(Decimal("0.001")),
        status=status,
        reasons=[
            "PSI >= 0.25 或波动率变化超过 2 倍时判定 critical",
            "critical 状态下禁止自动晋升 Challenger",
        ],
    )
