from __future__ import annotations

import math
import random
from decimal import Decimal
from itertools import combinations
from statistics import NormalDist

from pydantic import BaseModel, Field


class StatisticalValidationReport(BaseModel):
    """Uncertainty and data-mining evidence for one backtest."""

    trial_count: int = Field(ge=1)
    observation_count: int = Field(ge=0)
    one_sided_sharpe_p_value: Decimal = Field(ge=0, le=1)
    deflated_sharpe_probability: Decimal = Field(ge=0, le=1)
    win_rate_lower_bound_percent: Decimal = Field(ge=0, le=100)
    bootstrap_payoff_lower_bound: Decimal = Field(ge=0)
    monte_carlo_drawdown_95_percent: Decimal = Field(ge=0)
    false_discovery_rate_passed: bool
    uncertainty_passed: bool
    reasons: list[str] = Field(default_factory=list)


class BacktestOverfittingReport(BaseModel):
    """Bailey-style probability-of-overfitting approximation over time folds."""

    candidate_count: int
    fold_count: int
    split_count: int
    overfit_splits: int
    total_possible_splits: int = 0
    sampling_mode: str = "exact"
    standard_error: Decimal = Decimal("0")
    upper_confidence_bound: Decimal = Decimal("1")
    probability_of_backtest_overfitting: Decimal = Field(ge=0, le=1)
    passed: bool
    reasons: list[str] = Field(default_factory=list)


def wilson_lower_bound(
    wins: int,
    total: int,
    *,
    confidence: float = 0.95,
) -> Decimal:
    if total <= 0:
        return Decimal("0")
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    probability = wins / total
    denominator = 1 + z * z / total
    center = probability + z * z / (2 * total)
    margin = z * math.sqrt(probability * (1 - probability) / total + z * z / (4 * total * total))
    return Decimal(str(max((center - margin) / denominator, 0)))


def binomial_upper_tail_p_value(
    successes: int,
    trials: int,
    *,
    null_probability: Decimal = Decimal("0.5"),
) -> Decimal:
    """Exact one-sided binomial tail without a SciPy runtime dependency."""

    if trials <= 0 or successes < 0 or successes > trials:
        return Decimal("1")
    probability = float(null_probability)
    if not 0 < probability < 1:
        raise ValueError("null_probability must be strictly between zero and one")
    log_terms = [
        math.lgamma(trials + 1)
        - math.lgamma(outcome + 1)
        - math.lgamma(trials - outcome + 1)
        + outcome * math.log(probability)
        + (trials - outcome) * math.log1p(-probability)
        for outcome in range(successes, trials + 1)
    ]
    maximum_log = max(log_terms)
    tail = math.exp(maximum_log) * sum(math.exp(log_term - maximum_log) for log_term in log_terms)
    return Decimal(str(min(max(tail, 0.0), 1.0)))


def _sharpe_statistics(
    annualized_sharpe: Decimal,
    observation_count: int,
    annualization_periods: Decimal,
    trial_count: int,
) -> tuple[Decimal, Decimal]:
    if observation_count < 3 or annualization_periods <= 0:
        return Decimal("1"), Decimal("0")
    period_sharpe = float(annualized_sharpe) / math.sqrt(float(annualization_periods))
    standard_error = math.sqrt(
        max(1 + 0.5 * period_sharpe * period_sharpe, 1e-12) / max(observation_count - 1, 1)
    )
    null_z = period_sharpe / standard_error
    p_value = 1 - NormalDist().cdf(null_z)
    expected_max_z = (
        NormalDist().inv_cdf((trial_count - 0.375) / (trial_count + 0.25)) if trial_count > 1 else 0
    )
    deflated_z = null_z - expected_max_z
    probability = NormalDist().cdf(deflated_z)
    return (
        Decimal(str(min(max(p_value, 0), 1))),
        Decimal(str(min(max(probability, 0), 1))),
    )


