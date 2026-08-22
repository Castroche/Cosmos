from __future__ import annotations

import json
import math
from bisect import bisect_right
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from statistics import mean, pstdev
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .domain import Candle, FundingRatePoint
from .statistical_validation import (
    StatisticalValidationReport,
    validate_statistical_evidence,
)
from .timeframes import periods_per_year

NBER_PAIRS_TRADING = "https://www.nber.org/papers/w7032"
ENGLE_GRANGER_COINTEGRATION = "https://www.jstor.org/stable/1913236"
CROSS_SECTIONAL_MOMENTUM = (
    "https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1993.tb04702.x"
)
PAIR_BACKTEST_ENGINE_VERSION = 6


class PairStrategySpec(BaseModel):
    version: int = 2
    model: Literal[
        "log_spread_equal_dollar",
        "rolling_beta_residual",
        "relative_momentum_equal_dollar",
    ] = "log_spread_equal_dollar"
    formation_window: int = Field(default=120, ge=60, le=1000)
    entry_zscore: Decimal = Field(default=Decimal("2"), ge=Decimal("1"), le=5)
    exit_zscore: Decimal = Field(default=Decimal("0.5"), ge=0, le=2)
    stop_zscore: Decimal = Field(default=Decimal("4"), ge=2, le=10)
    maximum_holding_bars: int = Field(default=120, ge=2, le=2000)
    gross_leverage: Decimal = Field(default=Decimal("1"), gt=0, le=3)
    minimum_hedge_ratio: Decimal = Field(default=Decimal("0.25"), gt=0, le=2)
    maximum_hedge_ratio: Decimal = Field(default=Decimal("4"), ge=1, le=10)
    source_url: str = NBER_PAIRS_TRADING
    hypothesis: str = (
        "等美元多空两个同步市场，在历史对数价格比偏离滚动均值后研究收敛。"
        "这是可证伪相对价值假设，不是无风险套利。"
    )

    @model_validator(mode="before")
    @classmethod
    def bind_model_provenance(cls, payload):
        if isinstance(payload, dict) and payload.get("model") == "rolling_beta_residual":
            normalized = dict(payload)
            normalized.setdefault("source_url", ENGLE_GRANGER_COINTEGRATION)
            normalized.setdefault(
                "hypothesis",
                "仅用当时已收盘窗口估计对数价格滚动 OLS 对冲比率，研究残差偏离后的"
                "收敛；它是可证伪先验而不是无风险套利或协整结论。",
            )
            return normalized
        if isinstance(payload, dict) and payload.get("model") == "relative_momentum_equal_dollar":
            normalized = dict(payload)
            normalized.setdefault("source_url", CROSS_SECTIONAL_MOMENTUM)
            normalized.setdefault(
                "hypothesis",
                "仅用当时已收盘窗口比较两个市场的相对收益趋势，下一根同步开盘"
                "做多强者并做空弱者；这是可证伪跨市场动量先验，不是盈利声明。",
            )
            return normalized
        return payload

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.exit_zscore >= self.entry_zscore:
            raise ValueError("pair exit z-score must be below entry z-score")
        if self.stop_zscore <= self.entry_zscore:
            raise ValueError("pair stop z-score must exceed entry z-score")
        if self.minimum_hedge_ratio >= self.maximum_hedge_ratio:
            raise ValueError("minimum pair hedge ratio must be below maximum")
        return self

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()[:16]


class PairTrade(BaseModel):
    entry_time: datetime
    exit_time: datetime
    long_symbol: str
    short_symbol: str
    entry_zscore: Decimal
    exit_zscore: Decimal
    entry_hedge_ratio: Decimal = Decimal("1")
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    net_pnl: Decimal
    return_percent: Decimal = Decimal("0")
    holding_bars: int
    exit_reason: str


class PairResidualStabilityEvidence(BaseModel):
    """Formation-only mean-reversion premise plus untouched-period break checks.

    The Dickey-Fuller statistic is the intercept-only, zero-lag statistic.  Its
    large-sample 5% critical value is reported explicitly instead of presenting
    a fabricated p-value.  It is one conservative research gate, not proof of
    cointegration.
    """

    applicable: bool = False
    formation_observations: int = 0
    evaluation_observations: int = 0
    calibration_data_end: datetime | None = None
    evaluation_data_start: datetime | None = None
    formation_hedge_ratio: Decimal | None = None
    dickey_fuller_t_statistic: Decimal | None = None
    dickey_fuller_critical_value_5_percent: Decimal = Decimal("-2.86")
    ar1_coefficient: Decimal | None = None
    estimated_half_life_bars: Decimal | None = None
    formation_mean_shift_z: Decimal | None = None
    formation_variance_ratio: Decimal | None = None
    evaluation_mean_shift_z: Decimal | None = None
    evaluation_variance_ratio: Decimal | None = None
    formation_stationarity_passed: bool = False
    formation_structure_stable: bool = False
    evaluation_structure_stable: bool = False
    passed: bool = False
    reasons: list[str] = Field(default_factory=list)


