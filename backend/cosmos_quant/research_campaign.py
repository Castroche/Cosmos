from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from statistics import median
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field

from .backtest import (
    BacktestResult,
    CausalStrategyPrior,
    causal_library_policy_fingerprint,
)
from .statistical_validation import (
    BacktestOverfittingReport,
    benjamini_hochberg_adjusted_p_values,
    binomial_upper_tail_p_value,
    cauchy_combined_p_value,
    estimate_probability_of_backtest_overfitting,
    wilson_lower_bound,
)

CAMPAIGN_ENGINE_VERSION = 17
CAMPAIGN_CELL_CONCURRENCY = min(8, max(2, (os.cpu_count() or 4) // 2))


class ResearchCampaignJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchCampaignJob(BaseModel):
    id: str = Field(default_factory=lambda: f"rcj_{uuid4().hex[:16]}")
    request: dict
    request_fingerprint: str
    status: ResearchCampaignJobStatus = ResearchCampaignJobStatus.QUEUED
    total_cells: int = 0
    completed_cells: int = 0
    cache_hits: int = 0
    computed_cells: int = 0
    failed_cells: int = 0
    report_id: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def campaign_request_fingerprint(request_payload: dict) -> str:
    encoded = json.dumps(
        request_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:20]


def research_lineage_id(
    datasets: list[object],
    *,
    blind_set_id: str | None = None,
) -> str:
    """Identify an original-data lineage independently of split and ordering.

    ``blind_set_id`` is accepted for backwards-compatible callers but is not an
    identity component. Re-splitting the same immutable sources must not reset
    multiplicity or permit a second blind set on the same evidence lineage.
    """

    del blind_set_id
    sources = [
        {
            "source_identity": (
                getattr(dataset, "source_dataset_id", None) or getattr(dataset, "id", "")
            ),
            "source_fingerprint": (
                ""
                if getattr(dataset, "source_dataset_id", None)
                else getattr(dataset, "data_fingerprint", "")
            ),
            "exchange": getattr(dataset, "exchange", ""),
            "market_type": getattr(dataset, "market_type", ""),
            "symbol": getattr(dataset, "symbol", ""),
            "timeframe": getattr(dataset, "timeframe", ""),
        }
        for dataset in datasets
    ]
    identity: dict[str, object] = {
        "datasets": sorted(
            sources,
            key=lambda item: (
                str(item["exchange"]),
                str(item["market_type"]),
                str(item["symbol"]),
                str(item["timeframe"]),
                str(item["source_identity"]),
                str(item["source_fingerprint"]),
            ),
        )
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"research_lineage_{sha256(encoded).hexdigest()[:20]}"


def research_trial_family_id(datasets: list[object]) -> str:
    """Bind multiplicity to the research domain, not downloaded file IDs.

    A refreshed download, a different chronological split, another symbol, or
    another exchange must not reset how many strategy variants have already
    been tried in the same single-leg market type.  Evidence lineages remain
    separate so genuinely new blind data can still be sealed independently.
    """

    market_types = sorted(
        {
            str(getattr(dataset, "market_type", "")).strip().lower()
            for dataset in datasets
            if str(getattr(dataset, "market_type", "")).strip()
        }
    )
    if not market_types:
        raise ValueError("research trial family requires at least one market type")
    payload = {
        "version": 1,
        "research_domain": "single_leg_crypto_strategy_campaign",
        "market_types": market_types,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"research_trial_family_{sha256(encoded).hexdigest()[:20]}"


def research_experiment_fingerprint(
    *,
    lineage_id: str,
    engine_version: int,
    research_configuration_fingerprint: str,
    strategy_fingerprints: list[str],
) -> str:
    payload = {
        "lineage_id": lineage_id,
        "engine_version": engine_version,
        "research_configuration_fingerprint": research_configuration_fingerprint,
        "strategy_fingerprints": sorted(set(strategy_fingerprints)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"research_experiment_{sha256(encoded).hexdigest()[:20]}"


def campaign_cell_cache_key(
    *,
    strategy_fingerprint: str,
    dataset_fingerprint: str,
    dataset_id: str,
    trial_count: int,
    research_configuration_fingerprint: str,
) -> str:
    payload = {
        "engine_version": CAMPAIGN_ENGINE_VERSION,
        "strategy_fingerprint": strategy_fingerprint,
        "dataset_fingerprint": dataset_fingerprint,
        "dataset_id": dataset_id,
        "trial_count": trial_count,
        "research_configuration_fingerprint": research_configuration_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


class ResearchCampaignCell(BaseModel):
    library_entry_id: str
    strategy_fingerprint: str
    family: str
    archetype: str
    dataset_id: str
    exchange: str = "unknown"
    symbol: str
    timeframe: str
    total_trades: int
    return_percent: Decimal
    stress_return_percent: Decimal | None
    profit_factor: Decimal
    win_rate_percent: Decimal
    payoff_ratio: Decimal
    max_drawdown_percent: Decimal
    robustness_passed: bool
    statistical_uncertainty_passed: bool
    temporal_stability_passed: bool
    fold_returns: list[Decimal] = Field(default_factory=list)
    validation_reasons: list[str] = Field(default_factory=list)
    backtest_fingerprint: str
    dataset_role: str = "research"
    data_eligible: bool = True
    data_ineligibility_reasons: list[str] = Field(default_factory=list)
    win_count: int = Field(default=0, ge=0)
    loss_count: int = Field(default=0, ge=0)
    one_sided_sharpe_p_value: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    one_sided_win_rate_p_value: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    deflated_sharpe_probability: Decimal = Field(default=Decimal("0"), ge=0, le=1)


class CampaignCandidateAggregate(BaseModel):
    library_entry_id: str
    strategy_fingerprint: str
    family: str
    archetype: str
    market_count: int
    robust_market_count: int
    positive_stress_market_count: int
    unique_symbol_count: int = 0
    unique_exchange_count: int = 0
    unique_timeframe_count: int = 0
    high_quality_market_count: int = 0
    robust_market_ratio: Decimal
    positive_stress_ratio: Decimal
    high_quality_market_ratio: Decimal = Decimal("0")
    median_return_percent: Decimal
    median_profit_factor: Decimal
    median_win_rate_percent: Decimal
    median_payoff_ratio: Decimal
    worst_drawdown_percent: Decimal
    cross_market_passed: bool
    rank_score: Decimal
    reasons: list[str] = Field(default_factory=list)
    total_trades: int = 0
    temporal_stable_market_count: int = 0
    temporal_stable_market_ratio: Decimal = Decimal("0")
    pooled_win_rate_lower_bound_percent: Decimal = Decimal("0")
    strategy_p_value: Decimal = Decimal("1")
    multiple_testing_adjusted_p_value: Decimal = Decimal("1")
    strategy_statistical_passed: bool = False


def campaign_candidate_fingerprint(candidate: CampaignCandidateAggregate) -> str:
    encoded = json.dumps(
        candidate.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def campaign_portfolio_allocations_fingerprint(allocations: list[dict]) -> str:
    encoded = json.dumps(
        allocations,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def validate_causal_policy_definition(definition: dict) -> str:
    """Recompute one frozen Agent policy fingerprint from persisted inputs."""

    priors = [
        CausalStrategyPrior.model_validate(item)
        for item in definition.get("priors", [])
    ]
    if len(priors) < 2:
        raise ValueError("causal policy definition requires at least two frozen priors")
    if len({prior.id for prior in priors}) != len(priors):
        raise ValueError("causal policy definition contains duplicate prior identifiers")
    expected = causal_library_policy_fingerprint(
        priors,
        maximum_candidates_per_bar=int(definition["maximum_candidates_per_bar"]),
        abstain_on_ambiguous_regime=bool(
            definition["abstain_on_ambiguous_regime"]
        ),
        minimum_regime_confidence=Decimal(
            str(definition.get("minimum_regime_confidence", "0"))
        ),
        minimum_signal_confidence=Decimal(
            str(definition.get("minimum_signal_confidence", "0"))
        ),
        minimum_directional_consensus=int(
            definition.get("minimum_directional_consensus", 1)
        ),
    )
    if definition.get("policy_fingerprint") != expected:
        raise ValueError("causal policy definition does not match its executed fingerprint")
    return expected


def causal_policy_definitions_fingerprint(definitions: list[dict]) -> str:
    """Bind every pre-registered meta-policy, independent of request ordering."""

    if not definitions:
        return ""
    normalized: list[dict] = []
    entry_ids: set[str] = set()
    strategy_fingerprints: set[str] = set()
    for definition in definitions:
        entry_id = str(definition.get("campaign_entry_id") or "")
        if not entry_id or entry_id in entry_ids:
            raise ValueError("causal policy campaign entry identifiers must be unique")
        entry_ids.add(entry_id)
        strategy_fingerprint = validate_causal_policy_definition(definition)
        if strategy_fingerprint in strategy_fingerprints:
            raise ValueError("causal policy hypotheses must have unique fingerprints")
        strategy_fingerprints.add(strategy_fingerprint)
        normalized.append(definition)
    encoded = json.dumps(
        sorted(normalized, key=lambda item: str(item["campaign_entry_id"])),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


class CampaignExecutionCertificate(BaseModel):
    """Immutable binding between one admitted strategy and all sealed evidence."""

    certificate_version: int = 8
    campaign_id: str
    engine_version: int
    selected_library_entry_id: str
    strategy_fingerprint: str
    research_dataset_fingerprints: dict[str, str]
    blind_dataset_fingerprints: dict[str, str]
    blind_set_id: str
    blind_consumption_id: str
    pbo_upper_confidence_bound: Decimal
    median_win_rate_percent: Decimal
    median_payoff_ratio: Decimal
    median_profit_factor: Decimal
    high_quality_market_ratio: Decimal
    pooled_win_rate_lower_bound_percent: Decimal = Decimal("0")
    multiple_testing_adjusted_p_value: Decimal = Decimal("1")
    research_lineage_id: str = ""
    research_trial_family_id: str = ""
    research_experiment_id: str = ""
    research_configuration_fingerprint: str = ""
    cumulative_hypothesis_count: int = 1
    sequential_experiment_count: int = 1
    effective_trial_count: int = 1
    universe_snapshot_id: str = ""
    universe_snapshot_fingerprint: str = ""
    survivorship_bias_control_passed: bool = False
    research_aggregate_fingerprint: str = ""
    blind_aggregate_fingerprint: str = ""
    selected_strategy_definition_fingerprint: str = ""
    causal_policy_definitions_fingerprint: str = ""
    portfolio_allocations_fingerprint: str = ""
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"fingerprint"},
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()[:24]


class ResearchCampaignReport(BaseModel):
    id: str = Field(default_factory=lambda: f"campaign_{uuid4().hex[:16]}")
    engine_version: int = 0
    status: str = "completed"
    dataset_ids: list[str]
    research_dataset_ids: list[str] = Field(default_factory=list)
    blind_dataset_ids: list[str] = Field(default_factory=list)
    blind_set_id: str | None = None
    blind_consumption_id: str | None = None
    library_entry_ids: list[str]
    causal_policy_definitions: list[dict] = Field(default_factory=list)
    causal_policy_definitions_fingerprint: str = ""
    trial_count: int
    research_lineage_id: str = ""
    research_trial_family_id: str = ""
    research_experiment_id: str = ""
    research_configuration_fingerprint: str = ""
    cumulative_hypothesis_count: int = 1
    sequential_experiment_count: int = 1
    effective_trial_count: int = 1
    parent_campaign_id: str | None = None
    predecessor_campaign_id: str | None = None
    experiment_kind: str = "initial"
    authorized_revision_candidate_ids: list[str] = Field(default_factory=list)
    authorized_revision_fingerprints: list[str] = Field(default_factory=list)
    authorized_policy_revision_definitions: list[dict] = Field(default_factory=list)
    authorized_policy_revision_fingerprints: list[str] = Field(default_factory=list)
    revision_source_library_entry_id: str | None = None
    universe_snapshot_id: str | None = None
    universe_snapshot_fingerprint: str | None = None
    point_in_time_selection_passed: bool = False
    historical_survivorship_complete: bool = False
    survivorship_bias_control_passed: bool = False
    cells: list[ResearchCampaignCell]
    candidates: list[CampaignCandidateAggregate]
    pbo: BacktestOverfittingReport
    portfolio_allocations: list[dict] = Field(default_factory=list)
    selected_library_entry_id: str | None = None
    autonomous_execution_eligible: bool = False
    blind_selected_candidate_passed: bool = False
    blind_candidate: CampaignCandidateAggregate | None = None
    execution_certificate: CampaignExecutionCertificate | None = None
    selection_reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def campaign_cell(
    *,
    library_entry,
    dataset,
    result: BacktestResult,
    backtest_fingerprint: str,
    dataset_role: str = "research",
) -> ResearchCampaignCell:
    expected_strategy_fingerprint = library_entry.validated_spec.fingerprint
    if (
        result.strategy_fingerprint
        and result.strategy_fingerprint != expected_strategy_fingerprint
    ):
        raise ValueError("backtest strategy fingerprint does not match Campaign hypothesis")
    temporal = result.temporal_stability
    episode_pnls = (
        [episode.pnl for episode in result.trade_episodes]
        if result.trade_episodes
        else [trade.pnl for trade in result.trades]
    )
    statistical = result.statistical_validation
    return ResearchCampaignCell(
        library_entry_id=library_entry.id,
        strategy_fingerprint=expected_strategy_fingerprint,
        family=library_entry.family.value,
        archetype=library_entry.archetype,
        dataset_id=dataset.id,
        exchange=dataset.exchange,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        total_trades=result.total_trades,
        return_percent=result.return_percent,
        stress_return_percent=result.stress_return_percent,
        profit_factor=result.profit_factor,
        win_rate_percent=result.win_rate_percent,
        payoff_ratio=result.payoff_ratio,
        max_drawdown_percent=result.max_drawdown_percent,
        robustness_passed=result.robustness_passed,
        statistical_uncertainty_passed=bool(
            result.statistical_validation and result.statistical_validation.uncertainty_passed
        ),
        temporal_stability_passed=bool(temporal and temporal.passed),
        fold_returns=([window.return_percent for window in temporal.windows] if temporal else []),
        validation_reasons=result.validation_reasons,
        backtest_fingerprint=backtest_fingerprint,
        dataset_role=dataset_role,
        win_count=sum(pnl > 0 for pnl in episode_pnls),
        loss_count=sum(pnl < 0 for pnl in episode_pnls),
        one_sided_sharpe_p_value=(
            statistical.one_sided_sharpe_p_value if statistical else Decimal("1")
        ),
        one_sided_win_rate_p_value=binomial_upper_tail_p_value(
            sum(pnl > 0 for pnl in episode_pnls),
            sum(pnl != 0 for pnl in episode_pnls),
        ),
        deflated_sharpe_probability=(
            statistical.deflated_sharpe_probability if statistical else Decimal("0")
        ),
    )


FEATURE_MAXIMUM_GAP_HOURS = {
    "funding_rate": 48,
    "open_interest": 48,
    "positioning": 48,
    "basis": 48,
    "order_flow": 8,
    "liquidation_flow": 1,
}


def historical_feature_eligibility(
    *,
    dataset,
    required_data: list[str],
    funding,
    open_interest,
    positioning,
    basis,
    order_flow,
    liquidation_flow=(),
) -> list[str]:
    """Reject sparse-factor backtests before they can enter selection statistics."""

    series = {
        "funding_rate": funding,
        "open_interest": open_interest,
        "positioning": positioning,
        "basis": basis,
        "order_flow": order_flow,
        "liquidation_flow": liquidation_flow,
    }
    reasons: list[str] = []
    for feature in required_data:
        if feature == "ohlcv":
            continue
        points = sorted(series.get(feature, []), key=lambda item: item.timestamp)
        if len(points) < 2:
            reasons.append(f"{feature}: fewer than two causal observations")
            continue
        maximum_gap_hours = FEATURE_MAXIMUM_GAP_HOURS.get(feature, 48)
        tolerance = timedelta(hours=maximum_gap_hours)
        endpoint_tolerance = timedelta(hours=max(maximum_gap_hours, 24))
        if points[0].timestamp > dataset.data_start + tolerance:
            reasons.append(
                f"{feature}: starts {points[0].timestamp.isoformat()} after dataset start"
            )
        if points[-1].timestamp < dataset.data_end - endpoint_tolerance:
            reasons.append(f"{feature}: ends {points[-1].timestamp.isoformat()} before dataset end")
        observed_gap = max(
            (
                (current.timestamp - previous.timestamp).total_seconds() / 3600
                for previous, current in zip(points, points[1:], strict=False)
            ),
            default=0,
        )
        if observed_gap > maximum_gap_hours:
            reasons.append(
                f"{feature}: maximum gap {observed_gap:.3f}h exceeds {maximum_gap_hours}h"
            )
    return reasons


def ineligible_campaign_cell(
    *,
    library_entry,
    dataset,
    reasons: list[str],
    dataset_role: str,
) -> ResearchCampaignCell:
    payload = {
        "engine_version": CAMPAIGN_ENGINE_VERSION,
        "strategy_fingerprint": library_entry.validated_spec.fingerprint,
        "dataset_fingerprint": dataset.data_fingerprint,
        "reasons": reasons,
    }
    fingerprint = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return ResearchCampaignCell(
        library_entry_id=library_entry.id,
        strategy_fingerprint=library_entry.validated_spec.fingerprint,
        family=library_entry.family.value,
        archetype=library_entry.archetype,
        dataset_id=dataset.id,
        exchange=getattr(dataset, "exchange", "unknown"),
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        total_trades=0,
        return_percent=Decimal("0"),
        stress_return_percent=None,
        profit_factor=Decimal("0"),
        win_rate_percent=Decimal("0"),
        payoff_ratio=Decimal("0"),
        max_drawdown_percent=Decimal("0"),
        robustness_passed=False,
        statistical_uncertainty_passed=False,
        temporal_stability_passed=False,
        validation_reasons=["required historical data coverage failed", *reasons],
        backtest_fingerprint=f"ineligible_{fingerprint}",
        dataset_role=dataset_role,
        data_eligible=False,
        data_ineligibility_reasons=reasons,
    )


def _aggregate_campaign_v8(
    cells: list[ResearchCampaignCell],
    *,
    minimum_robust_market_ratio: Decimal = Decimal("0.50"),
    minimum_positive_stress_ratio: Decimal = Decimal("0.60"),
    minimum_high_quality_market_ratio: Decimal = Decimal("0.50"),
    minimum_markets: int = 3,
    minimum_unique_symbols: int = 3,
    minimum_median_win_rate_percent: Decimal = Decimal("50"),
    minimum_median_payoff_ratio: Decimal = Decimal("1.30"),
    minimum_median_profit_factor: Decimal = Decimal("1.25"),
    maximum_pbo: Decimal = Decimal("0.20"),
) -> tuple[list[CampaignCandidateAggregate], BacktestOverfittingReport]:
    grouped: dict[str, list[ResearchCampaignCell]] = {}
    for cell in cells:
        if not cell.data_eligible:
            continue
        grouped.setdefault(cell.library_entry_id, []).append(cell)
    aggregates: list[CampaignCandidateAggregate] = []
    pbo_cohorts: dict[tuple[str, ...], list[list[Decimal]]] = {}
    for entry_id, items in grouped.items():
        market_count = len(items)
        robust_count = sum(item.robustness_passed for item in items)
        positive_stress_count = sum(
            item.stress_return_percent is not None and item.stress_return_percent > 0
            for item in items
        )
        unique_symbol_count = len({item.symbol for item in items})
        unique_timeframe_count = len({item.timeframe for item in items})
        high_quality_count = sum(
            item.win_rate_percent >= minimum_median_win_rate_percent
            and item.payoff_ratio >= minimum_median_payoff_ratio
            and item.profit_factor >= minimum_median_profit_factor
            for item in items
        )
        robust_ratio = Decimal(robust_count) / Decimal(market_count)
        stress_ratio = Decimal(positive_stress_count) / Decimal(market_count)
        high_quality_ratio = Decimal(high_quality_count) / Decimal(market_count)
        median_return_percent = Decimal(str(median([float(item.return_percent) for item in items])))
        median_profit_factor = Decimal(str(median([float(item.profit_factor) for item in items])))
        median_win_rate_percent = Decimal(
            str(median([float(item.win_rate_percent) for item in items]))
        )
        median_payoff_ratio = Decimal(str(median([float(item.payoff_ratio) for item in items])))
        enough_markets = market_count >= minimum_markets
        enough_unique_symbols = unique_symbol_count >= minimum_unique_symbols
        cross_market_passed = bool(
            enough_markets
            and enough_unique_symbols
            and robust_ratio >= minimum_robust_market_ratio
            and stress_ratio >= minimum_positive_stress_ratio
            and high_quality_ratio >= minimum_high_quality_market_ratio
            and median_win_rate_percent >= minimum_median_win_rate_percent
            and median_payoff_ratio >= minimum_median_payoff_ratio
            and median_profit_factor >= minimum_median_profit_factor
        )
        reasons: list[str] = []
        if not enough_markets:
            reasons.append(f"仅覆盖 {market_count} 个市场，少于要求的 {minimum_markets}")
        if robust_ratio < minimum_robust_market_ratio:
            reasons.append(f"稳健市场比例 {robust_ratio:.2f} 低于 {minimum_robust_market_ratio}")
        if stress_ratio < minimum_positive_stress_ratio:
            reasons.append(
                f"正压力收益市场比例 {stress_ratio:.2f} 低于 {minimum_positive_stress_ratio}"
            )
        if not enough_unique_symbols:
            reasons.append(
                f"仅覆盖 {unique_symbol_count} 个独立币种，少于要求的 {minimum_unique_symbols}"
            )
        if high_quality_ratio < minimum_high_quality_market_ratio:
            reasons.append(
                "胜率、盈亏比和 Profit Factor 同时达标的市场比例 "
                f"{high_quality_ratio:.2f} 低于 {minimum_high_quality_market_ratio}"
            )
        if median_win_rate_percent < minimum_median_win_rate_percent:
            reasons.append(
                f"跨市场胜率中位数 {median_win_rate_percent:.2f}% "
                f"低于 {minimum_median_win_rate_percent}%"
            )
        if median_payoff_ratio < minimum_median_payoff_ratio:
            reasons.append(
                f"跨市场盈亏比中位数 {median_payoff_ratio:.2f} 低于 {minimum_median_payoff_ratio}"
            )
        if median_profit_factor < minimum_median_profit_factor:
            reasons.append(
                f"跨市场 Profit Factor 中位数 {median_profit_factor:.2f} "
                f"低于 {minimum_median_profit_factor}"
            )
        rank_score = (
            robust_ratio * Decimal("30")
            + stress_ratio * Decimal("15")
            + high_quality_ratio * Decimal("25")
            + median_profit_factor * Decimal("10")
            + median_payoff_ratio * Decimal("5")
            + (median_win_rate_percent / Decimal("100")) * Decimal("15")
            - max(item.max_drawdown_percent for item in items) * Decimal("0.5")
        )
        aggregates.append(
            CampaignCandidateAggregate(
                library_entry_id=entry_id,
                strategy_fingerprint=items[0].strategy_fingerprint,
                family=items[0].family,
                archetype=items[0].archetype,
                market_count=market_count,
                robust_market_count=robust_count,
                positive_stress_market_count=positive_stress_count,
                unique_symbol_count=unique_symbol_count,
                unique_timeframe_count=unique_timeframe_count,
                high_quality_market_count=high_quality_count,
                robust_market_ratio=robust_ratio.quantize(Decimal("0.001")),
                positive_stress_ratio=stress_ratio.quantize(Decimal("0.001")),
                high_quality_market_ratio=high_quality_ratio.quantize(Decimal("0.001")),
                median_return_percent=median_return_percent.quantize(Decimal("0.01")),
                median_profit_factor=median_profit_factor.quantize(Decimal("0.01")),
                median_win_rate_percent=median_win_rate_percent.quantize(Decimal("0.01")),
                median_payoff_ratio=median_payoff_ratio.quantize(Decimal("0.01")),
                worst_drawdown_percent=max(item.max_drawdown_percent for item in items),
                cross_market_passed=cross_market_passed,
                rank_score=rank_score.quantize(Decimal("0.01")),
                reasons=reasons or ["跨市场稳健性与成本压力比例通过"],
            )
        )
        fold_row = [
            fold_return
            for item in sorted(
                items,
                key=lambda value: (
                    value.symbol,
                    value.timeframe,
                    value.dataset_id,
                ),
            )
            for fold_return in item.fold_returns
        ]
        if fold_row:
            evidence_scope = tuple(sorted(item.dataset_id for item in items))
            pbo_cohorts.setdefault(evidence_scope, []).append(fold_row)
    aggregates.sort(key=lambda item: item.rank_score, reverse=True)
    # Strategies requiring unavailable features can legitimately have a
    # smaller eligible market scope than OHLCV-only strategies. PBO ranks must
    # remain comparable, so evaluate the largest cohort that used the exact
    # same immutable datasets instead of discarding every strategy because row
    # lengths differ across cohorts.
    comparable_rows = (
        max(
            pbo_cohorts.values(),
            key=lambda rows: (len(rows), len(rows[0]) if rows else 0),
        )
        if pbo_cohorts
        else []
    )
    equal_length_rows = (
        comparable_rows
        if comparable_rows
        and len({len(row) for row in comparable_rows}) == 1
        and len(comparable_rows[0]) % 2 == 0
        else []
    )
    pbo = estimate_probability_of_backtest_overfitting(
        equal_length_rows,
        maximum_probability=maximum_pbo,
    )
    return aggregates, pbo


def aggregate_campaign(
    cells: list[ResearchCampaignCell],
    *,
    minimum_positive_stress_ratio: Decimal = Decimal("0.60"),
    minimum_high_quality_market_ratio: Decimal = Decimal("0.50"),
    minimum_temporal_stable_market_ratio: Decimal = Decimal("0.50"),
    minimum_markets: int = 3,
    minimum_unique_symbols: int = 3,
    minimum_unique_exchanges: int = 2,
    minimum_total_trades: int = 300,
    minimum_median_win_rate_percent: Decimal = Decimal("50"),
    minimum_median_payoff_ratio: Decimal = Decimal("1.30"),
    minimum_median_profit_factor: Decimal = Decimal("1.25"),
    maximum_worst_drawdown_percent: Decimal = Decimal("20"),
    minimum_pooled_win_rate_lower_bound_percent: Decimal = Decimal("45"),
    maximum_false_discovery_rate: Decimal = Decimal("0.05"),
    maximum_pbo: Decimal = Decimal("0.20"),
    cumulative_hypothesis_count: int | None = None,
) -> tuple[list[CampaignCandidateAggregate], BacktestOverfittingReport]:
    """Apply one strategy-level multiple-testing correction across markets."""

    grouped: dict[str, list[ResearchCampaignCell]] = {}
    for cell in cells:
        if cell.data_eligible:
            grouped.setdefault(cell.library_entry_id, []).append(cell)
    inputs: list[dict] = []
    pbo_cohorts: dict[tuple[str, ...], list[list[Decimal]]] = {}
    for entry_id, items in grouped.items():
        market_count = len(items)
        robust_count = sum(item.robustness_passed for item in items)
        positive_stress_count = sum(
            item.stress_return_percent is not None and item.stress_return_percent > 0
            for item in items
        )
        temporal_stable_count = sum(item.temporal_stability_passed for item in items)
        unique_symbol_count = len({item.symbol for item in items})
        unique_exchange_count = len({item.exchange for item in items})
        unique_timeframe_count = len({item.timeframe for item in items})
        high_quality_count = sum(
            item.win_rate_percent >= minimum_median_win_rate_percent
            and item.payoff_ratio >= minimum_median_payoff_ratio
            and item.profit_factor >= minimum_median_profit_factor
            and item.stress_return_percent is not None
            and item.stress_return_percent > 0
            and item.temporal_stability_passed
            and item.max_drawdown_percent <= maximum_worst_drawdown_percent
            for item in items
        )
        total_trades = sum(item.total_trades for item in items)
        total_wins = 0
        total_resolved_trades = 0
        for item in items:
            resolved = item.win_count + item.loss_count
            if resolved:
                total_wins += item.win_count
                total_resolved_trades += resolved
            else:
                total_wins += round(item.total_trades * float(item.win_rate_percent) / 100)
                total_resolved_trades += item.total_trades
        pooled_win_lower = (
            wilson_lower_bound(total_wins, total_resolved_trades) * Decimal("100")
            if total_resolved_trades
            else Decimal("0")
        )
        # Timeframes of the same symbol overlap heavily. Collapse them first
        # with a within-symbol Bonferroni correction. Crypto symbols also move
        # together, so combine the symbol p-values with the dependence-robust
        # Cauchy test instead of assuming they are independent observations.
        per_symbol_p_values: list[Decimal] = []
        for symbol in sorted({item.symbol for item in items}):
            symbol_items = [item for item in items if item.symbol == symbol]
            per_symbol_p_values.append(
                min(
                    min(item.one_sided_win_rate_p_value for item in symbol_items)
                    * Decimal(len(symbol_items)),
                    Decimal("1"),
                )
            )
        strategy_p_value = cauchy_combined_p_value(per_symbol_p_values)
        robust_ratio = Decimal(robust_count) / Decimal(market_count)
        stress_ratio = Decimal(positive_stress_count) / Decimal(market_count)
        high_quality_ratio = Decimal(high_quality_count) / Decimal(market_count)
        temporal_stable_ratio = Decimal(temporal_stable_count) / Decimal(market_count)
        inputs.append(
            {
                "library_entry_id": entry_id,
                "strategy_fingerprint": items[0].strategy_fingerprint,
                "family": items[0].family,
                "archetype": items[0].archetype,
                "market_count": market_count,
                "robust_market_count": robust_count,
                "positive_stress_market_count": positive_stress_count,
                "unique_symbol_count": unique_symbol_count,
                "unique_exchange_count": unique_exchange_count,
                "unique_timeframe_count": unique_timeframe_count,
                "high_quality_market_count": high_quality_count,
                "robust_market_ratio": robust_ratio.quantize(Decimal("0.001")),
                "positive_stress_ratio": stress_ratio.quantize(Decimal("0.001")),
                "high_quality_market_ratio": high_quality_ratio.quantize(Decimal("0.001")),
                "median_return_percent": Decimal(
                    str(median([float(item.return_percent) for item in items]))
                ).quantize(Decimal("0.01")),
                "median_profit_factor": Decimal(
                    str(median([float(item.profit_factor) for item in items]))
                ).quantize(Decimal("0.01")),
                "median_win_rate_percent": Decimal(
                    str(median([float(item.win_rate_percent) for item in items]))
                ).quantize(Decimal("0.01")),
                "median_payoff_ratio": Decimal(
                    str(median([float(item.payoff_ratio) for item in items]))
                ).quantize(Decimal("0.01")),
                "worst_drawdown_percent": max(item.max_drawdown_percent for item in items),
                "total_trades": total_trades,
                "temporal_stable_market_count": temporal_stable_count,
                "temporal_stable_market_ratio": temporal_stable_ratio.quantize(Decimal("0.001")),
                "pooled_win_rate_lower_bound_percent": pooled_win_lower.quantize(Decimal("0.01")),
                "strategy_p_value": strategy_p_value,
            }
        )
        fold_row = [
            fold_return
            for item in sorted(
                items,
                key=lambda value: (value.symbol, value.timeframe, value.dataset_id),
            )
            for fold_return in item.fold_returns
        ]
        if fold_row:
            evidence_scope = tuple(sorted(item.dataset_id for item in items))
            pbo_cohorts.setdefault(evidence_scope, []).append(fold_row)

    adjusted = benjamini_hochberg_adjusted_p_values(
        {item["library_entry_id"]: item["strategy_p_value"] for item in inputs},
        total_hypothesis_count=cumulative_hypothesis_count,
    )
    aggregates: list[CampaignCandidateAggregate] = []
    for item in inputs:
        adjusted_p = adjusted[item["library_entry_id"]]
        statistical_passed = bool(
            adjusted_p <= maximum_false_discovery_rate
            and item["pooled_win_rate_lower_bound_percent"]
            >= minimum_pooled_win_rate_lower_bound_percent
        )
        cross_market_passed = bool(
            item["market_count"] >= minimum_markets
            and item["unique_symbol_count"] >= minimum_unique_symbols
            and item["unique_exchange_count"] >= minimum_unique_exchanges
            and item["total_trades"] >= minimum_total_trades
            and item["positive_stress_ratio"] >= minimum_positive_stress_ratio
            and item["high_quality_market_ratio"] >= minimum_high_quality_market_ratio
            and item["temporal_stable_market_ratio"] >= minimum_temporal_stable_market_ratio
            and item["median_win_rate_percent"] >= minimum_median_win_rate_percent
            and item["median_payoff_ratio"] >= minimum_median_payoff_ratio
            and item["median_profit_factor"] >= minimum_median_profit_factor
            and item["worst_drawdown_percent"] <= maximum_worst_drawdown_percent
            and statistical_passed
        )
        reasons: list[str] = []
        checks = (
            (item["market_count"] >= minimum_markets, "market count", minimum_markets),
            (
                item["unique_symbol_count"] >= minimum_unique_symbols,
                "独立币种数量",
                minimum_unique_symbols,
            ),
            (
                item["unique_exchange_count"] >= minimum_unique_exchanges,
                "independent exchange count",
                minimum_unique_exchanges,
            ),
            (item["total_trades"] >= minimum_total_trades, "pooled trades", minimum_total_trades),
            (
                item["positive_stress_ratio"] >= minimum_positive_stress_ratio,
                "positive stress ratio",
                minimum_positive_stress_ratio,
            ),
            (
                item["high_quality_market_ratio"] >= minimum_high_quality_market_ratio,
                "joint quality ratio",
                minimum_high_quality_market_ratio,
            ),
            (
                item["temporal_stable_market_ratio"] >= minimum_temporal_stable_market_ratio,
                "temporal stability ratio",
                minimum_temporal_stable_market_ratio,
            ),
            (
                item["median_win_rate_percent"] >= minimum_median_win_rate_percent,
                "胜率中位数",
                minimum_median_win_rate_percent,
            ),
            (
                item["median_payoff_ratio"] >= minimum_median_payoff_ratio,
                "盈亏比中位数",
                minimum_median_payoff_ratio,
            ),
            (
                item["median_profit_factor"] >= minimum_median_profit_factor,
                "median profit factor",
                minimum_median_profit_factor,
            ),
            (
                item["worst_drawdown_percent"] <= maximum_worst_drawdown_percent,
                "worst drawdown percent",
                maximum_worst_drawdown_percent,
            ),
        )
        observed = {
            "market count": item["market_count"],
            "独立币种数量": item["unique_symbol_count"],
            "independent exchange count": item["unique_exchange_count"],
            "pooled trades": item["total_trades"],
            "positive stress ratio": item["positive_stress_ratio"],
            "joint quality ratio": item["high_quality_market_ratio"],
            "temporal stability ratio": item["temporal_stable_market_ratio"],
            "胜率中位数": item["median_win_rate_percent"],
            "盈亏比中位数": item["median_payoff_ratio"],
            "median profit factor": item["median_profit_factor"],
            "worst drawdown percent": item["worst_drawdown_percent"],
        }
        for passed, label, threshold in checks:
            if not passed:
                reasons.append(f"{label} {observed[label]} below {threshold}")
        if adjusted_p > maximum_false_discovery_rate:
            reasons.append(
                f"strategy BH adjusted p-value {adjusted_p:.4f} above "
                f"{maximum_false_discovery_rate}"
            )
        if (
            item["pooled_win_rate_lower_bound_percent"]
            < minimum_pooled_win_rate_lower_bound_percent
        ):
            reasons.append(
                "pooled win-rate Wilson lower bound "
                f"{item['pooled_win_rate_lower_bound_percent']:.2f}% below "
                f"{minimum_pooled_win_rate_lower_bound_percent}%"
            )
        significance_score = Decimal(
            str(min(max(-math.log10(max(float(adjusted_p), 1e-12)), 0), 5))
        )
        rank_score = (
            item["positive_stress_ratio"] * Decimal("15")
            + item["high_quality_market_ratio"] * Decimal("25")
            + item["temporal_stable_market_ratio"] * Decimal("10")
            + item["median_profit_factor"] * Decimal("10")
            + item["median_payoff_ratio"] * Decimal("5")
            + (item["median_win_rate_percent"] / Decimal("100")) * Decimal("15")
            + significance_score * Decimal("4")
            - item["worst_drawdown_percent"] * Decimal("0.5")
        )
        item["strategy_p_value"] = item["strategy_p_value"].quantize(Decimal("0.0001"))
        aggregates.append(
            CampaignCandidateAggregate(
                **item,
                multiple_testing_adjusted_p_value=adjusted_p.quantize(Decimal("0.0001")),
                strategy_statistical_passed=statistical_passed,
                cross_market_passed=cross_market_passed,
                rank_score=rank_score.quantize(Decimal("0.01")),
                reasons=reasons or ["strategy-level cross-market evidence passed"],
            )
        )
    aggregates.sort(key=lambda item: item.rank_score, reverse=True)
    # Strategies requiring unavailable features can legitimately have a
    # smaller eligible market scope than OHLCV-only strategies. PBO ranks must
    # remain comparable, so evaluate the largest cohort that used the exact
    # same immutable datasets instead of discarding every strategy because row
    # lengths differ across cohorts.
    comparable_rows = (
        max(
            pbo_cohorts.values(),
            key=lambda rows: (len(rows), len(rows[0]) if rows else 0),
        )
        if pbo_cohorts
        else []
    )
    equal_length_rows = (
        comparable_rows
        if comparable_rows
        and len({len(row) for row in comparable_rows}) == 1
        and len(comparable_rows[0]) % 2 == 0
        else []
    )
    pbo = estimate_probability_of_backtest_overfitting(
        equal_length_rows,
        maximum_probability=maximum_pbo,
    )
    return aggregates, pbo


def finalize_campaign(
    *,
    dataset_ids: list[str],
    blind_dataset_ids: list[str] | None = None,
    blind_set_id: str | None = None,
    blind_consumption_id: str | None = None,
    library_entry_ids: list[str],
    causal_policy_definitions: list[dict] | None = None,
    cells: list[ResearchCampaignCell],
    campaign_id: str | None = None,
    research_lineage_id_value: str | None = None,
    research_trial_family_id_value: str = "",
    research_experiment_id: str | None = None,
    research_configuration_fingerprint: str = "legacy_finalize",
    cumulative_hypothesis_count: int | None = None,
    sequential_experiment_count: int = 1,
    effective_trial_count: int | None = None,
    parent_campaign_id: str | None = None,
    predecessor_campaign_id: str | None = None,
    experiment_kind: str = "initial",
    universe_snapshot_id: str | None = None,
    universe_snapshot_fingerprint: str | None = None,
    point_in_time_selection_passed: bool = False,
    historical_survivorship_complete: bool = False,
    survivorship_bias_control_passed: bool = False,
) -> ResearchCampaignReport:
    frozen_policy_definitions = list(causal_policy_definitions or [])
    frozen_policy_definitions_fingerprint = causal_policy_definitions_fingerprint(
        frozen_policy_definitions
    )
    blind_ids = set(blind_dataset_ids or [])
    research_cells = [cell for cell in cells if cell.dataset_id not in blind_ids]
    blind_cells = [cell for cell in cells if cell.dataset_id in blind_ids]
    visible_hypothesis_count = len({cell.strategy_fingerprint for cell in research_cells})
    cumulative_count = max(cumulative_hypothesis_count or 0, visible_hypothesis_count, 1)
    effective_count = max(effective_trial_count or 0, cumulative_count, 1)
    candidates, pbo = aggregate_campaign(
        research_cells,
        cumulative_hypothesis_count=effective_count,
    )
    selected = next(
        (candidate for candidate in candidates if candidate.cross_market_passed),
        None,
    )
    blind_candidate = None
    if selected is not None and blind_cells:
        selected_blind_cells = [
            cell for cell in blind_cells if cell.library_entry_id == selected.library_entry_id
        ]
        blind_candidates, _ = aggregate_campaign(selected_blind_cells)
        blind_candidate = blind_candidates[0] if blind_candidates else None
    blind_passed = bool(blind_candidate and blind_candidate.cross_market_passed)
    eligible = bool(selected and pbo.passed and blind_passed and survivorship_bias_control_passed)
    from .strategy_portfolio import allocate_campaign_strategies

    # Only the selected strategy is evaluated on the sealed blind set. Never
    # present development-only candidates as an executable portfolio prior.
    allocations = (
        allocate_campaign_strategies([selected])
        if eligible and selected is not None
        else []
    )
    reasons: list[str] = []
    if selected is None:
        reasons.append("没有策略通过跨市场稳健性门槛")
    if not pbo.passed:
        reasons.extend(pbo.reasons)
    if not blind_ids:
        reasons.append("缺少独立密封盲测数据集，禁止签发执行资格")
    elif not blind_passed:
        reasons.append("研究集候选未通过独立盲测市场稳健性门槛")
    if not universe_snapshot_id or not point_in_time_selection_passed:
        reasons.append("缺少完整点时市场宇宙快照，无法证明研究市场不是事后挑选")
    elif not survivorship_bias_control_passed:
        reasons.append("点时选择已冻结，但历史退市资产覆盖仍未证明，禁止签发执行资格")
    if eligible and selected is not None:
        reasons.append(
            f"{selected.library_entry_id} 同时通过跨市场与 PBO 门槛，"
            "仍须绑定最新市场状态和账户风险后才可执行"
        )
    if research_lineage_id_value is None:
        encoded = json.dumps(sorted(dataset_ids), separators=(",", ":")).encode("utf-8")
        research_lineage_id_value = f"legacy_research_lineage_{sha256(encoded).hexdigest()[:20]}"
    if not research_trial_family_id_value:
        research_trial_family_id_value = (
            f"legacy_trial_family_{sha256(research_lineage_id_value.encode()).hexdigest()[:20]}"
        )
    if research_experiment_id is None:
        research_experiment_id = research_experiment_fingerprint(
            lineage_id=research_lineage_id_value,
            engine_version=CAMPAIGN_ENGINE_VERSION,
            research_configuration_fingerprint="legacy_finalize",
            strategy_fingerprints=[cell.strategy_fingerprint for cell in research_cells],
        )
    return ResearchCampaignReport(
        id=campaign_id or f"campaign_{uuid4().hex[:16]}",
        dataset_ids=dataset_ids,
        research_dataset_ids=[
            dataset_id for dataset_id in dataset_ids if dataset_id not in blind_ids
        ],
        blind_dataset_ids=list(blind_dataset_ids or []),
        blind_set_id=blind_set_id,
        blind_consumption_id=blind_consumption_id,
        library_entry_ids=library_entry_ids,
        causal_policy_definitions=frozen_policy_definitions,
        causal_policy_definitions_fingerprint=(
            frozen_policy_definitions_fingerprint
        ),
        trial_count=len(cells),
        research_lineage_id=research_lineage_id_value,
        research_trial_family_id=research_trial_family_id_value,
        research_experiment_id=research_experiment_id,
        research_configuration_fingerprint=research_configuration_fingerprint,
        cumulative_hypothesis_count=cumulative_count,
        sequential_experiment_count=max(sequential_experiment_count, 1),
        effective_trial_count=effective_count,
        parent_campaign_id=parent_campaign_id,
        predecessor_campaign_id=predecessor_campaign_id,
        experiment_kind=experiment_kind,
        universe_snapshot_id=universe_snapshot_id,
        universe_snapshot_fingerprint=universe_snapshot_fingerprint,
        point_in_time_selection_passed=point_in_time_selection_passed,
        historical_survivorship_complete=historical_survivorship_complete,
        survivorship_bias_control_passed=survivorship_bias_control_passed,
        cells=cells,
        candidates=candidates,
        pbo=pbo,
        portfolio_allocations=[allocation.model_dump(mode="json") for allocation in allocations],
        selected_library_entry_id=(selected.library_entry_id if eligible and selected else None),
        autonomous_execution_eligible=eligible,
        blind_selected_candidate_passed=blind_passed,
        blind_candidate=blind_candidate,
        engine_version=CAMPAIGN_ENGINE_VERSION,
        selection_reasons=reasons,
    )


def build_campaign_execution_certificate(
    report: ResearchCampaignReport,
    *,
    dataset_fingerprints: dict[str, str],
    issued_at: datetime | None = None,
) -> CampaignExecutionCertificate:
    selected = next(
        (
            item
            for item in report.candidates
            if item.library_entry_id == report.selected_library_entry_id
        ),
        None,
    )
    if not (
        report.engine_version >= 14
        and report.autonomous_execution_eligible
        and report.pbo.passed
        and report.blind_selected_candidate_passed
        and report.blind_set_id
        and report.blind_consumption_id
        and selected
        and selected.cross_market_passed
        and report.blind_candidate
        and report.blind_candidate.cross_market_passed
        and report.research_lineage_id
        and report.research_trial_family_id
        and report.research_experiment_id
        and report.research_configuration_fingerprint
        and report.cumulative_hypothesis_count >= 1
        and report.sequential_experiment_count >= 1
        and report.effective_trial_count >= report.cumulative_hypothesis_count
        and report.universe_snapshot_id
        and report.universe_snapshot_fingerprint
        and report.survivorship_bias_control_passed
    ):
        raise ValueError("Campaign is not eligible for an execution certificate")
    required_ids = set(report.research_dataset_ids) | set(report.blind_dataset_ids)
    if required_ids != set(dataset_fingerprints):
        raise ValueError("certificate dataset fingerprint scope is incomplete or excessive")
    selected_definition_fingerprint = selected.strategy_fingerprint
    policy_definitions_fingerprint = causal_policy_definitions_fingerprint(
        report.causal_policy_definitions
    )
    if policy_definitions_fingerprint != report.causal_policy_definitions_fingerprint:
        raise ValueError("Campaign causal policy definition set was modified")
    if (
        not report.portfolio_allocations
        or any(
            item.get("library_entry_id") != selected.library_entry_id
            for item in report.portfolio_allocations
        )
    ):
        raise ValueError(
            "Campaign risk allocation must contain only the sealed-blind selected strategy"
        )
    portfolio_allocations_fingerprint = campaign_portfolio_allocations_fingerprint(
        report.portfolio_allocations
    )
    if report.causal_policy_definitions:
        selected_policy_definition = next(
            (
                item
                for item in report.causal_policy_definitions
                if item.get("campaign_entry_id") == selected.library_entry_id
            ),
            None,
        )
        if selected_policy_definition is None:
            raise ValueError("selected causal policy definition is missing")
        if (
            validate_causal_policy_definition(selected_policy_definition)
            != selected.strategy_fingerprint
        ):
            raise ValueError("selected causal policy differs from Campaign evidence")
        selected_definition_fingerprint = sha256(
            json.dumps(
                selected_policy_definition,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
    return CampaignExecutionCertificate(
        campaign_id=report.id,
        engine_version=report.engine_version,
        selected_library_entry_id=selected.library_entry_id,
        strategy_fingerprint=selected.strategy_fingerprint,
        research_dataset_fingerprints={
            dataset_id: dataset_fingerprints[dataset_id]
            for dataset_id in report.research_dataset_ids
        },
        blind_dataset_fingerprints={
            dataset_id: dataset_fingerprints[dataset_id] for dataset_id in report.blind_dataset_ids
        },
        blind_set_id=report.blind_set_id,
        blind_consumption_id=report.blind_consumption_id,
        pbo_upper_confidence_bound=report.pbo.upper_confidence_bound,
        median_win_rate_percent=selected.median_win_rate_percent,
        median_payoff_ratio=selected.median_payoff_ratio,
        median_profit_factor=selected.median_profit_factor,
        high_quality_market_ratio=selected.high_quality_market_ratio,
        pooled_win_rate_lower_bound_percent=(selected.pooled_win_rate_lower_bound_percent),
        multiple_testing_adjusted_p_value=selected.multiple_testing_adjusted_p_value,
        research_lineage_id=report.research_lineage_id,
        research_trial_family_id=report.research_trial_family_id,
        research_experiment_id=report.research_experiment_id,
        research_configuration_fingerprint=(report.research_configuration_fingerprint),
        cumulative_hypothesis_count=report.cumulative_hypothesis_count,
        sequential_experiment_count=report.sequential_experiment_count,
        effective_trial_count=report.effective_trial_count,
        universe_snapshot_id=report.universe_snapshot_id,
        universe_snapshot_fingerprint=report.universe_snapshot_fingerprint,
        survivorship_bias_control_passed=report.survivorship_bias_control_passed,
        research_aggregate_fingerprint=campaign_candidate_fingerprint(selected),
        blind_aggregate_fingerprint=campaign_candidate_fingerprint(report.blind_candidate),
        selected_strategy_definition_fingerprint=(
            selected_definition_fingerprint
        ),
        causal_policy_definitions_fingerprint=(
            policy_definitions_fingerprint
        ),
        portfolio_allocations_fingerprint=portfolio_allocations_fingerprint,
        issued_at=issued_at or datetime.now(UTC),
    )


def validate_campaign_execution_certificate(
    report: ResearchCampaignReport,
    certificate: CampaignExecutionCertificate,
    *,
    dataset_fingerprints: dict[str, str],
) -> None:
    expected = build_campaign_execution_certificate(
        report,
        dataset_fingerprints=dataset_fingerprints,
        issued_at=certificate.issued_at,
    )
    if certificate.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise ValueError("Campaign execution certificate does not match current evidence")