def stouffer_combined_p_value(
    p_values: list[Decimal],
    *,
    weights: list[Decimal] | None = None,
) -> Decimal:
    """Combine pre-registered market p-values at the strategy level."""

    if not p_values:
        return Decimal("1")
    if weights is not None and len(weights) != len(p_values):
        raise ValueError("weights must match p_values")
    normalized_weights = weights or [Decimal("1")] * len(p_values)
    weighted_z = 0.0
    squared_weight_sum = 0.0
    for p_value, weight in zip(p_values, normalized_weights, strict=True):
        bounded_p = min(max(float(p_value), 1e-12), 1 - 1e-12)
        numeric_weight = max(float(weight), 0.0)
        if numeric_weight == 0:
            continue
        weighted_z += numeric_weight * NormalDist().inv_cdf(1 - bounded_p)
        squared_weight_sum += numeric_weight * numeric_weight
    if squared_weight_sum == 0:
        return Decimal("1")
    combined_z = weighted_z / math.sqrt(squared_weight_sum)
    combined_p = 1 - NormalDist().cdf(combined_z)
    return Decimal(str(min(max(combined_p, 0.0), 1.0)))


def cauchy_combined_p_value(p_values: list[Decimal]) -> Decimal:
    """Combine p-values without assuming independent markets.

    Crypto markets can remain strongly correlated even after collapsing
    overlapping timeframes within each symbol.  The Cauchy combination test
    keeps the strategy-level tail calibration robust under arbitrary
    dependence instead of treating correlated symbols as independent votes.
    """

    if not p_values:
        return Decimal("1")
    bounded = [min(max(float(value), 1e-15), 1 - 1e-15) for value in p_values]
    statistic = sum(math.tan((0.5 - value) * math.pi) for value in bounded) / len(bounded)
    combined = 0.5 - math.atan(statistic) / math.pi
    return Decimal(str(min(max(combined, 0.0), 1.0)))


def benjamini_hochberg_adjusted_p_values(
    p_values: dict[str, Decimal],
    *,
    total_hypothesis_count: int | None = None,
) -> dict[str, Decimal]:
    """Return monotone BH values, optionally charged to a larger research family."""

    if not p_values:
        return {}
    ordered = sorted(p_values.items(), key=lambda item: (item[1], item[0]))
    count = max(len(ordered), total_hypothesis_count or 0)
    adjusted: dict[str, Decimal] = {}
    running = Decimal("1")
    for reverse_index in range(len(ordered) - 1, -1, -1):
        key, p_value = ordered[reverse_index]
        rank = reverse_index + 1
        candidate = min(p_value * Decimal(count) / Decimal(rank), Decimal("1"))
        running = min(running, candidate)
        adjusted[key] = running
    return adjusted


def bootstrap_payoff_lower_bound(
    pnls: list[Decimal],
    *,
    samples: int = 500,
    confidence: float = 0.95,
    seed: int = 20260729,
) -> Decimal:
    if len(pnls) < 2 or not any(item > 0 for item in pnls) or not any(item < 0 for item in pnls):
        return Decimal("0")
    rng = random.Random(seed)
    ratios: list[float] = []
    population = [float(item) for item in pnls]
    for _ in range(samples):
        sample = rng.choices(population, k=len(population))
        wins = [item for item in sample if item > 0]
        losses = [-item for item in sample if item < 0]
        if wins and losses:
            ratios.append((sum(wins) / len(wins)) / (sum(losses) / len(losses)))
    if not ratios:
        return Decimal("0")
    ratios.sort()
    index = max(0, int(len(ratios) * (1 - confidence)) - 1)
    return Decimal(str(ratios[index]))