class PairBacktestReport(BaseModel):
    engine_version: int = 1
    id: str = Field(default_factory=lambda: f"pairbt_{uuid4().hex[:16]}")
    strategy_spec: PairStrategySpec
    strategy_fingerprint: str
    dataset_a_id: str
    dataset_b_id: str
    data_fingerprint_a: str
    data_fingerprint_b: str
    symbol_a: str
    symbol_b: str
    timeframe: str
    aligned_bars: int
    formation_bars: int = 0
    evaluation_bars: int = 0
    evaluation_start: datetime | None = None
    data_start: datetime
    data_end: datetime
    initial_equity: Decimal
    final_equity: Decimal
    return_percent: Decimal
    max_drawdown_percent: Decimal
    total_trades: int
    win_rate_percent: Decimal
    payoff_ratio: Decimal
    profit_factor: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    total_funding: Decimal
    funding_coverage_percent_a: Decimal = Decimal("0")
    funding_coverage_percent_b: Decimal = Decimal("0")
    terminal_position_closed: bool
    statistical_validation: StatisticalValidationReport
    residual_stability: PairResidualStabilityEvidence = Field(
        default_factory=PairResidualStabilityEvidence
    )
    research_gate_passed: bool = False
    robustness_passed: bool
    execution_eligible: Literal[False] = False
    validation_reasons: list[str]
    integrity_checks: dict[str, bool | int | str]
    trades: list[PairTrade]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


PAIR_STRATEGY_LIBRARY: tuple[PairStrategySpec, ...] = (
    PairStrategySpec(formation_window=120, entry_zscore=Decimal("2")),
    PairStrategySpec(
        formation_window=240,
        entry_zscore=Decimal("2.5"),
        exit_zscore=Decimal("0.5"),
        stop_zscore=Decimal("4.5"),
        maximum_holding_bars=240,
    ),
    PairStrategySpec(
        model="rolling_beta_residual",
        formation_window=120,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.5"),
        stop_zscore=Decimal("4"),
        maximum_holding_bars=120,
        source_url=ENGLE_GRANGER_COINTEGRATION,
        hypothesis=(
            "仅用当时已收盘窗口估计对数价格滚动 OLS 对冲比率，研究残差偏离后的"
            "收敛；对冲比率在入场时冻结，不能据此宣称两个市场已协整。"
        ),
    ),
    PairStrategySpec(
        model="rolling_beta_residual",
        formation_window=240,
        entry_zscore=Decimal("2.5"),
        exit_zscore=Decimal("0.5"),
        stop_zscore=Decimal("4.5"),
        maximum_holding_bars=240,
        source_url=ENGLE_GRANGER_COINTEGRATION,
        hypothesis=(
            "仅用当时已收盘窗口估计对数价格滚动 OLS 对冲比率，研究较慢残差偏离后的"
            "收敛；它是可证伪先验而不是无风险套利或协整结论。"
        ),
    ),
    PairStrategySpec(
        model="relative_momentum_equal_dollar",
        formation_window=96,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.25"),
        stop_zscore=Decimal("3"),
        maximum_holding_bars=96,
        source_url=CROSS_SECTIONAL_MOMENTUM,
        hypothesis=(
            "仅用过去 96 根已收盘 K 线比较相对收益趋势，下一根同步开盘做多强者并"
            "做空弱者；研究短周期相对动量是否覆盖双腿成本。"
        ),
    ),
    PairStrategySpec(
        model="relative_momentum_equal_dollar",
        formation_window=480,
        entry_zscore=Decimal("2.5"),
        exit_zscore=Decimal("0.25"),
        stop_zscore=Decimal("3.5"),
        maximum_holding_bars=192,
        source_url=CROSS_SECTIONAL_MOMENTUM,
        hypothesis=(
            "仅用过去 480 根已收盘 K 线比较相对收益趋势，下一根同步开盘做多强者并"
            "做空弱者；研究较慢相对动量是否在多交易所一致。"
        ),
    ),
)


def _prefix(values: list[float]) -> list[float]:
    result = [0.0]
    total = 0.0
    for value in values:
        total += value
        result.append(total)
    return result


def _window_total(prefix: list[float], start: int, end: int) -> float:
    return prefix[end + 1] - prefix[start]


