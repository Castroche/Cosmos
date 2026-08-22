from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from statistics import median

from pydantic import BaseModel, Field

from .research_campaign import CampaignCandidateAggregate, ResearchCampaignCell
from .research_lifecycle import StrategyCandidate, create_candidate
from .strategy_spec import validate_strategy_spec


class FailureAttribution(BaseModel):
    codes: list[str] = Field(default_factory=list)
    evidence: dict[str, Decimal | int | bool | str] = Field(default_factory=dict)
    explanation: list[str] = Field(default_factory=list)


class HypothesisRevision(BaseModel):
    label: str
    changed_parameters: dict[str, Decimal | int | str]
    rationale: str
    candidate: StrategyCandidate


def attribute_candidate_failure(
    candidate: StrategyCandidate,
) -> FailureAttribution:
    result = candidate.backtest
    if result is None:
        return FailureAttribution(
            codes=["missing_backtest"],
            explanation=["候选尚未完成不可变数据集回测"],
        )
    codes: list[str] = []
    explanations: list[str] = []
    if result.total_trades < max(result.trade_sample_floor, 30):
        codes.append("insufficient_trade_evidence")
        explanations.append("独立交易样本不足，不能推断稳定胜率或盈亏比")
    if result.profit_factor < Decimal("1.25"):
        codes.append("low_profit_factor")
        explanations.append("扣除成本后 Profit Factor 未达到执行级门槛 1.25")
    if result.win_rate_percent < Decimal("50"):
        codes.append("low_win_rate")
        explanations.append("样本胜率未达到执行级门槛 50%")
    if result.payoff_ratio < Decimal("1.30"):
        codes.append("low_payoff")
        explanations.append("平均盈利相对平均亏损未达到执行级门槛 1.30")
    if result.stress_return_percent is None or result.stress_return_percent <= 0:
        codes.append("cost_fragile")
        explanations.append("手续费与滑点压力后收益不为正")
    if result.temporal_stability and not result.temporal_stability.passed:
        codes.append("temporal_instability")
        explanations.append("优势没有稳定分布在非重叠时间分段")
    if result.statistical_validation and not result.statistical_validation.uncertainty_passed:
        codes.append("statistical_uncertainty")
        explanations.append("置信区间或多重试验修正不足")
    return FailureAttribution(
        codes=list(dict.fromkeys(codes)),
        evidence={
            "total_trades": result.total_trades,
            "profit_factor": result.profit_factor,
            "win_rate_percent": result.win_rate_percent,
            "payoff_ratio": result.payoff_ratio,
            "stress_return_percent": (
                result.stress_return_percent
                if result.stress_return_percent is not None
                else "unavailable"
            ),
            "temporal_stability_passed": bool(
                result.temporal_stability and result.temporal_stability.passed
            ),
        },
        explanation=explanations or ["候选已通过现有失败归因检查"],
    )


def attribute_campaign_failure(
    aggregate: CampaignCandidateAggregate,
    cells: list[ResearchCampaignCell],
) -> FailureAttribution:
    """Attribute failure from the complete search scope, not a chosen market."""

    codes: list[str] = []
    explanations: list[str] = []
    median_trades = int(median([cell.total_trades for cell in cells])) if cells else 0
    if median_trades < 30:
        codes.append("insufficient_trade_evidence")
        explanations.append("跨市场独立交易样本中位数不足 30 笔")
    if aggregate.median_profit_factor < Decimal("1.25"):
        codes.append("low_profit_factor")
        explanations.append("跨市场 Profit Factor 中位数低于 1.25")
    if aggregate.median_win_rate_percent < Decimal("50"):
        codes.append("low_win_rate")
        explanations.append("跨市场胜率中位数低于 50%")
    if aggregate.median_payoff_ratio < Decimal("1.30"):
        codes.append("low_payoff")
        explanations.append("跨市场盈亏比中位数低于 1.30")
    if aggregate.high_quality_market_ratio < Decimal("0.50"):
        codes.append("joint_quality_insufficient")
        explanations.append("胜率、盈亏比和 Profit Factor 同时达标的市场不足 50%")
    if aggregate.positive_stress_ratio < Decimal("0.60"):
        codes.append("cost_fragile")
        explanations.append("成本压力后正收益市场比例不足 60%")
    if cells and not all(cell.temporal_stability_passed for cell in cells):
        codes.append("temporal_instability")
        explanations.append("至少一个市场的非重叠时间分段不稳定")
    if cells and not any(cell.statistical_uncertainty_passed for cell in cells):
        codes.append("statistical_uncertainty")
        explanations.append("所有市场均未通过搜索后统计不确定性门槛")
    return FailureAttribution(
        codes=list(dict.fromkeys(codes)),
        evidence={
            "market_count": aggregate.market_count,
            "robust_market_count": aggregate.robust_market_count,
            "median_trades": median_trades,
            "median_profit_factor": aggregate.median_profit_factor,
            "median_win_rate_percent": aggregate.median_win_rate_percent,
            "median_payoff_ratio": aggregate.median_payoff_ratio,
            "positive_stress_ratio": aggregate.positive_stress_ratio,
            "high_quality_market_ratio": aggregate.high_quality_market_ratio,
            "worst_drawdown_percent": aggregate.worst_drawdown_percent,
        },
        explanation=explanations or ["Campaign 聚合证据未发现预设失败类型"],
    )


