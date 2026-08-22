from __future__ import annotations

from decimal import Decimal

from ..backtest import BacktestResult


def stratified_actionable_candidates(
    candidates: list[dict],
    *,
    max_total: int = 3,
    max_per_family: int = 3,
) -> tuple[list[dict], dict]:
    """Keep candidate screening broad without weakening execution gates.

    The market analyzer already orders candidates by current evidence. A
    global slice can nevertheless let parameter variants from one strategy
    family consume every screening slot. This bounded two-pass selection
    first preserves the best candidate from each represented family, then
    fills remaining capacity in the analyzer's original order.
    """

    if max_total <= 0 or max_per_family <= 0:
        return [], {
            "input_count": len(candidates),
            "unique_count": 0,
            "selected_count": 0,
            "duplicate_count": 0,
            "input_family_counts": {},
            "selected_family_counts": {},
            "max_total": max_total,
            "max_per_family": max_per_family,
            "selection_role": "screening_breadth_not_execution_gate",
        }

    unique: list[dict] = []
    seen: set[tuple[str, str]] = set()
    input_family_counts: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        family = str(candidate.get("strategy_kind") or "unknown")
        input_family_counts[family] = input_family_counts.get(family, 0) + 1
        fingerprint = str(candidate.get("spec_fingerprint") or "")
        identity = (
            family,
            fingerprint
            or "|".join(
                (
                    str(candidate.get("adaptive_label") or ""),
                    str(candidate.get("direction") or ""),
                    str(index),
                )
            ),
        )
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(candidate)

    selected: list[dict] = []
    selected_ids: set[int] = set()
    selected_family_counts: dict[str, int] = {}

    # First pass: retain the analyzer's best candidate for every family.
    for candidate in unique:
        family = str(candidate.get("strategy_kind") or "unknown")
        if family in selected_family_counts:
            continue
        selected.append(candidate)
        selected_ids.add(id(candidate))
        selected_family_counts[family] = 1
        if len(selected) >= max_total:
            break

    # Second pass: use spare capacity for the remaining highest-ranked
    # parameter variants, while bounding concentration in any one family.
    if len(selected) < max_total:
        for candidate in unique:
            if id(candidate) in selected_ids:
                continue
            family = str(candidate.get("strategy_kind") or "unknown")
            if selected_family_counts.get(family, 0) >= max_per_family:
                continue
            selected.append(candidate)
            selected_ids.add(id(candidate))
            selected_family_counts[family] = selected_family_counts.get(family, 0) + 1
            if len(selected) >= max_total:
                break

    return selected, {
        "input_count": len(candidates),
        "unique_count": len(unique),
        "selected_count": len(selected),
        "duplicate_count": len(candidates) - len(unique),
        "input_family_counts": input_family_counts,
        "selected_family_counts": selected_family_counts,
        "max_total": max_total,
        "max_per_family": max_per_family,
        "selection_role": "screening_breadth_not_execution_gate",
    }


def _unit(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(value, Decimal("1")))


def _scaled(value: Decimal, span: Decimal) -> Decimal:
    return _unit(value / span) if span > 0 else Decimal("0")


def evidence_quality_score(result: BacktestResult) -> dict:
    """Return a transparent 0–100 evidence score used only for ranking.

    This is deliberately not a probability or an additional execution gate.
    It summarizes independent full-sample, cost-stress, recent-window,
    drawdown, and sample-size evidence so equally valid candidates can be
    compared without pretending that one metric predicts future wins.
    """

    standard = result.trade_sample_standard or 100
    sample = Decimal("10") * _unit(Decimal(result.total_trades) / Decimal(standard))
    full_profit_factor = Decimal("15") * _scaled(
        result.profit_factor - Decimal("1"),
        Decimal("0.75"),
    )
    win_loss_quality = Decimal("5") * _scaled(
        result.win_rate_percent - Decimal("40"), Decimal("20")
    ) + Decimal("5") * _scaled(result.payoff_ratio - Decimal("1"), Decimal("1"))
    full_return = Decimal("5") * _scaled(
        result.return_percent,
        Decimal("5"),
    )
    stress_return = result.stress_return_percent or Decimal("0")
    cost_resilience = (
        Decimal("10") + Decimal("10") * _scaled(stress_return, Decimal("2"))
        if stress_return > 0
        else Decimal("0")
    )
    recent = result.recent_window
    recent_edge = Decimal("0")
    if recent and recent.return_percent > 0 and recent.profit_factor >= 1:
        recent_edge = (
            Decimal("8")
            + Decimal("8") * _scaled(recent.profit_factor - Decimal("1"), Decimal("0.75"))
            + Decimal("4") * _scaled(recent.return_percent, Decimal("2"))
        )
    temporal = result.temporal_stability
    temporal_stability = (
        Decimal("10") * _unit(temporal.positive_fold_ratio)
        if temporal and temporal.evaluable_folds >= 3
        else Decimal("0")
    )
    drawdown_control = Decimal("5") * (
        Decimal("1") - _scaled(result.max_drawdown_percent, Decimal("20"))
    )
    robustness = Decimal("15") if result.robustness_passed else Decimal("0")
    components = {
        "robustness": robustness,
        "sample": sample,
        "full_profit_factor": full_profit_factor,
        "win_loss_quality": win_loss_quality,
        "full_return": full_return,
        "cost_resilience": cost_resilience,
        "recent_edge": recent_edge,
        "temporal_stability": temporal_stability,
        "drawdown_control": drawdown_control,
    }
    total = min(sum(components.values(), Decimal("0")), Decimal("100"))
    return {
        "total": total.quantize(Decimal("0.1")),
        "components": {key: value.quantize(Decimal("0.1")) for key, value in components.items()},
        "role": "ranking_only",
        "is_probability": False,
    }