def _causal_pair_signals(
    aligned: list[tuple[Candle, Candle]],
    spec: PairStrategySpec,
) -> list[tuple[Decimal, Decimal] | None]:
    """Compute bar-close pair signals with no future observations.

    The rolling-beta model estimates ``log(A) = alpha + beta * log(B)`` on
    the formation window ending at the signal bar.  The residual z-score and
    hedge ratio therefore exist only after that bar closes; execution remains
    delayed to the next synchronized open by ``run_pair_backtest``.
    """

    log_a = [math.log(float(item_a.close)) for item_a, _ in aligned]
    log_b = [math.log(float(item_b.close)) for _, item_b in aligned]
    difference = [left - right for left, right in zip(log_a, log_b, strict=True)]
    prefix_a = _prefix(log_a)
    prefix_b = _prefix(log_b)
    prefix_aa = _prefix([value * value for value in log_a])
    prefix_bb = _prefix([value * value for value in log_b])
    prefix_ab = _prefix(
        [left * right for left, right in zip(log_a, log_b, strict=True)]
    )
    prefix_difference = _prefix(difference)
    prefix_difference_squared = _prefix([value * value for value in difference])
    relative_returns = [
        0.0,
        *(difference[index] - difference[index - 1] for index in range(1, len(difference))),
    ]
    prefix_relative_returns = _prefix(relative_returns)
    prefix_relative_returns_squared = _prefix(
        [value * value for value in relative_returns]
    )
    signals: list[tuple[Decimal, Decimal] | None] = [None] * len(aligned)
    window = spec.formation_window
    minimum_beta = float(spec.minimum_hedge_ratio)
    maximum_beta = float(spec.maximum_hedge_ratio)
    for index in range(window - 1, len(aligned)):
        start = index - window + 1
        count = float(window)
        if spec.model == "relative_momentum_equal_dollar":
            return_start = start + 1
            return_count = float(window - 1)
            total_return = _window_total(
                prefix_relative_returns,
                return_start,
                index,
            )
            total_return_squared = _window_total(
                prefix_relative_returns_squared,
                return_start,
                index,
            )
            average_return = total_return / return_count
            return_variance = max(
                total_return_squared / return_count - average_return * average_return,
                0.0,
            )
            beta = 1.0
            residual = difference[index] - difference[start]
            variance = return_variance * return_count
        elif spec.model == "log_spread_equal_dollar":
            total = _window_total(prefix_difference, start, index)
            total_squared = _window_total(prefix_difference_squared, start, index)
            average = total / count
            variance = max(total_squared / count - average * average, 0.0)
            beta = 1.0
            residual = difference[index] - average
        else:
            total_a = _window_total(prefix_a, start, index)
            total_b = _window_total(prefix_b, start, index)
            total_aa = _window_total(prefix_aa, start, index)
            total_bb = _window_total(prefix_bb, start, index)
            total_ab = _window_total(prefix_ab, start, index)
            average_a = total_a / count
            average_b = total_b / count
            variance_a = max(total_aa / count - average_a * average_a, 0.0)
            variance_b = max(total_bb / count - average_b * average_b, 0.0)
            covariance = total_ab / count - average_a * average_b
            if variance_b <= 1e-15:
                continue
            beta = covariance / variance_b
            if not math.isfinite(beta) or not minimum_beta <= abs(beta) <= maximum_beta:
                continue
            intercept = average_a - beta * average_b
            residual = log_a[index] - intercept - beta * log_b[index]
            variance = max(variance_a - covariance * covariance / variance_b, 0.0)
        deviation = math.sqrt(variance)
        if deviation <= 1e-12 or not math.isfinite(residual):
            continue
        zscore = residual / deviation
        if math.isfinite(zscore):
            signals[index] = (Decimal(str(zscore)), Decimal(str(beta)))
    return signals


def latest_causal_pair_signal(
    candles_a: list[Candle],
    candles_b: list[Candle],
    spec: PairStrategySpec,
) -> tuple[datetime, Decimal, Decimal] | None:
    """Return the latest synchronized closed-bar signal used by the backtest.

    Execution monitoring calls the same causal implementation as research so
    it cannot silently substitute a future-aware or differently fitted signal.
    """

    aligned = _aligned_candles(candles_a, candles_b)
    if len(aligned) < spec.formation_window:
        return None
    signals = _causal_pair_signals(aligned, spec)
    for index in range(len(signals) - 1, -1, -1):
        signal = signals[index]
        if signal is not None:
            return aligned[index][0].timestamp, signal[0], signal[1]
    return None


class _FundingLedger:
    """Causal prefix ledger for repeated interval funding queries."""

    def __init__(self, points: list[FundingRatePoint]):
        ordered = sorted(points, key=lambda item: item.timestamp)
        self.timestamps = [item.timestamp for item in ordered]
        self.cumulative_rates = [Decimal("0")]
        total = Decimal("0")
        for item in ordered:
            total += item.rate
            self.cumulative_rates.append(total)

    def rate_between(self, entry_time: datetime, exit_time: datetime) -> Decimal:
        left = bisect_right(self.timestamps, entry_time)
        right = bisect_right(self.timestamps, exit_time)
        return self.cumulative_rates[right] - self.cumulative_rates[left]


def _funding_cost(
    ledger: _FundingLedger,
    *,
    entry_time: datetime,
    exit_time: datetime,
    side: int,
    notional: Decimal,
) -> Decimal:
    return ledger.rate_between(entry_time, exit_time) * notional * Decimal(side)


def _aligned_candles(
    candles_a: list[Candle],
    candles_b: list[Candle],
) -> list[tuple[Candle, Candle]]:
    by_time_b = {item.timestamp: item for item in candles_b}
    return [(item, by_time_b[item.timestamp]) for item in candles_a if item.timestamp in by_time_b]


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    return sum((value - average) ** 2 for value in values) / len(values)


def _segment_shift(
    reference: list[float],
    candidate: list[float],
) -> tuple[float, float]:
    """Return normalized mean shift and symmetric variance ratio."""

    reference_variance = _variance(reference)
    candidate_variance = _variance(candidate)
    scale = math.sqrt(max(reference_variance, 1e-18))
    mean_shift = abs(mean(candidate) - mean(reference)) / scale
    lower_variance = min(reference_variance, candidate_variance)
    upper_variance = max(reference_variance, candidate_variance)
    variance_ratio = (
        upper_variance / lower_variance
        if lower_variance > 1e-18
        else (1.0 if upper_variance <= 1e-18 else math.inf)
    )
    return mean_shift, variance_ratio


