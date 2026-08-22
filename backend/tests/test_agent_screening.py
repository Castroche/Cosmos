from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.agent.screening import (
    candidate_selection_score,
    evidence_quality_score,
    evidence_rank_key,
    evidence_summary,
    stratified_actionable_candidates,
)
from cosmos_quant.backtest import BacktestResult, BacktestWindowSummary


def _result(
    *,
    robust: bool,
    return_percent: str,
    stress_return: str,
    profit_factor: str,
    drawdown: str,
    recent_return: str,
    recent_profit_factor: str,
) -> BacktestResult:
    now = datetime.now(UTC)
    return BacktestResult(
        initial_equity=Decimal("10000"),
        final_equity=Decimal("10100"),
        net_profit=Decimal("100"),
        return_percent=Decimal(return_percent),
        max_drawdown_percent=Decimal(drawdown),
        win_rate_percent=Decimal("52"),
        profit_factor=Decimal(profit_factor),
        sharpe=Decimal("1"),
        total_trades=120,
        trades=[],
        equity_curve=[Decimal("10000"), Decimal("10100")],
        sample_bars=2000,
        sample_years=Decimal("1"),
        stress_return_percent=Decimal(stress_return),
        robustness_passed=robust,
        validation_reasons=["passed" if robust else "failed"],
        trade_sample_standard=100,
        trade_sample_floor=80,
        trade_sample_corroborated=True,
        recent_window=BacktestWindowSummary(
            label="latest_25_percent",
            sample_bars=500,
            data_start=now - timedelta(days=30),
            data_end=now,
            net_profit=Decimal("50"),
            return_percent=Decimal(recent_return),
            max_drawdown_percent=Decimal("2"),
            win_rate_percent=Decimal("51"),
            profit_factor=Decimal(recent_profit_factor),
            total_trades=30,
        ),
    )


def test_robust_candidate_always_ranks_above_non_robust_candidate():
    robust = _result(
        robust=True,
        return_percent="2",
        stress_return="0.2",
        profit_factor="1.2",
        drawdown="5",
        recent_return="0.2",
        recent_profit_factor="1.1",
    )
    overfit = _result(
        robust=False,
        return_percent="20",
        stress_return="-5",
        profit_factor="3",
        drawdown="1",
        recent_return="-2",
        recent_profit_factor="0.5",
    )

    assert evidence_rank_key(robust) > evidence_rank_key(overfit)


def test_recent_and_stress_evidence_break_ties_between_robust_candidates():
    current = _result(
        robust=True,
        return_percent="3",
        stress_return="1",
        profit_factor="1.3",
        drawdown="4",
        recent_return="1",
        recent_profit_factor="1.4",
    )
    stale = _result(
        robust=True,
        return_percent="8",
        stress_return="0.1",
        profit_factor="1.8",
        drawdown="2",
        recent_return="-1",
        recent_profit_factor="0.8",
    )

    assert evidence_rank_key(current) > evidence_rank_key(stale)
    summary = evidence_summary(current)
    assert summary["recent_window"]["profit_factor"] == Decimal("1.4")
    assert summary["stress_return_percent"] == Decimal("1")
    assert summary["trade_sample_standard"] == 100
    assert summary["trade_sample_floor"] == 80
    assert summary["trade_sample_corroborated"] is True


def test_evidence_score_is_transparent_ranking_not_win_probability():
    result = _result(
        robust=True,
        return_percent="3",
        stress_return="1",
        profit_factor="1.3",
        drawdown="4",
        recent_return="1",
        recent_profit_factor="1.4",
    )

    evidence = evidence_quality_score(result)
    selection = candidate_selection_score(
        result,
        signal_confidence=Decimal("0.8"),
        regime_score=Decimal("0.7"),
    )

    assert Decimal("0") <= evidence["total"] <= Decimal("100")
    assert evidence["components"]["recent_edge"] > 0
    assert evidence["role"] == "ranking_only"
    assert evidence["is_probability"] is False
    assert Decimal("0") <= selection["total"] <= Decimal("100")
    assert selection["signal_confidence"] == Decimal("0.8")
    assert selection["is_probability"] is False