def candidate_selection_score(
    result: BacktestResult,
    *,
    signal_confidence: Decimal = Decimal("0"),
    regime_score: Decimal = Decimal("0"),
    forward_expectancy_r: Decimal | None = None,
    forward_sample: int = 0,
) -> dict:
    """Blend tested edge, current market fit, and mature forward outcomes."""

    evidence = Decimal(evidence_quality_score(result)["total"])
    signal = _unit(signal_confidence) * Decimal("100")
    regime = _unit(regime_score) * Decimal("100")
    forward_confidence = (
        _unit(Decimal(forward_sample) / Decimal("10"))
        if forward_sample >= 5 and forward_expectancy_r is not None
        else Decimal("0")
    )
    forward_weight = Decimal("0.10") * forward_confidence
    base_weight = Decimal("1") - forward_weight
    evidence_weight = Decimal("0.70") * base_weight
    signal_weight = Decimal("0.20") * base_weight
    regime_weight = Decimal("0.10") * base_weight
    forward_score = (
        _unit((forward_expectancy_r + Decimal("0.25")) / Decimal("0.50")) * Decimal("100")
        if forward_weight > 0 and forward_expectancy_r is not None
        else Decimal("0")
    )
    total = (
        evidence * evidence_weight
        + signal * signal_weight
        + regime * regime_weight
        + forward_score * forward_weight
    )
    return {
        "total": total.quantize(Decimal("0.1")),
        "evidence_weight": evidence_weight.quantize(Decimal("0.001")),
        "signal_weight": signal_weight.quantize(Decimal("0.001")),
        "regime_weight": regime_weight.quantize(Decimal("0.001")),
        "forward_weight": forward_weight.quantize(Decimal("0.001")),
        "signal_confidence": signal_confidence,
        "regime_score": regime_score,
        "forward_expectancy_r": forward_expectancy_r,
        "forward_sample": forward_sample,
        "forward_score": forward_score.quantize(Decimal("0.1")),
        "role": "ranking_only",
        "is_probability": False,
    }


def evidence_rank_key(
    result: BacktestResult,
    *,
    signal_confidence: Decimal = Decimal("0"),
    regime_score: Decimal = Decimal("0"),
    forward_expectancy_r: Decimal | None = None,
    forward_sample: int = 0,
) -> tuple:
    """Rank already-tested plans without weakening the execution gate.

    Robustness remains the first and non-negotiable dimension. The remaining
    dimensions prefer evidence that survives costs and is still present in the
    latest quarter of the immutable dataset.
    """

    recent = result.recent_window
    recent_positive = bool(
        recent and recent.return_percent > 0 and recent.profit_factor >= Decimal("1")
    )
    selection_score = candidate_selection_score(
        result,
        signal_confidence=signal_confidence,
        regime_score=regime_score,
        forward_expectancy_r=forward_expectancy_r,
        forward_sample=forward_sample,
    )
    evidence_score = evidence_quality_score(result)
    return (
        int(result.robustness_passed),
        int(recent_positive),
        selection_score["total"],
        evidence_score["total"],
        result.stress_return_percent
        if result.stress_return_percent is not None
        else Decimal("-999"),
        recent.profit_factor if recent else Decimal("-999"),
        recent.return_percent if recent else Decimal("-999"),
        result.profit_factor,
        result.return_percent,
        -result.max_drawdown_percent,
        result.total_trades,
    )


def evidence_summary(result: BacktestResult) -> dict:
    recent = result.recent_window
    return {
        "robustness_passed": result.robustness_passed,
        "return_percent": result.return_percent,
        "stress_return_percent": result.stress_return_percent,
        "profit_factor": result.profit_factor,
        "max_drawdown_percent": result.max_drawdown_percent,
        "win_rate_percent": result.win_rate_percent,
        "average_win": result.average_win,
        "average_loss": result.average_loss,
        "payoff_ratio": result.payoff_ratio,
        "win_loss_quality_passed": result.win_loss_quality_passed,
        "total_trades": result.total_trades,
        "sample_bars": result.sample_bars,
        "sample_years": result.sample_years,
        "trade_sample_standard": result.trade_sample_standard,
        "trade_sample_floor": result.trade_sample_floor,
        "trade_sample_strong_floor": result.trade_sample_strong_floor,
        "trade_sample_corroborated": result.trade_sample_corroborated,
        "trade_sample_strongly_corroborated": (result.trade_sample_strongly_corroborated),
        "trade_sample_tier": result.trade_sample_tier,
        "temporal_stability": (
            result.temporal_stability.model_dump(mode="json") if result.temporal_stability else None
        ),
        "parameter_walk_forward": result.parameter_walk_forward,
        "evidence_score": evidence_quality_score(result),
        "recent_window": (
            {
                "return_percent": recent.return_percent,
                "profit_factor": recent.profit_factor,
                "max_drawdown_percent": recent.max_drawdown_percent,
                "win_rate_percent": recent.win_rate_percent,
                "total_trades": recent.total_trades,
            }
            if recent
            else None
        ),
        "validation_reasons": result.validation_reasons,
        "dataset_id": result.dataset_id,
        "strategy_fingerprint": result.strategy_fingerprint,
        "data_fingerprint": result.data_fingerprint,
    }
