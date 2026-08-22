from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from .configuration import StrategyKind
from .market_evidence import MarketEvidenceSnapshot
from .signals import MarketStateAssessment
from .strategy_spec import (
    BreakoutStrategySpec,
    MeanReversionStrategySpec,
    StrategySpec,
    TrendStrategySpec,
    default_strategy_spec,
)


class AdaptiveStrategyCandidate(BaseModel):
    """A bounded parameter hypothesis selected only from current market evidence."""

    label: str
    selection_basis: str = "current_market_only"
    backtest_tuned: bool = False
    rationale: list[str] = Field(default_factory=list)
    spec: dict
    spec_fingerprint: str


def _decimal(value: Decimal, lower: str, upper: str) -> Decimal:
    return max(Decimal(lower), min(value, Decimal(upper)))


def _candidate(
    label: str,
    spec: StrategySpec,
    rationale: list[str],
) -> AdaptiveStrategyCandidate:
    return AdaptiveStrategyCandidate(
        label=label,
        rationale=[
            *rationale,
            "参数只由当前已收盘行情和市场证据选择，未读取回测结果",
        ],
        spec=spec.model_dump(mode="json", exclude={"fingerprint"}),
        spec_fingerprint=spec.fingerprint,
    )


def _volatility_adjustment(evidence: MarketEvidenceSnapshot) -> Decimal:
    if evidence.volatility_regime == "high":
        return Decimal("0.25")
    if evidence.volatility_regime == "low":
        return Decimal("-0.15")
    return Decimal("0")


def _trend_candidates(
    assessment: MarketStateAssessment,
    evidence: MarketEvidenceSnapshot,
) -> list[AdaptiveStrategyCandidate]:
    stop_adjustment = _volatility_adjustment(evidence)
    baseline = default_strategy_spec(StrategyKind.TREND)
    assert isinstance(baseline, TrendStrategySpec)
    responsive = TrendStrategySpec(
        source="system",
        hypothesis="当前市场趋势结构需要更快确认，但仍要求斜率与高周期方向一致。",
        parameters={
            "ema_fast": 12,
            "ema_slow": 36,
            "signal_gap_atr": "0.10",
            "signal_slope_atr": "0.035",
            "efficiency_window": 20,
            "minimum_efficiency_ratio": "0.15",
            "pullback_mode": "wick_reclaim",
            "pullback_tolerance_atr": "0.20",
            "maximum_entry_extension_atr": "0.65",
            "entry_zone_atr": "0.10",
            "stop_atr": _decimal(
                Decimal("1.45") + stop_adjustment,
                "0.8",
                "2.4",
            ),
            "reward_buffer": "0.02",
            "second_target_extension": "1.5",
            "trailing_stop_atr": _decimal(
                Decimal("1.05") + stop_adjustment / Decimal("2"),
                "0.7",
                "2",
            ),
            "trailing_activation": "one_r",
            "confirmation_ema_fast": 12,
            "confirmation_ema_slow": 36,
        },
    )
    selective = TrendStrategySpec(
        source="system",
        hypothesis="仅接受更持久的趋势结构，并使用更宽的波动缓冲降低噪声止损。",
        parameters={
            "ema_fast": 20,
            "ema_slow": 60,
            "signal_gap_atr": "0.22",
            "signal_slope_atr": "0.08",
            "efficiency_window": 20,
            "minimum_efficiency_ratio": "0.25",
            "pullback_mode": "close_reclaim",
            "pullback_tolerance_atr": "0.10",
            "maximum_entry_extension_atr": "0.55",
            "entry_zone_atr": "0.16",
            "stop_atr": _decimal(
                Decimal("1.85") + stop_adjustment,
                "1.1",
                "2.8",
            ),
            "reward_buffer": "0.03",
            "second_target_extension": "1.8",
            "trailing_stop_atr": _decimal(
                Decimal("1.4") + stop_adjustment / Decimal("2"),
                "0.8",
                "2.3",
            ),
            "trailing_activation": "first_target",
            "confirmation_ema_fast": 20,
            "confirmation_ema_slow": 60,
        },
    )
    cost_resilient = TrendStrategySpec(
        source="system",
        hypothesis=(
            "只参与持续时间更长、预期波幅明显覆盖手续费与滑点的趋势，以较低换手率提高净收益韧性。"
        ),
        parameters={
            "ema_fast": 32,
            "ema_slow": 96,
            "signal_gap_atr": "0.30",
            "signal_slope_atr": "0.11",
            "efficiency_window": 20,
            "minimum_efficiency_ratio": "0.35",
            "pullback_mode": "close_reclaim",
            "pullback_tolerance_atr": "0.10",
            "maximum_entry_extension_atr": "0.45",
            "entry_zone_atr": "0.12",
            "stop_atr": _decimal(
                Decimal("2.10") + stop_adjustment,
                "1.3",
                "3.0",
            ),
            "reward_buffer": "0.08",
            "second_target_extension": "2.3",
            "trailing_stop_atr": _decimal(
                Decimal("1.70") + stop_adjustment / Decimal("2"),
                "1.0",
                "2.6",
            ),
            "trailing_activation": "one_r",
            "confirmation_ema_fast": 32,
            "confirmation_ema_slow": 96,
        },
    )
    context = [
        f"趋势结构评分 {assessment.scores.get('trend', Decimal('0')):.3f}",
        f"波动率状态 {evidence.volatility_regime}，20/100 比值 {evidence.volatility_ratio:.2f}",
        f"衍生品参与状态 {evidence.derivatives_regime}，"
        f"方向确认 {evidence.derivatives_confirmation}",
    ]
    return [
        _candidate("baseline", baseline, [*context, "保留标准趋势规格作为对照组"]),
        _candidate(
            "responsive",
            responsive,
            [*context, "使用更快 EMA，并要求最低方向效率过滤无序路径"],
        ),
        _candidate(
            "selective",
            selective,
            [*context, "提高趋势强度与方向效率门槛以过滤横盘噪声"],
        ),
        _candidate(
            "cost_resilient",
            cost_resilient,
            [
                *context,
                "延长趋势确认并提高目标扩展，减少换手并要求潜在波幅覆盖执行摩擦",
            ],
        ),
    ]


