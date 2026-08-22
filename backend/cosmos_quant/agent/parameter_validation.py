from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from ..backtest import BacktestWindowSummary


class ParameterFoldEvidence(BaseModel):
    label: str
    spec_fingerprint: str
    windows: list[BacktestWindowSummary]


class RollingParameterStep(BaseModel):
    validation_fold: str
    selected_label: str
    selected_spec_fingerprint: str
    training_fold_count: int
    training_trades: int
    training_score: Decimal
    validation_trades: int
    validation_return_percent: Decimal
    validation_profit_factor: Decimal
    positive: bool


class RollingParameterReport(BaseModel):
    mode: str = "prior_folds_select_next_fold"
    lookahead_safe: bool = True
    selection_basis: str = "prior_folds_only"
    execution_gate: bool = False
    candidate_count: int
    step_count: int
    evaluable_steps: int
    positive_steps: int
    positive_step_ratio: Decimal
    cumulative_validation_return_percent: Decimal
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    steps: list[RollingParameterStep] = Field(default_factory=list)


def _training_score(
    windows: list[BacktestWindowSummary],
) -> tuple[Decimal, int]:
    trades = sum(window.total_trades for window in windows)
    cumulative_return = sum(
        (window.return_percent for window in windows),
        Decimal("0"),
    )
    worst_drawdown = max(
        (window.max_drawdown_percent for window in windows),
        default=Decimal("0"),
    )
    sample_credit = min(Decimal(trades), Decimal("30")) / Decimal("30")
    score = cumulative_return - worst_drawdown * Decimal("0.5")
    score += sample_credit
    return score.quantize(Decimal("0.0001")), trades


def build_rolling_parameter_report(
    candidates: list[ParameterFoldEvidence],
    *,
    minimum_training_trades: int = 3,
    minimum_validation_trades: int = 2,
) -> RollingParameterReport:
    """Validate preselected specs without using the validation fold for selection."""

    fold_count = min(
        (len(candidate.windows) for candidate in candidates),
        default=0,
    )
    fixed_spec = len(candidates) == 1
    steps: list[RollingParameterStep] = []
    for validation_index in range(1, fold_count):
        eligible: list[tuple[Decimal, int, ParameterFoldEvidence]] = []
        for candidate in candidates:
            score, trades = _training_score(candidate.windows[:validation_index])
            if trades >= minimum_training_trades:
                eligible.append((score, trades, candidate))
        if not eligible:
            continue
        training_score, training_trades, selected = max(
            eligible,
            key=lambda item: (
                item[0],
                item[1],
                item[2].spec_fingerprint,
            ),
        )
        validation = selected.windows[validation_index]
        evaluable = validation.total_trades >= minimum_validation_trades
        positive = bool(
            evaluable and validation.return_percent > 0 and validation.profit_factor >= Decimal("1")
        )
        steps.append(
            RollingParameterStep(
                validation_fold=validation.label,
                selected_label=selected.label,
                selected_spec_fingerprint=selected.spec_fingerprint,
                training_fold_count=validation_index,
                training_trades=training_trades,
                training_score=training_score,
                validation_trades=validation.total_trades,
                validation_return_percent=validation.return_percent,
                validation_profit_factor=validation.profit_factor,
                positive=positive,
            )
        )
    evaluable = [step for step in steps if step.validation_trades >= minimum_validation_trades]
    positive = [step for step in evaluable if step.positive]
    ratio = Decimal(len(positive)) / Decimal(len(evaluable)) if evaluable else Decimal("0")
    cumulative_return = sum(
        (step.validation_return_percent for step in evaluable),
        Decimal("0"),
    )
    reasons: list[str] = []
    if not candidates:
        reasons.append("没有可验证的预选参数规格。")
    if len(evaluable) < 2:
        reasons.append(
            f"仅 {len(evaluable)} 个下一时间分段达到 {minimum_validation_trades} 笔最低样本。"
        )
    required_positive_ratio = Decimal("1") if fixed_spec else Decimal("0.5")
    if evaluable and ratio < required_positive_ratio:
        reasons.append(
            f"{'固定规格' if fixed_spec else '前推选择后'}的正收益分段占比 "
            f"{ratio * 100:.1f}% 低于 {required_positive_ratio * 100:.0f}%。"
        )
    if evaluable and cumulative_return <= 0:
        reasons.append(f"前推选择的累计验证收益 {cumulative_return:.2f}% 不为正。")
    passed = not reasons
    return RollingParameterReport(
        mode=("fixed_spec_forward_validation" if fixed_spec else "prior_folds_select_next_fold"),
        selection_basis=("current_market_fixed_spec" if fixed_spec else "prior_folds_only"),
        candidate_count=len(candidates),
        step_count=max(fold_count - 1, 0),
        evaluable_steps=len(evaluable),
        positive_steps=len(positive),
        positive_step_ratio=ratio,
        cumulative_validation_return_percent=cumulative_return,
        passed=passed,
        reasons=reasons
        or [
            (
                "当前市场预先确定的唯一规格未被回测调参，并在全部可评估后续分段保持正向证据。"
                if fixed_spec
                else "参数只用过去分段选择，并在后续未见分段保持正向证据。"
            )
        ],
        steps=steps,
    )
