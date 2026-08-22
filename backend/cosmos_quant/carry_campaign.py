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

from .carry import (
    CARRY_STRATEGY_LIBRARY,
    CarryBacktestReport,
    CarryStrategySpec,
    run_carry_backtest,
)
from .domain import Candle, FundingRatePoint
from .multi_leg_execution import MultiLegExecutionCertificate
from .research_campaign import (
    CampaignCandidateAggregate,
    ResearchCampaignCell,
    aggregate_campaign,
)
from .statistical_validation import BacktestOverfittingReport, binomial_upper_tail_p_value

CARRY_CAMPAIGN_ENGINE_VERSION = 4


@dataclass(frozen=True)
class CarryMarketData:
    spot_dataset: object
    spot_candles: list[Candle]
    perpetual_dataset: object
    perpetual_candles: list[Candle]
    funding_rates: list[FundingRatePoint]

    def validate(self) -> None:
        if self.spot_dataset.exchange != self.perpetual_dataset.exchange:
            raise ValueError("carry market legs must use one exchange")
        if self.spot_dataset.symbol != self.perpetual_dataset.symbol:
            raise ValueError("carry market legs must use one canonical symbol")
        if self.spot_dataset.timeframe != self.perpetual_dataset.timeframe:
            raise ValueError("carry market legs must use one timeframe")
        if self.spot_dataset.market_type != "spot":
            raise ValueError("carry market requires a spot leg")
        if self.perpetual_dataset.market_type != "swap":
            raise ValueError("carry market requires a perpetual swap leg")
        if not self.spot_candles or not self.perpetual_candles:
            raise ValueError("carry market data cannot be empty")

    @property
    def id(self) -> str:
        payload = {
            "spot_dataset_id": self.spot_dataset.id,
            "perpetual_dataset_id": self.perpetual_dataset.id,
            "spot_fingerprint": self.spot_dataset.data_fingerprint,
            "perpetual_fingerprint": self.perpetual_dataset.data_fingerprint,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"carrymarket_{sha256(encoded).hexdigest()[:20]}"


class CarryCampaignCellEvidence(BaseModel):
    cell: ResearchCampaignCell
    base_report_id: str
    stress_report_id: str
    base_return_percent: Decimal
    stress_return_percent: Decimal
    active_temporal_folds: int
    positive_temporal_folds: int
    source_urls: list[str]


class CarryFailureAttribution(BaseModel):
    codes: list[str]
    evidence: dict[str, Decimal | int | str]
    requires_new_data: bool = False
    summary: str


class CarryHypothesisRevision(BaseModel):
    label: str
    parent_strategy_fingerprint: str
    changed_parameters: dict[str, Decimal | int | str]
    rationale: str
    strategy_spec: CarryStrategySpec


class CarryResearchCampaignReport(BaseModel):
    id: str = Field(default_factory=lambda: f"carrycampaign_{uuid4().hex[:16]}")
    engine_version: int = CARRY_CAMPAIGN_ENGINE_VERSION
    status: str = "completed"
    research_market_ids: list[str]
    strategy_ids: list[str]
    strategy_fingerprints: list[str]
    research_lineage_id: str
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
    formation_fraction: Decimal = Decimal("0.25")
    strict_formation_trading_separation: bool = True
    trial_count: int
    cells: list[CarryCampaignCellEvidence]
    candidates: list[CampaignCandidateAggregate]
    pbo: BacktestOverfittingReport
    provisional_selected_strategy_id: str | None = None
    failure_attribution: CarryFailureAttribution | None = None
    recommended_revisions: list[CarryHypothesisRevision] = Field(default_factory=list)
    blind_set_id: str | None = None
    blind_consumption_id: str | None = None
    independent_blind_test_completed: bool = False
    blind_selected_candidate_passed: bool = False
    blind_market_count: int = 0
    blind_evidence_fingerprint: str | None = None
    execution_eligible: bool = False
    execution_certificate: MultiLegExecutionCertificate | None = None
    profitability_claim: Literal[False] = False
    selection_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def carry_strategy_id(spec: CarryStrategySpec) -> str:
    basis = str(spec.entry_basis_rate).replace(".", "p")
    funding = str(spec.minimum_observed_funding_rate).replace(".", "p")
    return f"carry_spot_perp_{basis}_{funding}_{spec.fingerprint[:8]}"


def carry_research_lineage_id(markets: list[CarryMarketData]) -> str:
    identities = [
        {
            "exchange": market.spot_dataset.exchange,
            "symbol": market.spot_dataset.symbol,
            "timeframe": market.spot_dataset.timeframe,
            "spot_source": (market.spot_dataset.source_dataset_id or market.spot_dataset.id),
            "perpetual_source": (
                market.perpetual_dataset.source_dataset_id or market.perpetual_dataset.id
            ),
        }
        for market in markets
    ]
    encoded = json.dumps(
        sorted(identities, key=lambda item: json.dumps(item, sort_keys=True)),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"carry_lineage_{sha256(encoded).hexdigest()[:20]}"


def generate_carry_failure_revisions(
    candidate: CampaignCandidateAggregate,
    parent_spec: CarryStrategySpec,
    *,
    max_revisions: int = 3,
) -> tuple[CarryFailureAttribution, list[CarryHypothesisRevision]]:
    """Create bounded one-variable children, or explicitly demand new data.

    Zero confirmed opportunities never authorizes lowering entry edge or funding
    thresholds after seeing the data. That outcome calls for a new market/time
    sample, not parameter fishing on the same lineage.
    """

    codes: list[str] = []
    if candidate.total_trades == 0:
        codes.append("no_confirmed_entry_opportunity")
    elif candidate.total_trades < 300:
        codes.append("insufficient_trade_sample")
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
    requires_new_data = candidate.total_trades == 0
    attribution = CarryFailureAttribution(
        codes=list(dict.fromkeys(codes)),
        evidence={
            "total_trades": candidate.total_trades,
            "median_win_rate_percent": candidate.median_win_rate_percent,
            "median_payoff_ratio": candidate.median_payoff_ratio,
            "median_profit_factor": candidate.median_profit_factor,
            "positive_stress_ratio": candidate.positive_stress_ratio,
            "temporal_stable_market_ratio": candidate.temporal_stable_market_ratio,
        },
        requires_new_data=requires_new_data,
        summary=(
            "No confirmed cash-and-carry opportunity occurred. Preserve the public prior "
            "and acquire a new independent market/time sample; do not lower edge or "
            "funding thresholds on this development lineage."
            if requires_new_data
            else "The carry candidate failed joint quality gates. Only predeclared, "
            "single-variable children may be tested and none inherit validation."
        ),
    )
    if requires_new_data or max_revisions <= 0:
        return attribution, []

    revisions: list[CarryHypothesisRevision] = []
    seen_parameters: set[str] = set()

    def add_revision(
        *,
        label: str,
        parameter: str,
        value: Decimal | int,
        rationale: str,
    ) -> None:
        if parameter in seen_parameters:
            return
        payload = parent_spec.model_dump(mode="python")
        payload[parameter] = value
        child = CarryStrategySpec.model_validate(payload)
        revisions.append(
            CarryHypothesisRevision(
                label=label,
                parent_strategy_fingerprint=parent_spec.fingerprint,
                changed_parameters={parameter: value},
                rationale=rationale,
                strategy_spec=child,
            )
        )
        seen_parameters.add(parameter)

    if {"cost_fragile", "low_payoff", "low_profit_factor"} & set(codes):
        stronger_basis = max(
            parent_spec.entry_basis_rate * Decimal("1.25"),
            parent_spec.entry_basis_rate + Decimal("0.0005"),
        ).quantize(Decimal("0.00001"))
        maximum_basis = (parent_spec.stop_basis_rate * Decimal("0.80")).quantize(Decimal("0.00001"))
        if stronger_basis < maximum_basis:
            add_revision(
                label="Require more pre-cost basis edge",
                parameter="entry_basis_rate",
                value=stronger_basis,
                rationale=(
                    "Stress costs or payoff failed; raise only the required premium. "
                    "This cannot manufacture more trades by weakening admission."
                ),
            )
    if "low_win_rate" in codes and parent_spec.confirmation_bars < 96:
        add_revision(
            label="Require longer basis confirmation",
            parameter="confirmation_bars",
            value=min(parent_spec.confirmation_bars * 2, 96),
            rationale=(
                "Win rate failed; increase only persistence confirmation to reject "
                "single-bar dislocations."
            ),
        )
    if "temporal_instability" in codes:
        shorter_holding = max(
            int(parent_spec.maximum_holding_bars * Decimal("0.67")),
            parent_spec.minimum_holding_bars + 1,
            24,
        )
        if shorter_holding < parent_spec.maximum_holding_bars:
            add_revision(
                label="Limit stale carry exposure",
                parameter="maximum_holding_bars",
                value=shorter_holding,
                rationale=(
                    "Temporal stability failed; shorten only the maximum holding period "
                    "to test whether stale carry exposure drives the failure."
                ),
            )
    return attribution, revisions[:max_revisions]


def _fold_returns(
    report: CarryBacktestReport,
    *,
    fold_count: int,
) -> tuple[list[Decimal], int, int]:
    total_seconds = (report.data_end - report.evaluation_start).total_seconds()
    growth = [Decimal("1") for _ in range(fold_count)]
    counts = [0 for _ in range(fold_count)]
    for trade in report.trades:
        elapsed = max((trade.exit_time - report.evaluation_start).total_seconds(), 0.0)
        fold = (
            min(int(elapsed / total_seconds * fold_count), fold_count - 1)
            if total_seconds > 0
            else fold_count - 1
        )
        growth[fold] *= Decimal("1") + trade.return_percent / Decimal("100")
        counts[fold] += 1
    returns = [
        ((value - Decimal("1")) * Decimal("100")).quantize(Decimal("0.0001")) for value in growth
    ]
    active = sum(count > 0 for count in counts)
    positive = sum(value > 0 for value, count in zip(returns, counts, strict=True) if count)
    return returns, active, positive


def _backtest_fingerprint(report: CarryBacktestReport) -> str:
    payload = report.model_dump(mode="json", exclude={"id", "created_at", "trades"})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def _campaign_cell(
    *,
    market: CarryMarketData,
    spec: CarryStrategySpec,
    base: CarryBacktestReport,
    stress: CarryBacktestReport,
    fold_count: int,
) -> CarryCampaignCellEvidence:
    fold_returns, active_folds, positive_folds = _fold_returns(
        base,
        fold_count=fold_count,
    )
    temporal_stable = bool(
        active_folds >= math.ceil(fold_count / 2)
        and positive_folds >= math.ceil(active_folds / 2)
        and min(fold_returns, default=Decimal("0")) > Decimal("-10")
    )
    robustness_passed = bool(
        base.research_gate_passed
        and stress.return_percent > 0
        and temporal_stable
        and not base.terminal_position_closed
    )
    reasons = list(base.validation_reasons)
    if stress.return_percent <= 0:
        reasons.append("carry return is not positive under stressed dual-leg costs")
    if not temporal_stable:
        reasons.append(
            f"carry temporal stability failed: active folds {active_folds}/{fold_count}, "
            f"positive active folds {positive_folds}/{max(active_folds, 1)}"
        )
    wins = sum(trade.net_pnl > 0 for trade in base.trades)
    losses = sum(trade.net_pnl < 0 for trade in base.trades)
    statistical = base.statistical_validation
    cell = ResearchCampaignCell(
        library_entry_id=carry_strategy_id(spec),
        strategy_fingerprint=spec.fingerprint,
        family="carry",
        archetype=spec.model,
        dataset_id=market.id,
        exchange=market.spot_dataset.exchange,
        symbol=market.spot_dataset.symbol,
        timeframe=market.spot_dataset.timeframe,
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
    return CarryCampaignCellEvidence(
        cell=cell,
        base_report_id=base.id,
        stress_report_id=stress.id,
        base_return_percent=base.return_percent,
        stress_return_percent=stress.return_percent,
        active_temporal_folds=active_folds,
        positive_temporal_folds=positive_folds,
        source_urls=[spec.source_url],
    )


def run_carry_research_campaign(
    markets: list[CarryMarketData],
    *,
    strategy_specs: list[CarryStrategySpec] | None = None,
    initial_equity: Decimal = Decimal("100000"),
    spot_fee_bps: Decimal = Decimal("8"),
    perpetual_fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
    fold_count: int = 6,
    formation_fraction: Decimal = Decimal("0.25"),
    cumulative_hypothesis_count: int | None = None,
    sequential_experiment_count: int = 1,
    effective_trial_count: int | None = None,
    new_trial_variant_count: int | None = None,
    research_experiment_id: str = "",
    research_configuration_fingerprint: str = "",
    report_id: str | None = None,
    report_sink: Callable[[CarryBacktestReport], None] | None = None,
    parent_campaign_id: str | None = None,
    predecessor_campaign_id: str | None = None,
    experiment_kind: Literal[
        "initial",
        "structural_extension",
        "bounded_revision",
    ] = "initial",
    generate_failure_revisions: bool = True,
    include_blind_pending_reason: bool = True,
) -> CarryResearchCampaignReport:
    if not markets:
        raise ValueError("carry campaign requires at least one market")
    if fold_count < 4 or fold_count % 2:
        raise ValueError("carry fold count must be an even integer of at least four")
    if not Decimal("0.10") <= formation_fraction <= Decimal("0.75"):
        raise ValueError("carry formation fraction must be between 0.10 and 0.75")
    for market in markets:
        market.validate()
    if len({market.id for market in markets}) != len(markets):
        raise ValueError("carry campaign contains duplicate market cells")
    specs = list(strategy_specs or CARRY_STRATEGY_LIBRARY)
    if not specs or len({spec.fingerprint for spec in specs}) != len(specs):
        raise ValueError("carry campaign requires unique strategy hypotheses")
    hypothesis_count = max(cumulative_hypothesis_count or 0, len(specs))
    trial_count = max(
        effective_trial_count or 0,
        hypothesis_count + max(sequential_experiment_count, 1) - 1,
    )
    evidence: list[CarryCampaignCellEvidence] = []
    for spec in specs:
        for market in markets:
            perpetual_times = {item.timestamp for item in market.perpetual_candles}
            aligned_count = sum(item.timestamp in perpetual_times for item in market.spot_candles)
            evaluation_index = max(
                spec.confirmation_bars,
                math.ceil(aligned_count * float(formation_fraction)),
            )
            if aligned_count - evaluation_index < max(fold_count * 10, 60):
                raise ValueError("carry campaign requires at least 60 out-of-sample trading bars")
            common = {
                "spot_dataset": market.spot_dataset,
                "spot_candles": market.spot_candles,
                "perpetual_dataset": market.perpetual_dataset,
                "perpetual_candles": market.perpetual_candles,
                "funding_rates": market.funding_rates,
                "strategy_spec": spec,
                "initial_equity": initial_equity,
                "research_trial_count": trial_count,
                "evaluation_start_index": evaluation_index,
            }
            base = run_carry_backtest(
                **common,
                spot_fee_bps=spot_fee_bps,
                perpetual_fee_bps=perpetual_fee_bps,
                slippage_bps=slippage_bps,
            )
            stress = run_carry_backtest(
                **common,
                spot_fee_bps=spot_fee_bps * Decimal("1.5"),
                perpetual_fee_bps=perpetual_fee_bps * Decimal("1.5"),
                slippage_bps=slippage_bps * Decimal("2"),
                capital_cost_multiplier=Decimal("1.5"),
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
        cumulative_hypothesis_count=trial_count,
    )
    provisional = next((item for item in candidates if item.cross_market_passed), None)
    selected = provisional.library_entry_id if provisional is not None and pbo.passed else None
    failure_attribution = None
    recommended_revisions: list[CarryHypothesisRevision] = []
    if (
        candidates
        and selected is None
        and generate_failure_revisions
        and parent_campaign_id is None
    ):
        spec_by_id = {carry_strategy_id(spec): spec for spec in specs}
        parent_spec = spec_by_id.get(candidates[0].library_entry_id)
        if parent_spec is not None:
            failure_attribution, recommended_revisions = generate_carry_failure_revisions(
                candidates[0],
                parent_spec,
            )
    reasons: list[str] = []
    if selected is None:
        reasons.append("no carry hypothesis passed cross-market quality and PBO")
    reasons.extend(pbo.reasons if not pbo.passed else [])
    if include_blind_pending_reason:
        reasons.append("sealed blind carry evaluation has not been completed")
    reasons.extend(
        [
            "atomic spot-perpetual execution and partial-fill compensation are not verified",
            "carry research cannot issue an execution certificate",
        ]
    )
    return CarryResearchCampaignReport(
        id=report_id or f"carrycampaign_{uuid4().hex[:16]}",
        research_market_ids=[market.id for market in markets],
        strategy_ids=[carry_strategy_id(spec) for spec in specs],
        strategy_fingerprints=[spec.fingerprint for spec in specs],
        research_lineage_id=carry_research_lineage_id(markets),
        parent_campaign_id=parent_campaign_id,
        predecessor_campaign_id=predecessor_campaign_id,
        experiment_kind=experiment_kind,
        research_experiment_id=research_experiment_id,
        research_configuration_fingerprint=research_configuration_fingerprint,
        cumulative_hypothesis_count=hypothesis_count,
        sequential_experiment_count=max(sequential_experiment_count, 1),
        effective_trial_count=trial_count,
        new_trial_variant_count=(
            len(specs) if new_trial_variant_count is None else new_trial_variant_count
        ),
        formation_fraction=formation_fraction,
        trial_count=len(evidence),
        cells=evidence,
        candidates=candidates,
        pbo=pbo,
        provisional_selected_strategy_id=selected,
        failure_attribution=failure_attribution,
        recommended_revisions=recommended_revisions,
        selection_reasons=list(dict.fromkeys(reasons)),
    )