def _finite_metric(value: float, *, ceiling: float = 1_000_000_000.0) -> Decimal:
    """Keep diagnostic payloads finite and JSON-portable under degenerate samples."""

    if not math.isfinite(value):
        return Decimal(str(ceiling))
    return Decimal(str(max(min(value, ceiling), -ceiling)))


def _residual_stability_evidence(
    aligned: list[tuple[Candle, Candle]],
    spec: PairStrategySpec,
    evaluation_index: int,
) -> PairResidualStabilityEvidence:
    """Test a mean-reversion premise without fitting on evaluation observations."""

    if spec.model == "relative_momentum_equal_dollar":
        return PairResidualStabilityEvidence(
            applicable=False,
            formation_observations=evaluation_index,
            evaluation_observations=len(aligned) - evaluation_index,
            calibration_data_end=aligned[evaluation_index - 1][0].timestamp,
            evaluation_data_start=aligned[evaluation_index][0].timestamp,
            passed=True,
            reasons=["relative-momentum hypothesis does not assume residual stationarity"],
        )

    log_a = [math.log(float(item_a.close)) for item_a, _ in aligned]
    log_b = [math.log(float(item_b.close)) for _, item_b in aligned]
    formation_a = log_a[:evaluation_index]
    formation_b = log_b[:evaluation_index]
    hedge_ratio = 1.0
    intercept = 0.0
    reasons: list[str] = []
    if spec.model == "rolling_beta_residual":
        average_a = mean(formation_a)
        average_b = mean(formation_b)
        variance_b = _variance(formation_b)
        covariance = mean(
            [
                (left - average_a) * (right - average_b)
                for left, right in zip(formation_a, formation_b, strict=True)
            ]
        )
        if variance_b <= 1e-18:
            reasons.append("formation-period hedge regression has zero explanatory variance")
        else:
            hedge_ratio = covariance / variance_b
            intercept = average_a - hedge_ratio * average_b
        if not (
            math.isfinite(hedge_ratio)
            and float(spec.minimum_hedge_ratio)
            <= abs(hedge_ratio)
            <= float(spec.maximum_hedge_ratio)
        ):
            reasons.append("formation-period hedge ratio is outside the declared bounds")

    residuals = [
        left - intercept - hedge_ratio * right
        for left, right in zip(log_a, log_b, strict=True)
    ]
    formation = residuals[:evaluation_index]
    evaluation = residuals[evaluation_index:]
    lagged = formation[:-1]
    changes = [
        formation[index] - formation[index - 1]
        for index in range(1, len(formation))
    ]
    df_t: float | None = None
    ar1_coefficient: float | None = None
    half_life: float | None = None
    if len(lagged) < 30 or _variance(lagged) <= 1e-18:
        reasons.append("formation residual has insufficient non-constant observations")
    else:
        average_lagged = mean(lagged)
        average_change = mean(changes)
        centered_sum = sum((value - average_lagged) ** 2 for value in lagged)
        gamma = sum(
            (left - average_lagged) * (right - average_change)
            for left, right in zip(lagged, changes, strict=True)
        ) / centered_sum
        intercept_change = average_change - gamma * average_lagged
        errors = [
            right - intercept_change - gamma * left
            for left, right in zip(lagged, changes, strict=True)
        ]
        residual_variance = sum(value * value for value in errors) / max(
            len(errors) - 2,
            1,
        )
        standard_error = math.sqrt(residual_variance / centered_sum)
        df_t = gamma / standard_error if standard_error > 1e-18 else None
        ar1_coefficient = 1.0 + gamma
        if ar1_coefficient is not None and 0.0 < ar1_coefficient < 1.0:
            half_life = -math.log(2.0) / math.log(ar1_coefficient)
        elif ar1_coefficient is not None and -1.0 < ar1_coefficient <= 0.0:
            half_life = 0.5

    split = len(formation) // 2
    formation_mean_shift, formation_variance_ratio = _segment_shift(
        formation[:split],
        formation[split:],
    )
    evaluation_mean_shift, evaluation_variance_ratio = _segment_shift(
        formation,
        evaluation,
    )
    stationarity_passed = bool(
        df_t is not None
        and df_t <= -2.86
        and ar1_coefficient is not None
        and -1.0 < ar1_coefficient < 1.0
        and half_life is not None
        and half_life <= spec.maximum_holding_bars
    )
    formation_structure_stable = bool(
        math.isfinite(formation_mean_shift)
        and formation_mean_shift <= 1.5
        and math.isfinite(formation_variance_ratio)
        and formation_variance_ratio <= 4.0
    )
    evaluation_structure_stable = bool(
        math.isfinite(evaluation_mean_shift)
        and evaluation_mean_shift <= 2.0
        and math.isfinite(evaluation_variance_ratio)
        and evaluation_variance_ratio <= 4.0
    )
    if not stationarity_passed:
        reasons.append(
            "formation-only residual failed the Dickey-Fuller/half-life stationarity gate"
        )
    if not formation_structure_stable:
        reasons.append("formation residual has an internal mean or variance break")
    if not evaluation_structure_stable:
        reasons.append("evaluation residual shifted beyond formation mean or variance bounds")
    return PairResidualStabilityEvidence(
        applicable=True,
        formation_observations=len(formation),
        evaluation_observations=len(evaluation),
        calibration_data_end=aligned[evaluation_index - 1][0].timestamp,
        evaluation_data_start=aligned[evaluation_index][0].timestamp,
        formation_hedge_ratio=Decimal(str(hedge_ratio)),
        dickey_fuller_t_statistic=(Decimal(str(df_t)) if df_t is not None else None),
        ar1_coefficient=(
            Decimal(str(ar1_coefficient)) if ar1_coefficient is not None else None
        ),
        estimated_half_life_bars=(
            Decimal(str(half_life)) if half_life is not None else None
        ),
        formation_mean_shift_z=_finite_metric(formation_mean_shift),
        formation_variance_ratio=_finite_metric(formation_variance_ratio),
        evaluation_mean_shift_z=_finite_metric(evaluation_mean_shift),
        evaluation_variance_ratio=_finite_metric(evaluation_variance_ratio),
        formation_stationarity_passed=stationarity_passed,
        formation_structure_stable=formation_structure_stable,
        evaluation_structure_stable=evaluation_structure_stable,
        passed=not reasons,
        reasons=list(dict.fromkeys(reasons)),
    )