def test_current_market_fit_breaks_equal_backtest_tie():
    result = _result(
        robust=True,
        return_percent="3",
        stress_return="1",
        profit_factor="1.3",
        drawdown="4",
        recent_return="1",
        recent_profit_factor="1.4",
    )

    aligned = evidence_rank_key(
        result,
        signal_confidence=Decimal("0.85"),
        regime_score=Decimal("0.80"),
    )
    weak_fit = evidence_rank_key(
        result,
        signal_confidence=Decimal("0.45"),
        regime_score=Decimal("0.40"),
    )

    assert aligned > weak_fit


def test_mature_forward_outcomes_break_equal_candidate_tie_without_becoming_gate():
    result = _result(
        robust=True,
        return_percent="3",
        stress_return="1",
        profit_factor="1.3",
        drawdown="4",
        recent_return="1",
        recent_profit_factor="1.4",
    )

    positive = evidence_rank_key(
        result,
        signal_confidence=Decimal("0.7"),
        regime_score=Decimal("0.7"),
        forward_expectancy_r=Decimal("0.20"),
        forward_sample=10,
    )
    negative = evidence_rank_key(
        result,
        signal_confidence=Decimal("0.7"),
        regime_score=Decimal("0.7"),
        forward_expectancy_r=Decimal("-0.20"),
        forward_sample=10,
    )
    immature = candidate_selection_score(
        result,
        signal_confidence=Decimal("0.7"),
        regime_score=Decimal("0.7"),
        forward_expectancy_r=Decimal("-1"),
        forward_sample=4,
    )

    assert positive > negative
    assert immature["forward_weight"] == Decimal("0.000")
    assert immature["forward_score"] == Decimal("0.0")


def test_candidate_screening_preserves_one_candidate_per_strategy_family():
    candidates = [
        {
            "strategy_kind": "trend",
            "spec_fingerprint": "trend-a",
        },
        {
            "strategy_kind": "trend",
            "spec_fingerprint": "trend-b",
        },
        {
            "strategy_kind": "trend",
            "spec_fingerprint": "trend-c",
        },
        {
            "strategy_kind": "mean_reversion",
            "spec_fingerprint": "mean-a",
        },
        {
            "strategy_kind": "breakout",
            "spec_fingerprint": "breakout-a",
        },
    ]

    selected, summary = stratified_actionable_candidates(
        candidates,
        max_total=3,
        max_per_family=3,
    )

    assert [candidate["strategy_kind"] for candidate in selected] == [
        "trend",
        "mean_reversion",
        "breakout",
    ]
    assert summary["input_count"] == 5
    assert summary["selected_count"] == 3
    assert summary["selected_family_counts"] == {
        "trend": 1,
        "mean_reversion": 1,
        "breakout": 1,
    }
    assert summary["selection_role"] == ("screening_breadth_not_execution_gate")


def test_candidate_screening_uses_spare_slots_for_ranked_family_variants():
    duplicate = {
        "strategy_kind": "trend",
        "spec_fingerprint": "trend-a",
    }
    candidates = [
        duplicate,
        duplicate.copy(),
        {
            "strategy_kind": "trend",
            "spec_fingerprint": "trend-b",
        },
        {
            "strategy_kind": "mean_reversion",
            "spec_fingerprint": "mean-a",
        },
        {
            "strategy_kind": "trend",
            "spec_fingerprint": "trend-c",
        },
    ]

    selected, summary = stratified_actionable_candidates(
        candidates,
        max_total=3,
        max_per_family=2,
    )

    assert [candidate["spec_fingerprint"] for candidate in selected] == [
        "trend-a",
        "mean-a",
        "trend-b",
    ]
    assert summary["duplicate_count"] == 1
    assert summary["selected_family_counts"] == {
        "trend": 2,
        "mean_reversion": 1,
    }


def test_candidate_screening_retains_cost_resilient_family_variant():
    candidates = [
        {
            "strategy_kind": "trend",
            "adaptive_label": label,
            "spec_fingerprint": f"trend-{label}",
        }
        for label in (
            "baseline",
            "selective",
            "responsive",
            "cost_resilient",
        )
    ]

    selected, summary = stratified_actionable_candidates(
        candidates,
        max_total=9,
        max_per_family=4,
    )

    assert [candidate["adaptive_label"] for candidate in selected] == [
        "baseline",
        "selective",
        "responsive",
        "cost_resilient",
    ]
    assert summary["selected_count"] == 4
    assert summary["selected_family_counts"] == {"trend": 4}
    assert summary["selection_role"] == ("screening_breadth_not_execution_gate")
