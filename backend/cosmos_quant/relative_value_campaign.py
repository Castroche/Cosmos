from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .domain import Candle, FundingRatePoint
from .multi_leg_execution import MultiLegExecutionCertificate
from .relative_value import (
    PAIR_STRATEGY_LIBRARY,
    PairBacktestReport,
    PairStrategySpec,
    run_pair_backtest,
)
from .research_campaign import (
    CampaignCandidateAggregate,
    ResearchCampaignCell,
    aggregate_campaign,
)
from .statistical_validation import BacktestOverfittingReport, binomial_upper_tail_p_value

PAIR_CAMPAIGN_ENGINE_VERSION = 6


@dataclass(frozen=True)
class PairMarketData:
    dataset_a: object
    candles_a: list[Candle]
    funding_a: list[FundingRatePoint]
    dataset_b: object
    candles_b: list[Candle]
    funding_b: list[FundingRatePoint]

    def validate(self) -> None:
        if self.dataset_a.exchange != self.dataset_b.exchange:
            raise ValueError("pair campaign legs must use one exchange per market cell")
        if self.dataset_a.timeframe != self.dataset_b.timeframe:
            raise ValueError("pair campaign legs must use the same timeframe")
        if self.dataset_a.symbol == self.dataset_b.symbol:
            raise ValueError("pair campaign legs must use different symbols")
        if not self.candles_a or not self.candles_b:
            raise ValueError("pair campaign market data cannot be empty")

    @property
    def pair_symbol(self) -> str:
        return f"{self.dataset_a.symbol}|{self.dataset_b.symbol}"

    @property
    def id(self) -> str:
        payload = {
            "dataset_a_id": self.dataset_a.id,
            "dataset_b_id": self.dataset_b.id,
            "data_fingerprint_a": self.dataset_a.data_fingerprint,
            "data_fingerprint_b": self.dataset_b.data_fingerprint,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"pairmarket_{sha256(encoded).hexdigest()[:20]}"


class PairCampaignCellEvidence(BaseModel):
    cell: ResearchCampaignCell
    base_report_id: str
    stress_report_id: str
    base_return_percent: Decimal
    stress_return_percent: Decimal
    active_temporal_folds: int
    positive_temporal_folds: int
    source_urls: list[str]


class PairFailureAttribution(BaseModel):
    codes: list[str]
    evidence: dict[str, Decimal | int | str]
    summary: str


class PairHypothesisRevision(BaseModel):
    label: str
    parent_strategy_fingerprint: str
    changed_parameters: dict[str, Decimal | int | str]
    rationale: str
    strategy_spec: PairStrategySpec


class PairResearchCampaignReport(BaseModel):
    id: str = Field(default_factory=lambda: f"paircampaign_{uuid4().hex[:16]}")
    engine_version: int = PAIR_CAMPAIGN_ENGINE_VERSION
    status: str = "completed"
    research_pair_ids: list[str]
    strategy_ids: list[str]
    strategy_fingerprints: list[str] = Field(default_factory=list)
    research_lineage_id: str = ""
    parent_campaign_id: str | None = None
    predecessor_campaign_id: str | None = None
    experiment_kind: Literal[
        "initial",
        "structural_extension",
        "bounded_revision",
    ] = "initial"
    research_experiment_id: str = ""
    research_configuration_fingerprint: str = ""
    cumulative_hypothesis_count: int = 1
    sequential_experiment_count: int = 1
    effective_trial_count: int = 1
    new_trial_variant_count: int = 1
    trial_count: int
    formation_fraction: Decimal = Decimal("0.25")
    strict_formation_trading_separation: bool = False
    cells: list[PairCampaignCellEvidence]
    candidates: list[CampaignCandidateAggregate]
    pbo: BacktestOverfittingReport
    provisional_selected_strategy_id: str | None = None
    failure_attribution: PairFailureAttribution | None = None
    recommended_revisions: list[PairHypothesisRevision] = Field(default_factory=list)
    blind_set_id: str | None = None
    blind_consumption_id: str | None = None
    independent_blind_test_completed: bool = False
    blind_selected_candidate_passed: bool = False
    blind_market_count: int = 0
    blind_evidence_fingerprint: str | None = None
    execution_eligible: bool = False
    execution_certificate: MultiLegExecutionCertificate | None = None
    profitability_claim: bool = False
    selection_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def pair_strategy_id(spec: PairStrategySpec) -> str:
    entry = str(spec.entry_zscore).replace(".", "p")
    return f"pair_{spec.model}_{spec.formation_window}_{entry}_{spec.fingerprint[:8]}"


def pair_research_lineage_id(markets: list[PairMarketData]) -> str:
    """Bind a lineage to original market sources, not mutable split artifacts."""

    identities = []
    for market in markets:
        identities.append(
            {
                "exchange": market.dataset_a.exchange,
                "timeframe": market.dataset_a.timeframe,
                "leg_a": market.dataset_a.source_dataset_id or market.dataset_a.id,
                "leg_b": market.dataset_b.source_dataset_id or market.dataset_b.id,
            }
        )
    encoded = json.dumps(
        sorted(identities, key=lambda item: json.dumps(item, sort_keys=True)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"pair_lineage_{sha256(encoded).hexdigest()[:20]}"


def generate_pair_failure_revisions(
    candidate: CampaignCandidateAggregate,
    parent_spec: PairStrategySpec,
    *,
    max_revisions: int = 3,
) -> tuple[PairFailureAttribution, list[PairHypothesisRevision]]:
    """Produce bounded one-variable revisions from cross-pair failure evidence."""

    codes: list[str] = []
    if candidate.median_win_rate_percent < Decimal("50"):
        codes.append("low_win_rate")
    if candidate.median_payoff_ratio < Decimal("1.30"):
        codes.append("low_payoff")
    if candidate.median_profit_factor < Decimal("1.25"):
        codes.append("low_profit_factor")
    if candidate.positive_stress_ratio < Decimal("0.60"):
        codes.append("cost_fragile")
    if candidate.temporal_stable_market_ratio < Decimal("0.50"):
        codes.append("temporal_instability")
    if candidate.worst_drawdown_percent > Decimal("20"):
        codes.append("excess_drawdown")
    attribution = PairFailureAttribution(
        codes=codes,
        evidence={
            "median_win_rate_percent": candidate.median_win_rate_percent,
            "median_payoff_ratio": candidate.median_payoff_ratio,
            "median_profit_factor": candidate.median_profit_factor,
            "positive_stress_ratio": candidate.positive_stress_ratio,
            "temporal_stable_market_ratio": candidate.temporal_stable_market_ratio,
            "worst_drawdown_percent": candidate.worst_drawdown_percent,
            "total_trades": candidate.total_trades,
        },
        summary=(
            "相对价值候选未通过跨配对联合门槛；只允许根据已记录失败类型生成"
            "单变量子假设，子假设不继承任何验证资格。"
        ),
    )
    revisions: list[PairHypothesisRevision] = []

    def add_revision(
        *,
        label: str,
        changes: dict[str, Decimal | int | str],
        rationale: str,
    ) -> None:
        payload = parent_spec.model_dump(mode="python")
        payload.update(changes)
        child = PairStrategySpec.model_validate(payload)
        revisions.append(
            PairHypothesisRevision(
                label=label,
                parent_strategy_fingerprint=parent_spec.fingerprint,
                changed_parameters=changes,
                rationale=rationale,
                strategy_spec=child,
            )
        )

    if "low_payoff" in codes:
        lower_exit = max(parent_spec.exit_zscore * Decimal("0.5"), Decimal("0"))
        add_revision(
            label="等待更完整的价差收敛",
            changes={"exit_zscore": lower_exit.quantize(Decimal("0.01"))},
            rationale=(
                "胜率已经接近或达到门槛但平均盈利过小；仅降低退出残差阈值，"
                "检验让盈利交易继续收敛能否提高盈亏比。"
            ),
        )
        tighter_stop = max(
            parent_spec.entry_zscore + Decimal("0.5"),
            parent_spec.stop_zscore * Decimal("0.80"),
        ).quantize(Decimal("0.01"))
        if tighter_stop < parent_spec.stop_zscore:
            add_revision(
                label="限制价差尾部止损",
                changes={"stop_zscore": tighter_stop},
                rationale=(
                    "高胜率仍对应低盈亏比，说明少数发散交易吞噬多数小盈利；"
                    "仅收紧价差止损，检验尾部损失是否可被因果截断。"
                ),
            )
    if {"cost_fragile", "temporal_instability", "excess_drawdown"} & set(codes):
        shorter_holding = max(
            int(parent_spec.maximum_holding_bars * Decimal("0.67")),
            2,
        )
        if shorter_holding != parent_spec.maximum_holding_bars:
            add_revision(
                label="缩短非收敛持仓时限",
                changes={"maximum_holding_bars": shorter_holding},
                rationale=(
                    "压力成本、时间稳定性或回撤失效；仅缩短持仓时限，"
                    "检验超过信号半衰期的仓位是否应更早退出。"
                ),
            )
    if not revisions and "low_win_rate" in codes:
        higher_entry = min(
            parent_spec.entry_zscore * Decimal("1.15"),
            Decimal("5"),
        ).quantize(Decimal("0.01"))
        if higher_entry < parent_spec.stop_zscore:
            add_revision(
                label="提高残差入场显著性",
                changes={"entry_zscore": higher_entry},
                rationale="胜率不足；仅提高入场偏离阈值，检验过滤噪声能否改善命中率。",
            )
    return attribution, revisions[: max(max_revisions, 0)]


def _fold_returns(
    report: PairBacktestReport,
    *,
    fold_count: int = 6,
) -> tuple[list[Decimal], int, int]:
    if fold_count < 2 or fold_count % 2:
        raise ValueError("pair campaign fold count must be an even integer of at least two")
    total_seconds = (report.data_end - report.data_start).total_seconds()
    growth = [Decimal("1") for _ in range(fold_count)]
    trade_counts = [0 for _ in range(fold_count)]
    for trade in report.trades:
        elapsed = max((trade.exit_time - report.data_start).total_seconds(), 0.0)
        fold = (
            min(int(elapsed / total_seconds * fold_count), fold_count - 1)
            if total_seconds > 0
            else fold_count - 1
        )
        growth[fold] *= Decimal("1") + trade.return_percent / Decimal("100")
        trade_counts[fold] += 1
    returns = [
        ((value - Decimal("1")) * Decimal("100")).quantize(Decimal("0.0001")) for value in growth
    ]
    active = sum(count > 0 for count in trade_counts)
    positive = sum(value > 0 for value, count in zip(returns, trade_counts, strict=True) if count)
    return returns, active, positive


def _backtest_fingerprint(report: PairBacktestReport) -> str:
    payload = report.model_dump(
        mode="json",
        exclude={"id", "created_at", "trades"},
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def _campaign_cell(
    *,
    market: PairMarketData,
    spec: PairStrategySpec,
    base: PairBacktestReport,
    stress: PairBacktestReport,
    fold_count: int,
) -> PairCampaignCellEvidence:
    fold_returns, active_folds, positive_folds = _fold_returns(
        base,
        fold_count=fold_count,
    )
    minimum_active_folds = math.ceil(fold_count / 2)
    temporal_stable = bool(
        active_folds >= minimum_active_folds
        and positive_folds >= math.ceil(active_folds / 2)
        and min(fold_returns, default=Decimal("0")) > Decimal("-20")
    )
    stress_positive = stress.return_percent > 0
    robustness_passed = bool(
        base.research_gate_passed
        and stress_positive
        and temporal_stable
        and not base.terminal_position_closed
    )
    reasons = list(base.validation_reasons)
    if not stress_positive:
        reasons.append("配对策略在加重双腿费用和滑点后收益不为正")
    if not temporal_stable:
        reasons.append(
            f"配对时间稳定性不足：活跃折 {active_folds}/{fold_count}，"
            f"正收益活跃折 {positive_folds}/{max(active_folds, 1)}"
        )
    wins = sum(trade.net_pnl > 0 for trade in base.trades)
    losses = sum(trade.net_pnl < 0 for trade in base.trades)
    statistical = base.statistical_validation
    cell = ResearchCampaignCell(
        library_entry_id=pair_strategy_id(spec),
        strategy_fingerprint=spec.fingerprint,
        family=(
            "relative_momentum"
            if spec.model == "relative_momentum_equal_dollar"
            else "relative_value"
        ),
        archetype=spec.model,
        dataset_id=market.id,
        exchange=market.dataset_a.exchange,
        symbol=market.pair_symbol,
        timeframe=market.dataset_a.timeframe,
        total_trades=base.total_trades,
        return_percent=base.return_percent,
        stress_return_percent=stress.return_percent,
        profit_factor=base.profit_factor,
        win_rate_percent=base.win_rate_percent,
        payoff_ratio=base.payoff_ratio,
        max_drawdown_percent=base.max_drawdown_percent,
        robustness_passed=robustness_passed,
        statistical_uncertainty_passed=statistical.uncertainty_passed,
        temporal_stability_passed=temporal_stable,
        fold_returns=fold_returns,
        validation_reasons=list(dict.fromkeys(reasons)),
        backtest_fingerprint=_backtest_fingerprint(base),
        win_count=wins,
        loss_count=losses,
        one_sided_sharpe_p_value=statistical.one_sided_sharpe_p_value,
        one_sided_win_rate_p_value=binomial_upper_tail_p_value(wins, wins + losses),
        deflated_sharpe_probability=statistical.deflated_sharpe_probability,
    )
    return PairCampaignCellEvidence(
        cell=cell,
        base_report_id=base.id,
        stress_report_id=stress.id,
        base_return_percent=base.return_percent,
        stress_return_percent=stress.return_percent,
        active_temporal_folds=active_folds,
        positive_temporal_folds=positive_folds,
        source_urls=sorted({spec.source_url}),
    )


def run_pair_research_campaign(
    markets: list[PairMarketData],
    *,
    strategy_specs: list[PairStrategySpec] | None = None,
    initial_equity: Decimal = Decimal("100000"),
    fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
    fold_count: int = 6,
    formation_fraction: Decimal = Decimal("0.25"),
    cumulative_hypothesis_count: int | None = None,
    effective_trial_count: int | None = None,
    report_sink: Callable[[PairBacktestReport], None] | None = None,
    report_id: str | None = None,
    parent_campaign_id: str | None = None,
    predecessor_campaign_id: str | None = None,
    experiment_kind: Literal[
        "initial",
        "structural_extension",
        "bounded_revision",
    ] = "initial",
    research_experiment_id: str = "",
    research_configuration_fingerprint: str = "",
    new_trial_variant_count: int | None = None,
    prior_hypothesis_fingerprints: list[str] | None = None,
    sequential_experiment_count: int = 1,
    generate_failure_revisions: bool = True,
    include_blind_pending_reason: bool = True,
) -> PairResearchCampaignReport:
    """Run bounded relative-value hypotheses across independent pair markets.

    This is a development-data research campaign.  It deliberately cannot
    issue an execution certificate: a selected hypothesis still requires a
    one-time sealed blind campaign and atomic two-leg execution support.
    """

    if not markets:
        raise ValueError("pair research campaign requires at least one market")
    if not Decimal("0.10") <= formation_fraction <= Decimal("0.75"):
        raise ValueError("pair formation fraction must be between 0.10 and 0.75")
    for market in markets:
        market.validate()
    if len({market.id for market in markets}) != len(markets):
        raise ValueError("pair research campaign contains duplicate market cells")
    specs = list(strategy_specs or PAIR_STRATEGY_LIBRARY)
    if not specs:
        raise ValueError("pair research campaign requires at least one strategy")
    if len({spec.fingerprint for spec in specs}) != len(specs):
        raise ValueError("pair research campaign contains duplicate hypotheses")
    current_fingerprints = [spec.fingerprint for spec in specs]
    all_hypotheses = set(prior_hypothesis_fingerprints or []) | set(current_fingerprints)
    effective_hypothesis_count = max(
        cumulative_hypothesis_count or 0,
        len(all_hypotheses),
        len(specs),
    )
    statistical_trial_count = max(
        effective_trial_count or 0,
        effective_hypothesis_count + max(sequential_experiment_count, 1) - 1,
    )
    evidence: list[PairCampaignCellEvidence] = []
    for spec in specs:
        for market in markets:
            timestamps_b = {item.timestamp for item in market.candles_b}
            aligned_count = sum(item.timestamp in timestamps_b for item in market.candles_a)
            evaluation_start_index = max(
                spec.formation_window,
                math.ceil(aligned_count * float(formation_fraction)),
            )
            if aligned_count - evaluation_start_index < max(fold_count * 10, 60):
                raise ValueError(
                    "pair campaign requires at least 60 out-of-sample trading bars "
                    "after the formation period"
                )
            base = run_pair_backtest(
                dataset_a=market.dataset_a,
                candles_a=market.candles_a,
                funding_a=market.funding_a,
                dataset_b=market.dataset_b,
                candles_b=market.candles_b,
                funding_b=market.funding_b,
                strategy_spec=spec,
                initial_equity=initial_equity,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                research_trial_count=statistical_trial_count,
                evaluation_start_index=evaluation_start_index,
            )
            stress = run_pair_backtest(
                dataset_a=market.dataset_a,
                candles_a=market.candles_a,
                funding_a=market.funding_a,
                dataset_b=market.dataset_b,
                candles_b=market.candles_b,
                funding_b=market.funding_b,
                strategy_spec=spec,
                initial_equity=initial_equity,
                fee_bps=fee_bps * Decimal("1.5"),
                slippage_bps=slippage_bps * Decimal("2"),
                research_trial_count=statistical_trial_count,
                evaluation_start_index=evaluation_start_index,
            )
            if report_sink is not None:
                report_sink(base)
                report_sink(stress)
            evidence.append(
                _campaign_cell(
                    market=market,
                    spec=spec,
                    base=base,
                    stress=stress,
                    fold_count=fold_count,
                )
            )
    candidates, pbo = aggregate_campaign(
        [item.cell for item in evidence],
        cumulative_hypothesis_count=statistical_trial_count,
    )
    provisional = next(
        (item for item in candidates if item.cross_market_passed),
        None,
    )
    failure_attribution = None
    recommended_revisions: list[PairHypothesisRevision] = []
    if (
        candidates
        and provisional is None
        and generate_failure_revisions
        and parent_campaign_id is None
    ):
        spec_by_id = {pair_strategy_id(spec): spec for spec in specs}
        parent_spec = spec_by_id.get(candidates[0].library_entry_id)
        if parent_spec is not None:
            failure_attribution, recommended_revisions = generate_pair_failure_revisions(
                candidates[0],
                parent_spec,
            )
    reasons: list[str] = []
    if provisional is None:
        reasons.append("没有相对价值策略通过跨配对、跨交易所稳健性门槛")
    if not pbo.passed:
        reasons.extend(pbo.reasons)
    if include_blind_pending_reason:
        reasons.append("尚未执行一次性密封盲测，禁止签发双腿执行资格")
    reasons.append("尚未验证双腿原子下单、部分成交补偿和双腿原生保护，执行保持禁用")
    return PairResearchCampaignReport(
        id=report_id or f"paircampaign_{uuid4().hex[:16]}",
        research_pair_ids=[market.id for market in markets],
        strategy_ids=[pair_strategy_id(spec) for spec in specs],
        strategy_fingerprints=current_fingerprints,
        research_lineage_id=pair_research_lineage_id(markets),
        parent_campaign_id=parent_campaign_id,
        predecessor_campaign_id=predecessor_campaign_id,
        experiment_kind=experiment_kind,
        research_experiment_id=research_experiment_id,
        research_configuration_fingerprint=research_configuration_fingerprint,
        cumulative_hypothesis_count=effective_hypothesis_count,
        sequential_experiment_count=max(sequential_experiment_count, 1),
        effective_trial_count=statistical_trial_count,
        new_trial_variant_count=(
            len(specs) if new_trial_variant_count is None else new_trial_variant_count
        ),
        trial_count=len(evidence),
        formation_fraction=formation_fraction,
        strict_formation_trading_separation=True,
        cells=evidence,
        candidates=candidates,
        pbo=pbo,
        provisional_selected_strategy_id=(
            provisional.library_entry_id if provisional is not None and pbo.passed else None
        ),
        failure_attribution=failure_attribution,
        recommended_revisions=recommended_revisions,
        selection_reasons=list(dict.fromkeys(reasons)),
    )