def monte_carlo_drawdown_percentile(
    pnls: list[Decimal],
    initial_equity: Decimal,
    *,
    samples: int = 500,
    percentile: float = 0.95,
    seed: int = 20260729,
) -> Decimal:
    if not pnls or initial_equity <= 0:
        return Decimal("0")
    rng = random.Random(seed)
    drawdowns: list[Decimal] = []
    for _ in range(samples):
        path = list(pnls)
        rng.shuffle(path)
        equity = initial_equity
        peak = initial_equity
        maximum = Decimal("0")
        for pnl in path:
            equity += pnl
            peak = max(peak, equity)
            if peak > 0:
                maximum = max(maximum, (peak - equity) / peak * Decimal("100"))
        drawdowns.append(maximum)
    drawdowns.sort()
    index = min(len(drawdowns) - 1, int((len(drawdowns) - 1) * percentile))
    return drawdowns[index]


def validate_statistical_evidence(
    *,
    annualized_sharpe: Decimal,
    annualization_periods: Decimal,
    observation_count: int,
    pnls: list[Decimal],
    initial_equity: Decimal,
    trial_count: int = 1,
    minimum_win_rate_percent: Decimal = Decimal("45"),
    minimum_payoff_ratio: Decimal = Decimal("1.20"),
    maximum_drawdown_percent: Decimal = Decimal("20"),
    minimum_deflated_sharpe_probability: Decimal = Decimal("0.90"),
) -> StatisticalValidationReport:
    trial_count = max(trial_count, 1)
    wins = sum(item > 0 for item in pnls)
    win_lower = wilson_lower_bound(wins, len(pnls)) * Decimal("100")
    payoff_lower = bootstrap_payoff_lower_bound(pnls)
    drawdown_95 = monte_carlo_drawdown_percentile(pnls, initial_equity)
    p_value, deflated_probability = _sharpe_statistics(
        annualized_sharpe,
        observation_count,
        annualization_periods,
        trial_count,
    )
    # Benjamini-Hochberg rank-one threshold. A complete batch report can
    # replace rank one with the selected candidate's actual rank.
    fdr_threshold = Decimal("0.05") / Decimal(trial_count)
    fdr_passed = p_value <= fdr_threshold
    reasons: list[str] = []
    if win_lower < minimum_win_rate_percent:
        reasons.append(f"胜率 95% Wilson 下界 {win_lower:.2f}% 低于 {minimum_win_rate_percent}%")
    if payoff_lower < minimum_payoff_ratio:
        reasons.append(f"盈亏比 bootstrap 下界 {payoff_lower:.2f} 低于 {minimum_payoff_ratio}")
    if drawdown_95 > maximum_drawdown_percent:
        reasons.append(
            f"交易顺序 Monte Carlo 95% 回撤 {drawdown_95:.2f}% 超过 {maximum_drawdown_percent}%"
        )
    if deflated_probability < minimum_deflated_sharpe_probability:
        reasons.append(
            f"Deflated Sharpe 概率 {deflated_probability:.3f} 低于 "
            f"{minimum_deflated_sharpe_probability}"
        )
    if not fdr_passed:
        reasons.append(f"单侧 Sharpe p 值 {p_value:.4f} 未通过 {trial_count} 次试验修正")
    return StatisticalValidationReport(
        trial_count=trial_count,
        observation_count=observation_count,
        one_sided_sharpe_p_value=p_value.quantize(Decimal("0.0001")),
        deflated_sharpe_probability=deflated_probability.quantize(Decimal("0.0001")),
        win_rate_lower_bound_percent=win_lower.quantize(Decimal("0.01")),
        bootstrap_payoff_lower_bound=payoff_lower.quantize(Decimal("0.01")),
        monte_carlo_drawdown_95_percent=drawdown_95.quantize(Decimal("0.01")),
        false_discovery_rate_passed=fdr_passed,
        uncertainty_passed=not reasons,
        reasons=reasons or ["统计不确定性与多重试验门槛全部通过"],
    )


