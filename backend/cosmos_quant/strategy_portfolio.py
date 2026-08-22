from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from .domain import PlanStatus, Side, StrategyPlan
from .research_campaign import CampaignCandidateAggregate
from .research_lifecycle import CandidateStatus, StrategyCandidate


class StrategyAllocation(BaseModel):
    library_entry_id: str
    family: str
    archetype: str
    weight: Decimal = Field(ge=0, le=1)
    evidence_score: Decimal
    risk_budget_role: str = "research_portfolio_prior"


class StrategyPortfolioAdmission(BaseModel):
    allowed: bool
    requested_risk_fraction: Decimal
    admitted_risk_fraction: Decimal
    total_active_risk_fraction: Decimal
    family_active_risk_fraction: Decimal
    symbol_active_risk_fraction: Decimal
    hard_conflicts: list[str] = Field(default_factory=list)
    scaling_reasons: list[str] = Field(default_factory=list)


class StrategyHealthReport(BaseModel):
    candidate_id: str
    status: str
    evidence_failures: int
    latest_robustness_passed: bool
    latest_stress_positive: bool
    latest_temporal_stability_passed: bool
    campaign_cross_market_passed: bool | None = None
    retirement_recommended: bool
    reasons: list[str] = Field(default_factory=list)


def allocate_campaign_strategies(
    candidates: list[CampaignCandidateAggregate],
    *,
    max_strategies: int = 6,
    max_family_weight: Decimal = Decimal("0.50"),
    max_archetype_weight: Decimal = Decimal("0.35"),
) -> list[StrategyAllocation]:
    eligible = [candidate for candidate in candidates if candidate.cross_market_passed][
        :max_strategies
    ]
    if not eligible:
        return []
    raw_scores = {
        candidate.library_entry_id: max(
            candidate.rank_score,
            Decimal("0.01"),
        )
        / max(candidate.worst_drawdown_percent, Decimal("1"))
        for candidate in eligible
    }
    total = sum(raw_scores.values(), Decimal("0"))
    allocations: list[StrategyAllocation] = []
    family_used: dict[str, Decimal] = {}
    archetype_used: dict[str, Decimal] = {}
    for candidate in eligible:
        raw_weight = raw_scores[candidate.library_entry_id] / total if total else Decimal("0")
        family_room = max_family_weight - family_used.get(
            candidate.family,
            Decimal("0"),
        )
        archetype_room = max_archetype_weight - archetype_used.get(
            candidate.archetype,
            Decimal("0"),
        )
        weight = max(
            Decimal("0"),
            min(raw_weight, family_room, archetype_room),
        )
        if weight <= 0:
            continue
        family_used[candidate.family] = family_used.get(candidate.family, Decimal("0")) + weight
        archetype_used[candidate.archetype] = (
            archetype_used.get(candidate.archetype, Decimal("0")) + weight
        )
        allocations.append(
            StrategyAllocation(
                library_entry_id=candidate.library_entry_id,
                family=candidate.family,
                archetype=candidate.archetype,
                weight=weight.quantize(Decimal("0.0001")),
                evidence_score=candidate.rank_score,
            )
        )
    allocated = sum(
        (allocation.weight for allocation in allocations),
        Decimal("0"),
    )
    if allocated > 0:
        # Keep caps authoritative: unallocated weight is an intentional cash
        # reserve rather than renormalizing concentrated strategies to 100%.
        for allocation in allocations:
            allocation.risk_budget_role = (
                "allocated_with_cash_reserve" if allocated < Decimal("0.999") else "fully_allocated"
            )
    return allocations


def admit_strategy_risk(
    *,
    family: str,
    symbol: str,
    side: Side,
    strategy_fingerprint: str,
    requested_risk_fraction: Decimal,
    active_plans: list[StrategyPlan],
    maximum_portfolio_risk: Decimal,
    maximum_family_share: Decimal = Decimal("0.50"),
    maximum_symbol_share: Decimal = Decimal("0.40"),
) -> StrategyPortfolioAdmission:
    relevant = [
        plan for plan in active_plans if plan.status in {PlanStatus.ACTIVE, PlanStatus.PAUSED}
    ]
    total_active = sum(
        (plan.sizing.risk_fraction for plan in relevant),
        Decimal("0"),
    )
    family_active = sum(
        (plan.sizing.risk_fraction for plan in relevant if plan.algorithm == family),
        Decimal("0"),
    )
    symbol_active = sum(
        (plan.sizing.risk_fraction for plan in relevant if plan.symbol == symbol),
        Decimal("0"),
    )
    hard_conflicts: list[str] = []
    if any(plan.symbol == symbol and plan.side != side for plan in relevant):
        hard_conflicts.append("同一品种已有反方向策略风险")
    if any(
        plan.strategy_spec_fingerprint
        and plan.strategy_spec_fingerprint == strategy_fingerprint
        and plan.symbol == symbol
        for plan in relevant
    ):
        hard_conflicts.append("同一品种已有相同策略规格风险")
    family_cap = maximum_portfolio_risk * maximum_family_share
    symbol_cap = maximum_portfolio_risk * maximum_symbol_share
    admitted = min(
        requested_risk_fraction,
        max(maximum_portfolio_risk - total_active, Decimal("0")),
        max(family_cap - family_active, Decimal("0")),
        max(symbol_cap - symbol_active, Decimal("0")),
    )
    reasons: list[str] = []
    if admitted < requested_risk_fraction:
        reasons.append("按组合、策略家族和品种剩余风险预算缩小仓位")
    if hard_conflicts:
        admitted = Decimal("0")
    return StrategyPortfolioAdmission(
        allowed=bool(admitted > 0 and not hard_conflicts),
        requested_risk_fraction=requested_risk_fraction,
        admitted_risk_fraction=admitted,
        total_active_risk_fraction=total_active,
        family_active_risk_fraction=family_active,
        symbol_active_risk_fraction=symbol_active,
        hard_conflicts=hard_conflicts,
        scaling_reasons=reasons,
    )


def assess_strategy_health(
    candidate: StrategyCandidate,
    *,
    campaign_candidate: CampaignCandidateAggregate | None = None,
) -> StrategyHealthReport:
    result = candidate.backtest
    failures = 0
    reasons: list[str] = []
    robustness = bool(result and result.robustness_passed)
    stress_positive = bool(
        result and result.stress_return_percent is not None and result.stress_return_percent > 0
    )
    temporal = bool(result and result.temporal_stability and result.temporal_stability.passed)
    for passed, reason in (
        (robustness, "最新不可变数据集稳健性失效"),
        (stress_positive, "成本压力收益不为正"),
        (temporal, "时间分段稳定性失效"),
    ):
        if not passed:
            failures += 1
            reasons.append(reason)
    campaign_passed = (
        campaign_candidate.cross_market_passed if campaign_candidate is not None else None
    )
    if campaign_passed is False:
        failures += 1
        reasons.append("最新 Campaign 跨市场证据失效")
    retirement = bool(candidate.status == CandidateStatus.CHAMPION and failures >= 2)
    status = (
        "retire"
        if retirement
        else "decaying"
        if failures >= 2
        else "watch"
        if failures == 1
        else "healthy"
    )
    return StrategyHealthReport(
        candidate_id=candidate.id,
        status=status,
        evidence_failures=failures,
        latest_robustness_passed=robustness,
        latest_stress_positive=stress_positive,
        latest_temporal_stability_passed=temporal,
        campaign_cross_market_passed=campaign_passed,
        retirement_recommended=retirement,
        reasons=reasons or ["最新独立证据未发现策略衰退"],
    )