def _funding_coverage_percent(
    points: list[FundingRatePoint],
    *,
    data_start: datetime,
    data_end: datetime,
) -> Decimal:
    if len(points) < 2 or data_end <= data_start:
        return Decimal("0")
    ordered = sorted(points, key=lambda item: item.timestamp)
    covered_start = max(ordered[0].timestamp, data_start)
    covered_end = min(ordered[-1].timestamp, data_end)
    if covered_end <= covered_start:
        return Decimal("0")
    covered_seconds = Decimal(str((covered_end - covered_start).total_seconds()))
    total_seconds = Decimal(str((data_end - data_start).total_seconds()))
    return min(
        covered_seconds / total_seconds * Decimal("100"),
        Decimal("100"),
    )


def run_pair_backtest(
    *,
    dataset_a,
    candles_a: list[Candle],
    funding_a: list[FundingRatePoint],
    dataset_b,
    candles_b: list[Candle],
    funding_b: list[FundingRatePoint],
    strategy_spec: PairStrategySpec | dict | None = None,
    initial_equity: Decimal = Decimal("100000"),
    fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
    research_trial_count: int = 1,
    evaluation_start_index: int | None = None,
) -> PairBacktestReport:
    spec = (
        strategy_spec
        if isinstance(strategy_spec, PairStrategySpec)
        else PairStrategySpec.model_validate(strategy_spec or {})
    )
    if dataset_a.timeframe != dataset_b.timeframe:
        raise ValueError("pair research requires the same timeframe")
    if dataset_a.symbol == dataset_b.symbol:
        raise ValueError("pair research requires two different symbols")
    if initial_equity <= 0:
        raise ValueError("pair research initial equity must be positive")
    if fee_bps < 0 or slippage_bps < 0:
        raise ValueError("pair research costs cannot be negative")
    aligned = _aligned_candles(candles_a, candles_b)
    if len(aligned) < spec.formation_window + 2:
        raise ValueError("pair research has insufficient synchronized closed bars")
    evaluation_index = (
        spec.formation_window
        if evaluation_start_index is None
        else evaluation_start_index
    )
    if evaluation_index < spec.formation_window:
        raise ValueError(
            "pair evaluation must start after the complete formation window"
        )
    if evaluation_index >= len(aligned) - 1:
        raise ValueError("pair research has insufficient out-of-sample trading bars")
    residual_stability = _residual_stability_evidence(aligned, spec, evaluation_index)
    signals = _causal_pair_signals(aligned, spec)
    funding_ledger_a = _FundingLedger(funding_a)
    funding_ledger_b = _FundingLedger(funding_b)
    fee_rate = fee_bps / Decimal("10000")
    slip_rate = slippage_bps / Decimal("10000")
    equity = initial_equity
    peak = initial_equity
    maximum_drawdown = Decimal("0")
    trades: list[PairTrade] = []
    position: dict | None = None
    pending_entry: tuple[int, int, Decimal, Decimal] | None = None
    pending_exit: tuple[str, Decimal] | None = None
    terminal_closed = False
    margin_breach = False
    marked_equity_returns: list[float] = []
    previous_marked_equity = initial_equity

    def close_position(
        index: int,
        reason: str,
        exit_zscore: Decimal,
    ) -> None:
        nonlocal equity, position
        assert position is not None
        candle_a, candle_b = aligned[index]
        direction_a = position["direction_a"]
        direction_b = position["direction_b"]
        exit_a = candle_a.open * (
            Decimal("1") - slip_rate if direction_a > 0 else Decimal("1") + slip_rate
        )
        exit_b = candle_b.open * (
            Decimal("1") - slip_rate if direction_b > 0 else Decimal("1") + slip_rate
        )
        gross_a = (exit_a - position["entry_a"]) * position["quantity_a"] * Decimal(direction_a)
        gross_b = (exit_b - position["entry_b"]) * position["quantity_b"] * Decimal(direction_b)
        fees = (
            position["entry_a"] * position["quantity_a"]
            + position["entry_b"] * position["quantity_b"]
            + exit_a * position["quantity_a"]
            + exit_b * position["quantity_b"]
        ) * fee_rate
        slippage_cost = (
            abs(position["entry_a"] - position["raw_entry_a"]) + abs(exit_a - candle_a.open)
        ) * position["quantity_a"] + (
            abs(position["entry_b"] - position["raw_entry_b"]) + abs(exit_b - candle_b.open)
        ) * position["quantity_b"]
        funding_cost = _funding_cost(
            funding_ledger_a,
            entry_time=position["entry_time"],
            exit_time=candle_a.timestamp,
            side=direction_a,
            notional=position["leg_notional_a"],
        ) + _funding_cost(
            funding_ledger_b,
            entry_time=position["entry_time"],
            exit_time=candle_b.timestamp,
            side=direction_b,
            notional=position["leg_notional_b"],
        )
        gross = gross_a + gross_b
        net = gross - fees - funding_cost
        equity_before = equity
        equity += net
        trades.append(
            PairTrade(
                entry_time=position["entry_time"],
                exit_time=candle_a.timestamp,
                long_symbol=(dataset_a.symbol if direction_a > 0 else dataset_b.symbol),
                short_symbol=(dataset_b.symbol if direction_a > 0 else dataset_a.symbol),
                entry_zscore=position["entry_zscore"],
                exit_zscore=exit_zscore,
                entry_hedge_ratio=position["hedge_ratio"],
                gross_pnl=gross.quantize(Decimal("0.01")),
                fees=fees.quantize(Decimal("0.01")),
                slippage_cost=slippage_cost.quantize(Decimal("0.01")),
                funding_cost=funding_cost.quantize(Decimal("0.01")),
                net_pnl=net.quantize(Decimal("0.01")),
                return_percent=(
                    net / equity_before * Decimal("100") if equity_before > 0 else Decimal("-100")
                ).quantize(Decimal("0.0001")),
                holding_bars=index - position["entry_index"],
                exit_reason=reason,
            )
        )
        position = None

    def marked_equity_at(index: int) -> Decimal:
        if position is None:
            return equity
        candle_a, candle_b = aligned[index]
        direction_a = position["direction_a"]
        direction_b = position["direction_b"]
        unrealized_a = (
            (candle_a.close - position["entry_a"]) * position["quantity_a"] * Decimal(direction_a)
        )
        unrealized_b = (
            (candle_b.close - position["entry_b"]) * position["quantity_b"] * Decimal(direction_b)
        )
        entry_fees = (
            position["entry_a"] * position["quantity_a"]
            + position["entry_b"] * position["quantity_b"]
        ) * fee_rate
        accrued_funding = _funding_cost(
            funding_ledger_a,
            entry_time=position["entry_time"],
            exit_time=candle_a.timestamp,
            side=direction_a,
            notional=position["leg_notional_a"],
        ) + _funding_cost(
            funding_ledger_b,
            entry_time=position["entry_time"],
            exit_time=candle_b.timestamp,
            side=direction_b,
            notional=position["leg_notional_b"],
        )
        return equity + unrealized_a + unrealized_b - entry_fees - accrued_funding

    for index in range(evaluation_index, len(aligned)):
        candle_a, candle_b = aligned[index]
        current_signal = signals[index]
        current_zscore = current_signal[0] if current_signal is not None else None
        if pending_exit is not None and position is not None:
            close_position(index, pending_exit[0], pending_exit[1])
            pending_exit = None
        if (
            pending_entry is not None
            and position is None
            and index < len(aligned) - 1
            and equity > 0
        ):
            direction_a, direction_b, signal_zscore, hedge_ratio = pending_entry
            gross_notional = equity * spec.gross_leverage
            absolute_hedge_ratio = abs(hedge_ratio)
            leg_notional_a = gross_notional / (Decimal("1") + absolute_hedge_ratio)
            leg_notional_b = gross_notional - leg_notional_a
            raw_entry_a = candle_a.open
            raw_entry_b = candle_b.open
            entry_a = raw_entry_a * (
                Decimal("1") + slip_rate if direction_a > 0 else Decimal("1") - slip_rate
            )
            entry_b = raw_entry_b * (
                Decimal("1") + slip_rate if direction_b > 0 else Decimal("1") - slip_rate
            )
            position = {
                "direction_a": direction_a,
                "direction_b": direction_b,
                "entry_a": entry_a,
                "entry_b": entry_b,
                "raw_entry_a": raw_entry_a,
                "raw_entry_b": raw_entry_b,
                "quantity_a": leg_notional_a / raw_entry_a,
                "quantity_b": leg_notional_b / raw_entry_b,
                "leg_notional_a": leg_notional_a,
                "leg_notional_b": leg_notional_b,
                "hedge_ratio": hedge_ratio,
                "entry_time": candle_a.timestamp,
                "entry_index": index,
                "entry_zscore": signal_zscore,
            }
            pending_entry = None
        elif pending_entry is not None and index == len(aligned) - 1:
            pending_entry = None
        if position is None and current_signal is not None:
            hedge_ratio = current_signal[1]
            if current_zscore >= spec.entry_zscore:
                if spec.model == "relative_momentum_equal_dollar":
                    pending_entry = (1, -1, current_zscore, hedge_ratio)
                else:
                    pending_entry = (
                        -1,
                        1 if hedge_ratio >= 0 else -1,
                        current_zscore,
                        hedge_ratio,
                    )
            elif current_zscore <= -spec.entry_zscore:
                if spec.model == "relative_momentum_equal_dollar":
                    pending_entry = (-1, 1, current_zscore, hedge_ratio)
                else:
                    pending_entry = (
                        1,
                        -1 if hedge_ratio >= 0 else 1,
                        current_zscore,
                        hedge_ratio,
                    )
        elif position is not None:
            holding_bars = index - position["entry_index"]
            if spec.model == "relative_momentum_equal_dollar":
                directional_score = (
                    current_zscore * Decimal(position["direction_a"])
                    if current_zscore is not None
                    else None
                )
                if directional_score is not None and directional_score <= -spec.stop_zscore:
                    pending_exit = ("relative_momentum_stop", current_zscore)
                elif directional_score is not None and directional_score <= spec.exit_zscore:
                    pending_exit = ("relative_momentum_decay", current_zscore)
            elif current_zscore is not None and abs(current_zscore) <= spec.exit_zscore:
                pending_exit = ("mean_reversion", current_zscore)
            elif current_zscore is not None and abs(current_zscore) >= spec.stop_zscore:
                pending_exit = ("spread_stop", current_zscore)
            if pending_exit is None and holding_bars >= spec.maximum_holding_bars:
                pending_exit = (
                    "maximum_holding",
                    current_zscore if current_zscore is not None else Decimal("0"),
                )
        marked_equity = marked_equity_at(index)
        if marked_equity <= 0 and position is not None:
            margin_breach = True
            pending_exit = (
                "margin_breach",
                current_zscore if current_zscore is not None else Decimal("0"),
            )
        if previous_marked_equity > 0:
            marked_equity_returns.append(
                float(marked_equity / previous_marked_equity - Decimal("1"))
            )
        previous_marked_equity = marked_equity
        peak = max(peak, marked_equity)
        if peak > 0:
            maximum_drawdown = max(
                maximum_drawdown,
                (peak - marked_equity) / peak * Decimal("100"),
            )

    if position is not None:
        last_index = len(aligned) - 1
        # Use the final close as an explicit terminal liquidation, including
        # adverse slippage by converting it to a synthetic next-open candle.
        last_a, last_b = aligned[last_index]
        aligned[last_index] = (
            last_a.model_copy(update={"open": last_a.close}),
            last_b.model_copy(update={"open": last_b.close}),
        )
        terminal_signal = signals[last_index]
        close_position(
            last_index,
            "end_of_data",
            terminal_signal[0] if terminal_signal is not None else Decimal("0"),
        )
        terminal_closed = True
        peak = max(peak, equity)
        if peak > 0:
            maximum_drawdown = max(
                maximum_drawdown,
                (peak - equity) / peak * Decimal("100"),
            )

    normalized_pnls = [trade.return_percent / Decimal("100") for trade in trades]
    wins = [value for value in normalized_pnls if value > 0]
    losses = [-value for value in normalized_pnls if value < 0]
    win_rate = (
        Decimal(len(wins)) / Decimal(len(normalized_pnls)) * Decimal("100")
        if normalized_pnls
        else Decimal("0")
    )
    payoff = (
        (sum(wins, Decimal("0")) / len(wins)) / (sum(losses, Decimal("0")) / len(losses))
        if wins and losses
        else Decimal("0")
    )
    profit_factor = sum(wins, Decimal("0")) / sum(losses, Decimal("0")) if losses else Decimal("0")
    sharpe = (
        Decimal(
            str(
                mean(marked_equity_returns)
                / pstdev(marked_equity_returns)
                * math.sqrt(float(periods_per_year(dataset_a.timeframe)))
            )
        )
        if len(marked_equity_returns) > 1 and pstdev(marked_equity_returns)
        else Decimal("0")
    )
    statistical = validate_statistical_evidence(
        annualized_sharpe=sharpe,
        annualization_periods=periods_per_year(dataset_a.timeframe),
        observation_count=len(marked_equity_returns),
        pnls=normalized_pnls,
        initial_equity=Decimal("1"),
        trial_count=research_trial_count,
    )
    reasons: list[str] = []
    if len(trades) < 30:
        reasons.append(f"配对交易仅 {len(trades)} 笔，少于 30 笔")
    if win_rate < Decimal("45"):
        reasons.append(f"配对胜率 {win_rate:.2f}% 低于 45%")
    if payoff < Decimal("1.20"):
        reasons.append(f"配对盈亏比 {payoff:.2f} 低于 1.20")
    if profit_factor < Decimal("1.15"):
        reasons.append(f"配对 Profit Factor {profit_factor:.2f} 低于 1.15")
    if equity <= initial_equity:
        reasons.append("配对策略扣除双腿成本后收益不为正")
    funding_coverage_a = _funding_coverage_percent(
        funding_a,
        data_start=aligned[evaluation_index][0].timestamp,
        data_end=aligned[-1][0].timestamp,
    )
    funding_coverage_b = _funding_coverage_percent(
        funding_b,
        data_start=aligned[evaluation_index][1].timestamp,
        data_end=aligned[-1][1].timestamp,
    )
    if dataset_a.market_type == "swap" and funding_coverage_a < Decimal("95"):
        reasons.append(f"{dataset_a.symbol} 资金费率时间覆盖仅 {funding_coverage_a:.2f}%，低于 95%")
    if dataset_b.market_type == "swap" and funding_coverage_b < Decimal("95"):
        reasons.append(f"{dataset_b.symbol} 资金费率时间覆盖仅 {funding_coverage_b:.2f}%，低于 95%")
    if margin_breach:
        reasons.append("逐K线盯市权益曾低于或等于零")
    if residual_stability.applicable and not residual_stability.passed:
        reasons.extend(residual_stability.reasons)
    if not statistical.uncertainty_passed:
        reasons.extend(statistical.reasons)
    research_gate_passed = not reasons
    robustness_reasons = ["单一配对单一周期回测不能证明跨窗口、跨配对及成本压力稳健性"]
    if terminal_closed:
        robustness_reasons.append("样本末端存在未自然退出的配对仓位")
    validation_reasons = list(dict.fromkeys([*reasons, *robustness_reasons]))
    return PairBacktestReport(
        engine_version=PAIR_BACKTEST_ENGINE_VERSION,
        strategy_spec=spec,
        strategy_fingerprint=spec.fingerprint,
        dataset_a_id=dataset_a.id,
        dataset_b_id=dataset_b.id,
        data_fingerprint_a=dataset_a.data_fingerprint,
        data_fingerprint_b=dataset_b.data_fingerprint,
        symbol_a=dataset_a.symbol,
        symbol_b=dataset_b.symbol,
        timeframe=dataset_a.timeframe,
        aligned_bars=len(aligned),
        formation_bars=evaluation_index,
        evaluation_bars=len(aligned) - evaluation_index,
        evaluation_start=aligned[evaluation_index][0].timestamp,
        data_start=aligned[evaluation_index][0].timestamp,
        data_end=aligned[-1][0].timestamp,
        initial_equity=initial_equity,
        final_equity=equity.quantize(Decimal("0.01")),
        return_percent=((equity / initial_equity - Decimal("1")) * Decimal("100")).quantize(
            Decimal("0.01")
        ),
        max_drawdown_percent=maximum_drawdown.quantize(Decimal("0.01")),
        total_trades=len(trades),
        win_rate_percent=win_rate.quantize(Decimal("0.01")),
        payoff_ratio=payoff.quantize(Decimal("0.01")),
        profit_factor=profit_factor.quantize(Decimal("0.01")),
        total_fees=sum((trade.fees for trade in trades), Decimal("0")),
        total_slippage=sum(
            (trade.slippage_cost for trade in trades),
            Decimal("0"),
        ),
        total_funding=sum(
            (trade.funding_cost for trade in trades),
            Decimal("0"),
        ),
        funding_coverage_percent_a=funding_coverage_a.quantize(Decimal("0.01")),
        funding_coverage_percent_b=funding_coverage_b.quantize(Decimal("0.01")),
        terminal_position_closed=terminal_closed,
        statistical_validation=statistical,
        residual_stability=residual_stability,
        research_gate_passed=research_gate_passed,
        robustness_passed=False,
        execution_eligible=False,
        validation_reasons=validation_reasons,
        integrity_checks={
            "closed_bars_only": True,
            "signal_to_fill_latency_bars": 1,
            "synchronized_timestamps_only": True,
            "equal_dollar_entry_legs": True,
            "entry_hedge_ratio_frozen": True,
            "gross_notional_normalized": True,
            "causal_rolling_hedge_ratio": spec.model == "rolling_beta_residual",
            "causal_relative_momentum": spec.model == "relative_momentum_equal_dollar",
            "formation_stationarity_uses_only_pre_evaluation_data": True,
            "fixed_formation_residual_parameters_in_evaluation": True,
            "residual_stationarity_applicable": residual_stability.applicable,
            "residual_stationarity_gate_passed": residual_stability.passed,
            "strict_formation_trading_separation": True,
            "formation_bars_excluded_from_metrics": evaluation_index,
            "evaluation_bars": len(aligned) - evaluation_index,
            "double_leg_fees_and_slippage": True,
            "historical_funding_included": bool(funding_a or funding_b),
            "funding_prefix_ledger": True,
            "complete_funding_coverage": bool(
                (dataset_a.market_type != "swap" or funding_coverage_a >= Decimal("95"))
                and (dataset_b.market_type != "swap" or funding_coverage_b >= Decimal("95"))
            ),
            "mark_to_market_drawdown": True,
            "bar_return_sharpe": True,
            "normalized_trade_statistics": True,
            "margin_breach_detected": margin_breach,
            "cross_sample_robustness_evaluated": False,
            "execution_enabled": False,
        },
        trades=trades,
    )