def _mean_reversion_candidates(
    assessment: MarketStateAssessment,
    evidence: MarketEvidenceSnapshot,
) -> list[AdaptiveStrategyCandidate]:
    stop_adjustment = _volatility_adjustment(evidence)
    baseline = default_strategy_spec(StrategyKind.MEAN_REVERSION)
    assert isinstance(baseline, MeanReversionStrategySpec)
    responsive = MeanReversionStrategySpec(
        source="system",
        hypothesis="在低到正常波动的区间结构中更快识别偏离与回归，但仍要求 RSI 共振。",
        parameters={
            "mean_window": 16,
            "rsi_period": 10,
            "zscore_entry": "0.85",
            "rsi_oversold": "42",
            "rsi_overbought": "58",
            "entry_zone_atr": "0.08",
            "stop_atr": _decimal(
                Decimal("1.25") + stop_adjustment,
                "0.8",
                "2.2",
            ),
            "reward_buffer": "0.02",
            "second_target_extension": "1.4",
            "confirmation_ema_fast": 12,
            "confirmation_ema_slow": 36,
        },
    )
    selective = MeanReversionStrategySpec(
        source="system",
        hypothesis="只交易更极端的统计偏离，换取更少但更清晰的均值回归样本。",
        parameters={
            "mean_window": 30,
            "rsi_period": 14,
            "zscore_entry": ("1.65" if evidence.volatility_regime == "high" else "1.45"),
            "rsi_oversold": "35",
            "rsi_overbought": "65",
            "entry_zone_atr": "0.12",
            "stop_atr": _decimal(
                Decimal("1.7") + stop_adjustment,
                "1.0",
                "2.6",
            ),
            "reward_buffer": "0.03",
            "second_target_extension": "1.7",
            "confirmation_ema_fast": 20,
            "confirmation_ema_slow": 50,
        },
    )
    cost_resilient = MeanReversionStrategySpec(
        source="system",
        hypothesis=(
            "只交易更深的统计偏离，并要求更大的回归空间覆盖执行成本，避免在均值附近频繁往返。"
        ),
        parameters={
            "mean_window": 50,
            "rsi_period": 14,
            "zscore_entry": ("2.20" if evidence.volatility_regime == "high" else "1.90"),
            "rsi_oversold": "32",
            "rsi_overbought": "68",
            "entry_zone_atr": "0.10",
            "stop_atr": _decimal(
                Decimal("1.90") + stop_adjustment,
                "1.2",
                "2.9",
            ),
            "reward_buffer": "0.08",
            "second_target_extension": "2.2",
            "confirmation_ema_fast": 32,
            "confirmation_ema_slow": 96,
        },
    )
    context = [
        f"均值回归结构评分 {assessment.scores.get('mean_reversion', Decimal('0')):.3f}",
        f"区间位置 {evidence.range_position_50 * 100:.1f}%，"
        f"波动率状态 {evidence.volatility_regime}",
        f"衍生品参与状态 {evidence.derivatives_regime}，"
        f"方向确认 {evidence.derivatives_confirmation}",
    ]
    return [
        _candidate("baseline", baseline, [*context, "保留标准均值回归规格作为对照组"]),
        _candidate("responsive", responsive, [*context, "缩短均值和 RSI 窗口以响应近期区间"]),
        _candidate("selective", selective, [*context, "扩大偏离门槛以避免轻微波动触发"]),
        _candidate(
            "cost_resilient",
            cost_resilient,
            [
                *context,
                "提高统计偏离和目标空间，降低均值附近的高成本反复交易",
            ],
        ),
    ]