def estimate_probability_of_backtest_overfitting(
    fold_returns_by_candidate: list[list[Decimal]],
    *,
    maximum_probability: Decimal = Decimal("0.20"),
    maximum_splits: int = 10_000,
    seed: int = 20260729,
) -> BacktestOverfittingReport:
    if not fold_returns_by_candidate:
        return BacktestOverfittingReport(
            candidate_count=0,
            fold_count=0,
            split_count=0,
            overfit_splits=0,
            total_possible_splits=0,
            probability_of_backtest_overfitting=Decimal("1"),
            passed=False,
            reasons=["缺少候选策略分段收益"],
        )
    fold_count = len(fold_returns_by_candidate[0])
    if (
        len(fold_returns_by_candidate) < 2
        or fold_count < 4
        or fold_count % 2
        or any(len(row) != fold_count for row in fold_returns_by_candidate)
    ):
        return BacktestOverfittingReport(
            candidate_count=len(fold_returns_by_candidate),
            fold_count=fold_count,
            split_count=0,
            overfit_splits=0,
            total_possible_splits=0,
            probability_of_backtest_overfitting=Decimal("1"),
            passed=False,
            reasons=["PBO 至少需要两个候选和四个偶数时间分段"],
        )
    half = fold_count // 2
    total_possible_splits = math.comb(fold_count, half) // 2
    if total_possible_splits <= maximum_splits:
        in_sample_sets = (
            tuple(indices) for indices in combinations(range(fold_count), half) if 0 in indices
        )
        sampling_mode = "exact"
    else:
        rng = random.Random(seed)
        sampled: set[tuple[int, ...]] = set()
        all_indices_tuple = tuple(range(fold_count))
        while len(sampled) < maximum_splits:
            selected = set(rng.sample(all_indices_tuple, half))
            if 0 not in selected:
                selected = set(all_indices_tuple) - selected
            sampled.add(tuple(sorted(selected)))
        in_sample_sets = iter(sorted(sampled))
        sampling_mode = "deterministic_sample"
    split_count = 0
    overfit_splits = 0
    all_indices = set(range(fold_count))
    for in_sample_indices in in_sample_sets:
        out_sample_indices = sorted(all_indices - set(in_sample_indices))
        in_scores = [
            sum(row[index] for index in in_sample_indices) for row in fold_returns_by_candidate
        ]
        winner = max(range(len(in_scores)), key=in_scores.__getitem__)
        out_scores = [
            sum(row[index] for index in out_sample_indices) for row in fold_returns_by_candidate
        ]
        ordered = sorted(out_scores)
        rank = ordered.index(out_scores[winner])
        if rank < len(ordered) / 2:
            overfit_splits += 1
        split_count += 1
    probability = Decimal(overfit_splits) / Decimal(split_count) if split_count else Decimal("1")
    standard_error = (
        Decimal(str(math.sqrt(float(probability * (Decimal("1") - probability)) / split_count)))
        if split_count
        else Decimal("0")
    )
    upper_confidence_bound = min(
        probability + Decimal("1.96") * standard_error,
        Decimal("1"),
    )
    passed = upper_confidence_bound <= maximum_probability
    return BacktestOverfittingReport(
        candidate_count=len(fold_returns_by_candidate),
        fold_count=fold_count,
        split_count=split_count,
        overfit_splits=overfit_splits,
        total_possible_splits=total_possible_splits,
        sampling_mode=sampling_mode,
        standard_error=standard_error.quantize(Decimal("0.0001")),
        upper_confidence_bound=upper_confidence_bound.quantize(Decimal("0.001")),
        probability_of_backtest_overfitting=probability.quantize(Decimal("0.001")),
        passed=passed,
        reasons=[
            (
                f"PBO 95% 上界 {upper_confidence_bound:.3f} 不高于门槛 "
                f"{maximum_probability}（{sampling_mode}，{split_count} 个切分）"
                if passed
                else f"PBO 95% 上界 {upper_confidence_bound:.3f} 高于门槛 "
                f"{maximum_probability}（{sampling_mode}，{split_count} 个切分）"
            )
        ],
    )