def _bounded_decimal(
    value,
    multiplier: str,
    *,
    lower: str,
    upper: str,
    quantum: str = "0.01",
) -> str:
    adjusted = Decimal(str(value)) * Decimal(multiplier)
    return str(max(Decimal(lower), min(adjusted, Decimal(upper))).quantize(Decimal(quantum)))


def _revision_candidate(
    candidate: StrategyCandidate,
    *,
    label: str,
    changes: dict,
    rationale: str,
) -> HypothesisRevision:
    payload = deepcopy(candidate.spec)
    payload["source"] = "ai"
    payload["hypothesis"] = (f"{payload.get('hypothesis', '')}\n修订 {label}：{rationale}")[-2000:]
    payload["parameters"].update(changes)
    spec = validate_strategy_spec(payload)
    revised = create_candidate(
        spec,
        parent_candidate_id=candidate.id,
    )
    revised.decision_reasons.extend(
        [
            f"基于失败归因生成单变量可证伪修订：{label}",
            rationale,
            "修订未读取未来数据，必须在新隔离测试集和完整 Campaign 中重新验证",
        ]
    )
    return HypothesisRevision(
        label=label,
        changed_parameters=changes,
        rationale=rationale,
        candidate=revised,
    )


def generate_failure_driven_revisions(
    candidate: StrategyCandidate,
    *,
    max_revisions: int = 3,
) -> tuple[FailureAttribution, list[HypothesisRevision]]:
    """Create bounded, explainable revisions instead of parameter-grid mining."""

    attribution = attribute_candidate_failure(candidate)
    return attribution, _generate_revisions(
        candidate,
        attribution,
        max_revisions=max_revisions,
    )


def generate_campaign_failure_revisions(
    candidate: StrategyCandidate,
    aggregate: CampaignCandidateAggregate,
    cells: list[ResearchCampaignCell],
    *,
    max_revisions: int = 3,
) -> tuple[FailureAttribution, list[HypothesisRevision]]:
    attribution = attribute_campaign_failure(aggregate, cells)
    return attribution, _generate_revisions(
        candidate,
        attribution,
        max_revisions=max_revisions,
    )