def _breakout_candidates(
    assessment: MarketStateAssessment,
    evidence: MarketEvidenceSnapshot,
) -> list[AdaptiveStrategyCandidate]:
    stop_adjustment = _volatility_adjustment(evidence)
    baseline = default_strategy_spec(StrategyKind.BREAKOUT)
    assert isinstance(baseline, BreakoutStrategySpec)
    responsive = BreakoutStrategySpec(
        source="system",
        hypothesis="在成交量扩张时使用较短区间捕捉新突破，并以原生保护限制失败突破。",
        parameters={
            "lookback": 14,
            "minimum_volume_ratio": "1.08",
            "entry_zone_atr": "0.12",
            "stop_atr": _decimal(
                Decimal("1.35") + stop_adjustment,
                "0.9",
                "2.3",
            ),
            "reward_buffer": "0.02",
            "second_target_extension": "1.5",
            "trailing_stop_atr": _decimal(
                Decimal("1.1") + stop_adjustment / Decimal("2"),
                "0.7",
                "2",
            ),
            "trailing_activation": "one_r",
            "confirmation_ema_fast": 12,
            "confirmation_ema_slow": 36,
        },
    )
    selective = BreakoutStrategySpec(
        source="system",
        hypothesis="要求更长价格区间和更强成交量确认，过滤短周期假突破。",
        parameters={
            "lookback": 40,
            "minimum_volume_ratio": (
                "1.35" if evidence.volume_ratio_20 >= Decimal("1.5") else "1.20"
            ),
            "entry_zone_atr": "0.18",
            "stop_atr": _decimal(
                Decimal("1.8") + stop_adjustment,
                "1.1",
                "2.8",
            ),
            "reward_buffer": "0.03",
            "second_target_extension": "1.9",
            "trailing_stop_atr": _decimal(
                Decimal("1.5") + stop_adjustment / Decimal("2"),
                "0.9",
                "2.4",
            ),
            "trailing_activation": "first_target",
            "confirmation_ema_fast": 20,
            "confirmation_ema_slow": 60,
        },
    )
    cost_resilient = BreakoutStrategySpec(
        source="system",
        hypothesis=(
            "仅交易更长区间、强成交量确认的突破，并保留更大的趋势尾部，"
            "使预期波幅能够覆盖交易成本与假突破损耗。"
        ),
        parameters={
            "lookback": 64,
            "minimum_volume_ratio": ("1.55" if evidence.volatility_regime == "high" else "1.40"),
            "entry_zone_atr": "0.15",
            "stop_atr": _decimal(
                Decimal("2.00") + stop_adjustment,
                "1.3",
                "3.0",
            ),
            "reward_buffer": "0.08",
            "second_target_extension": "2.3",
            "trailing_stop_atr": _decimal(
                Decimal("1.70") + stop_adjustment / Decimal("2"),
                "1.0",
                "2.6",
            ),
            "trailing_activation": "one_r",
            "confirmation_ema_fast": 32,
            "confirmation_ema_slow": 96,
        },
    )
    context = [
        f"突破结构评分 {assessment.scores.get('breakout', Decimal('0')):.3f}",
        f"最新成交量为 20 根均量的 {evidence.volume_ratio_20:.2f}x，"
        f"波动率状态 {evidence.volatility_regime}",
        f"衍生品参与状态 {evidence.derivatives_regime}，"
        f"方向确认 {evidence.derivatives_confirmation}",
    ]
    return [
        _candidate("baseline", baseline, [*context, "保留标准突破规格作为对照组"]),
        _candidate("responsive", responsive, [*context, "缩短突破窗口以响应成交量扩张"]),
        _candidate("selective", selective, [*context, "延长区间并提高成交量确认要求"]),
        _candidate(
            "cost_resilient",
            cost_resilient,
            [
                *context,
                "延长突破区间与目标扩展，只保留足以覆盖执行摩擦的机会",
            ],
        ),
    ]


def build_adaptive_strategy_candidates(
    kind: StrategyKind,
    assessment: MarketStateAssessment,
    evidence: MarketEvidenceSnapshot,
) -> list[AdaptiveStrategyCandidate]:
    """Return a deterministic, bounded mini-ensemble without backtest tuning."""

    if kind == StrategyKind.TREND:
        return _trend_candidates(assessment, evidence)
    if kind == StrategyKind.MEAN_REVERSION:
        return _mean_reversion_candidates(assessment, evidence)
    return _breakout_candidates(assessment, evidence)
