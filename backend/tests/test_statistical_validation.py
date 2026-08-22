from decimal import Decimal

from cosmos_quant.statistical_validation import (
    benjamini_hochberg_adjusted_p_values,
    binomial_upper_tail_p_value,
    cauchy_combined_p_value,
    estimate_probability_of_backtest_overfitting,
    stouffer_combined_p_value,
    validate_statistical_evidence,
    wilson_lower_bound,
)


def test_strategy_level_stouffer_combination_rewards_consistent_markets():
    consistent = stouffer_combined_p_value([Decimal("0.02"), Decimal("0.03"), Decimal("0.01")])
    conflicted = stouffer_combined_p_value([Decimal("0.02"), Decimal("0.80"), Decimal("0.90")])

    assert consistent < Decimal("0.001")
    assert conflicted > Decimal("0.10")


def test_strategy_level_cauchy_combination_handles_correlated_markets():
    consistent = cauchy_combined_p_value(
        [Decimal("0.001"), Decimal("0.002"), Decimal("0.003")]
    )
    one_signal = cauchy_combined_p_value(
        [Decimal("0.001"), Decimal("0.70"), Decimal("0.80")]
    )

    assert consistent < Decimal("0.003")
    assert Decimal("0.002") < one_signal < Decimal("0.01")
    assert cauchy_combined_p_value([]) == Decimal("1")


def test_exact_binomial_upper_tail_uses_resolved_trade_outcomes():
    assert binomial_upper_tail_p_value(8, 10).quantize(Decimal("0.0000001")) == Decimal("0.0546875")
    assert binomial_upper_tail_p_value(10, 10).quantize(Decimal("0.0000001")) == Decimal(
        "0.0009766"
    )
    assert binomial_upper_tail_p_value(0, 0) == Decimal("1")


def test_bh_adjustment_is_monotone_across_frozen_hypotheses():
    adjusted = benjamini_hochberg_adjusted_p_values(
        {
            "strong": Decimal("0.001"),
            "medium": Decimal("0.010"),
            "weak": Decimal("0.200"),
        }
    )

    assert adjusted["strong"] == Decimal("0.003")
    assert adjusted["medium"] == Decimal("0.015")
    assert adjusted["weak"] == Decimal("0.200")


def test_bh_adjustment_charges_hypotheses_from_prior_campaigns():
    adjusted = benjamini_hochberg_adjusted_p_values(
        {"current_winner": Decimal("0.001")},
        total_hypothesis_count=100,
    )

    assert adjusted["current_winner"] == Decimal("0.100")


def test_wilson_bound_penalizes_small_apparent_win_rate():
    small = wilson_lower_bound(7, 10)
    large = wilson_lower_bound(700, 1000)

    assert small < large < Decimal("0.70")


def test_statistical_validation_records_multiple_testing_and_path_risk():
    pnls = [Decimal("2")] * 70 + [Decimal("-1")] * 30
    report = validate_statistical_evidence(
        annualized_sharpe=Decimal("2.5"),
        annualization_periods=Decimal("365"),
        observation_count=3000,
        pnls=pnls,
        initial_equity=Decimal("100"),
        trial_count=30,
    )

    assert report.trial_count == 30
    assert report.win_rate_lower_bound_percent < Decimal("70")
    assert report.bootstrap_payoff_lower_bound > Decimal("1")
    assert report.monte_carlo_drawdown_95_percent > 0


def test_pbo_detects_candidate_that_wins_in_sample_and_loses_out_of_sample():
    report = estimate_probability_of_backtest_overfitting(
        [
            [Decimal("8"), Decimal("8"), Decimal("-8"), Decimal("-8")],
            [Decimal("-8"), Decimal("-8"), Decimal("8"), Decimal("8")],
            [Decimal("1"), Decimal("1"), Decimal("1"), Decimal("1")],
        ]
    )

    assert report.split_count > 0
    assert report.probability_of_backtest_overfitting > 0


def test_large_pbo_uses_deterministic_bounded_sampling_and_upper_bound():
    rows = [
        [Decimal(str((candidate + fold) % 7 - 3)) for fold in range(24)] for candidate in range(8)
    ]

    first = estimate_probability_of_backtest_overfitting(
        rows,
        maximum_splits=200,
    )
    second = estimate_probability_of_backtest_overfitting(
        rows,
        maximum_splits=200,
    )

    assert first.sampling_mode == "deterministic_sample"
    assert first.split_count == 200
    assert first.total_possible_splits > first.split_count
    assert first.probability_of_backtest_overfitting == (second.probability_of_backtest_overfitting)
    assert first.upper_confidence_bound >= first.probability_of_backtest_overfitting