def _generate_revisions(
    candidate: StrategyCandidate,
    attribution: FailureAttribution,
    *,
    max_revisions: int,
) -> list[HypothesisRevision]:
    parameters = candidate.spec["parameters"]
    family = candidate.spec["family"]
    entry_model = str(parameters.get("entry_model", ""))
    revisions: list[HypothesisRevision] = []
    payoff_evidence = Decimal(
        str(
            attribution.evidence.get(
                "median_payoff_ratio",
                attribution.evidence.get("payoff_ratio", "0"),
            )
        )
    )
    win_evidence = Decimal(
        str(
            attribution.evidence.get(
                "median_win_rate_percent",
                attribution.evidence.get("win_rate_percent", "0"),
            )
        )
    )
    profit_factor_evidence = Decimal(
        str(
            attribution.evidence.get(
                "median_profit_factor",
                attribution.evidence.get("profit_factor", "0"),
            )
        )
    )
    if (
        "insufficient_trade_evidence" in attribution.codes
        and win_evidence >= Decimal("50")
        and payoff_evidence >= Decimal("1.30")
        and profit_factor_evidence >= Decimal("1.25")
    ):
        changes: dict[str, Decimal | int | str] | None = None
        if family == "trend":
            changes = {
                "signal_gap_atr": _bounded_decimal(
                    parameters.get("signal_gap_atr", "0.15"),
                    "0.80",
                    lower="0",
                    upper="3",
                )
            }
        elif family == "breakout":
            changes = {
                "minimum_volume_ratio": _bounded_decimal(
                    parameters.get("minimum_volume_ratio", "1.1"),
                    "0.90",
                    lower="1",
                    upper="3",
                )
            }
        elif entry_model in {"bollinger_rsi", "bollinger_reentry"}:
            changes = {
                "band_deviations": _bounded_decimal(
                    parameters.get("band_deviations", "2"),
                    "0.90",
                    lower="0.5",
                    upper="4",
                )
            }
        elif entry_model == "zscore_reentry":
            changes = {
                "reentry_window": min(
                    max(int(parameters.get("reentry_window", 1)) + 2, 3),
                    10,
                )
            }
        elif entry_model == "zscore_rsi":
            changes = {
                "zscore_entry": _bounded_decimal(
                    parameters.get("zscore_entry", "1"),
                    "0.90",
                    lower="0.5",
                    upper="4",
                )
            }
        elif entry_model == "stochastic_reversal":
            oversold = Decimal(str(parameters.get("oscillator_oversold", "20")))
            overbought = Decimal(str(parameters.get("oscillator_overbought", "80")))
            changes = {
                "oscillator_oversold": str(min(Decimal("49"), oversold + Decimal("5"))),
                "oscillator_overbought": str(max(Decimal("51"), overbought - Decimal("5"))),
            }
        elif entry_model == "rsi_reversal":
            changes = {
                "rsi_oversold": _bounded_decimal(
                    parameters.get("rsi_oversold", "30"),
                    "1.10",
                    lower="5",
                    upper="49",
                ),
                "rsi_overbought": _bounded_decimal(
                    parameters.get("rsi_overbought", "70"),
                    "0.92",
                    lower="51",
                    upper="95",
                ),
            }
        if changes and any(
            str(parameters.get(key)) != str(value) for key, value in changes.items()
        ):
            revisions.append(
                _revision_candidate(
                    candidate,
                    label="increase_signal_frequency_with_quality_floor",
                    changes=changes,
                    rationale=(
                        "Cross-market win rate, payoff and profit factor meet their point "
                        "thresholds but the trade sample is too small. Relax exactly one "
                        "entry-selectivity dimension and retest the full frozen matrix."
                    ),
                )
            )
            if entry_model == "zscore_reentry":
                revisions.append(
                    _revision_candidate(
                        candidate,
                        label="放宽统计极端门槛以增加样本",
                        changes={
                            "zscore_entry": _bounded_decimal(
                                parameters.get("zscore_entry", "2"),
                                "0.90",
                                lower="0.75",
                                upper="4",
                            )
                        },
                        rationale=(
                            "点估计质量全部达标但样本不足；只小幅降低统计偏离"
                            "阈值，保留双重重返确认和原退出结构。"
                        ),
                    )
                )
                if parameters.get("trend_strength_mode") == "require_range":
                    revisions.append(
                        _revision_candidate(
                            candidate,
                            label="扩大可交易震荡状态",
                            changes={
                                "adx_threshold": _bounded_decimal(
                                    parameters.get("adx_threshold", "20"),
                                    "1.25",
                                    lower="5",
                                    upper="80",
                                )
                            },
                            rationale=(
                                "点估计质量全部达标但样本不足；只提高允许的 ADX"
                                "上限，检验较活跃但尚非强趋势的市场状态。"
                            ),
                        )
                    )
    if (
        "low_win_rate" in attribution.codes
        and payoff_evidence >= Decimal("1.30")
        and family == "mean_reversion"
    ):
        if parameters.get("maximum_holding_bars") is None:
            changes = {
                "maximum_holding_bars": min(
                    max(int(parameters.get("mean_window", 20)) * 2, 12),
                    2000,
                )
            }
            label = "限制均值回归持仓时限"
            rationale = (
                "均值回归盈亏比已经达标但胜率不足；只增加因果 K 线持仓上限，"
                "检验迟迟不回归的仓位是否应在信号半衰期后退出。"
            )
        elif parameters.get("mean_completion_mode", "minimum_reward") == "minimum_reward":
            changes = {"mean_completion_mode": "mean_touch"}
            label = "触及统计均值先减仓"
            rationale = (
                "持仓时限已经显式受控；只把第一目标切换为入场时可见的统计均值，"
                "第二目标自动扩展以维持加权收益风险下限。"
            )
        else:
            current_first_target = parameters.get("first_target_reward_risk")
            changes = {
                "first_target_reward_risk": _bounded_decimal(
                    current_first_target if current_first_target is not None else "1.25",
                    "0.80",
                    lower="0.75",
                    upper="2.50",
                )
            }
            label = "提前锁定部分盈利"
            rationale = (
                "均值回归盈亏比已经达到执行级门槛但胜率不足；仅降低首个部分"
                "止盈目标，第二目标自动扩展以维持加权收益风险下限。"
            )
        revisions.append(
            _revision_candidate(
                candidate,
                label=label,
                changes=changes,
                rationale=rationale,
            )
        )
        if entry_model == "zscore_rsi":
            revisions.append(
                _revision_candidate(
                    candidate,
                    label="等待统计偏离重返确认",
                    changes={"entry_model": "zscore_reentry"},
                    rationale=(
                        "统计偏离策略的盈亏比已达标但胜率不足；只把入场从"
                        "首次触及极值改为下一根已收盘 K 线同时重返 Z-Score 与"
                        "RSI 阈值，检验失败突破确认能否过滤延续性趋势。"
                    ),
                )
            )
    if {
        "low_win_rate",
        "low_profit_factor",
        "joint_quality_insufficient",
    } & set(attribution.codes):
        if family == "trend":
            changes = {
                "minimum_efficiency_ratio": _bounded_decimal(
                    parameters.get("minimum_efficiency_ratio", "0"),
                    "1.35",
                    lower="0.10",
                    upper="0.80",
                )
            }
        elif family == "mean_reversion":
            if entry_model == "stochastic_reversal":
                oversold = Decimal(str(parameters.get("oscillator_oversold", "20")))
                overbought = Decimal(str(parameters.get("oscillator_overbought", "80")))
                changes = {
                    "oscillator_oversold": str(max(Decimal("-250"), oversold - Decimal("5"))),
                    "oscillator_overbought": str(min(Decimal("250"), overbought + Decimal("5"))),
                }
            elif entry_model == "rsi_reversal":
                changes = {
                    "rsi_oversold": _bounded_decimal(
                        parameters.get("rsi_oversold", "30"),
                        "0.90",
                        lower="5",
                        upper="45",
                    ),
                    "rsi_overbought": _bounded_decimal(
                        parameters.get("rsi_overbought", "70"),
                        "1.08",
                        lower="55",
                        upper="95",
                    ),
                }
            elif entry_model == "cci_reversal":
                changes = {
                    "cci_threshold": _bounded_decimal(
                        parameters.get("cci_threshold", "100"),
                        "1.15",
                        lower="50",
                        upper="250",
                    )
                }
            elif entry_model == "williams_reversal":
                changes = {
                    "williams_oversold": "-85",
                    "williams_overbought": "-15",
                }
            elif entry_model == "vwap_reversion":
                changes = {
                    "vwap_deviation_atr": _bounded_decimal(
                        parameters.get("vwap_deviation_atr", "1"),
                        "1.15",
                        lower="0.25",
                        upper="5",
                    )
                }
            elif entry_model in {"bollinger_rsi", "bollinger_reentry"}:
                changes = {
                    "band_deviations": _bounded_decimal(
                        parameters.get("band_deviations", "2"),
                        "1.10",
                        lower="0.5",
                        upper="4",
                    )
                }
            elif entry_model == "funding_contrarian":
                changes = {
                    "funding_rate_threshold": _bounded_decimal(
                        parameters.get("funding_rate_threshold", "0.0001"),
                        "1.25",
                        lower="0.00001",
                        upper="0.01",
                        quantum="0.00001",
                    )
                }
            elif entry_model == "positioning_contrarian":
                changes = {
                    "positioning_ratio_threshold": _bounded_decimal(
                        parameters.get("positioning_ratio_threshold", "1.5"),
                        "1.20",
                        lower="1.05",
                        upper="10",
                    )
                }
            elif entry_model == "basis_contrarian":
                changes = {
                    "basis_rate_threshold": _bounded_decimal(
                        parameters.get("basis_rate_threshold", "0.0015"),
                        "1.25",
                        lower="0.0001",
                        upper="0.10",
                        quantum="0.0001",
                    )
                }
            else:
                changes = {
                    "zscore_entry": _bounded_decimal(
                        parameters.get("zscore_entry", "1"),
                        "1.15",
                        lower="0.75",
                        upper="3.5",
                    )
                }
        else:
            changes = {
                "minimum_volume_ratio": _bounded_decimal(
                    parameters.get("minimum_volume_ratio", "1.1"),
                    "1.10",
                    lower="1",
                    upper="3",
                )
            }
        revisions.append(
            _revision_candidate(
                candidate,
                label="提高联合信号质量",
                changes=changes,
                rationale=(
                    "胜率、盈亏比与 Profit Factor 的联合质量不足；单独提高信号"
                    "选择门槛，检验过滤噪声能否改善跨市场质量。"
                ),
            )
        )
    if "low_payoff" in attribution.codes:
        mean_touch_rebalance = bool(
            family == "mean_reversion"
            and parameters.get("mean_completion_mode") == "mean_touch"
            and win_evidence >= Decimal("50")
            and parameters.get("first_target_fraction") is not None
        )
        if mean_touch_rebalance:
            changes = {"mean_completion_mode": "minimum_reward"}
            label = "恢复最低收益风险目标"
            rationale = (
                "均值触及退出已达到胜率门槛但盈亏比不足，且独立 Campaign"
                "显示微调首目标仓位比例不能修复该缺陷；只把第一目标恢复为"
                "最低收益风险约束，重新检验入场优势能否支撑更远目标。"
            )
        elif parameters.get("first_target_reward_risk") is not None:
            changes = {
                "first_target_reward_risk": _bounded_decimal(
                    parameters.get("first_target_reward_risk", "1.25"),
                    "1.15",
                    lower="0.75",
                    upper="2.50",
                )
            }
            label = "提高主要收益风险目标"
            rationale = (
                "盈亏比不足且尾仓目标曾被首目标后的追踪退出遮蔽；只提高"
                "首个可执行收益风险目标，使修订直接改变止盈和追踪激活时点。"
            )
        else:
            changes = {
                "second_target_extension": _bounded_decimal(
                    parameters.get("second_target_extension", "1.6"),
                    "1.15",
                    lower="1",
                    upper="5",
                ),
            }
            label = "扩展尾仓收益目标"
            rationale = "盈亏比不足；只扩展尾仓目标，避免同时改变止损而无法归因。"
        revisions.append(
            _revision_candidate(
                candidate,
                label=label,
                changes=changes,
                rationale=rationale,
            )
        )
    if "cost_fragile" in attribution.codes or "temporal_instability" in attribution.codes:
        if family == "trend":
            changes = {
                "ema_slow": min(
                    int(parameters.get("ema_slow", 50) * 1.25),
                    200,
                )
            }
        elif family == "mean_reversion":
            if entry_model in {
                "stochastic_reversal",
                "cci_reversal",
                "williams_reversal",
            }:
                changes = {
                    "oscillator_period": min(
                        int(parameters.get("oscillator_period", 14) * 1.25),
                        50,
                    )
                }
            elif entry_model == "rsi_reversal":
                changes = {
                    "rsi_period": min(
                        int(parameters.get("rsi_period", 14) * 1.25),
                        50,
                    )
                }
            elif entry_model == "positioning_contrarian":
                changes = {
                    "positioning_ratio_threshold": _bounded_decimal(
                        parameters.get("positioning_ratio_threshold", "1.5"),
                        "1.15",
                        lower="1.05",
                        upper="10",
                    )
                }
            elif entry_model == "basis_contrarian":
                changes = {
                    "basis_rate_threshold": _bounded_decimal(
                        parameters.get("basis_rate_threshold", "0.0015"),
                        "1.15",
                        lower="0.0001",
                        upper="0.10",
                        quantum="0.0001",
                    )
                }
            else:
                changes = {
                    "mean_window": min(
                        int(parameters.get("mean_window", 20) * 1.25),
                        100,
                    )
                }
        else:
            changes = {
                "lookback": min(
                    int(parameters.get("lookback", 20) * 1.25),
                    200,
                )
            }
        revisions.append(
            _revision_candidate(
                candidate,
                label="降低换手并提高时间尺度",
                changes=changes,
                rationale="成本脆弱或时间不稳定，延长观察窗口检验更慢信号。",
            )
        )
    return revisions[: max(max_revisions, 0)]
