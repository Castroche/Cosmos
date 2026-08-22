from decimal import Decimal

from cosmos_quant.backtest import BacktestResult
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.research_campaign import (
    CampaignCandidateAggregate,
    ResearchCampaignCell,
)
from cosmos_quant.research_hypothesis import (
    generate_campaign_failure_revisions,
    generate_failure_driven_revisions,
)
from cosmos_quant.research_lifecycle import (
    attach_backtest,
    create_candidate,
)
from cosmos_quant.strategy_library import library_entry
from cosmos_quant.strategy_spec import default_strategy_spec


def test_failure_attribution_creates_new_unvalidated_child_hypotheses():
    candidate = create_candidate(default_strategy_spec(StrategyKind.TREND))
    failed = BacktestResult(
        initial_equity=Decimal("100"),
        final_equity=Decimal("90"),
        net_profit=Decimal("-10"),
        return_percent=Decimal("-10"),
        max_drawdown_percent=Decimal("15"),
        win_rate_percent=Decimal("35"),
        profit_factor=Decimal("0.8"),
        sharpe=Decimal("-1"),
        total_trades=40,
        trades=[],
        equity_curve=[Decimal("100"), Decimal("90")],
        payoff_ratio=Decimal("0.9"),
        stress_return_percent=Decimal("-12"),
        trade_sample_floor=80,
    )
    candidate = attach_backtest(candidate, "ds_failed", failed)

    attribution, revisions = generate_failure_driven_revisions(candidate)

    assert "low_win_rate" in attribution.codes
    assert "low_payoff" in attribution.codes
    assert "cost_fragile" in attribution.codes
    assert revisions
    assert all(item.candidate.parent_candidate_id == candidate.id for item in revisions)
    assert all(item.candidate.source == "ai" for item in revisions)
    assert all(item.candidate.backtest is None for item in revisions)
    assert len({item.candidate.spec_fingerprint for item in revisions}) == len(revisions)


def test_trend_low_win_rate_revision_prioritizes_signal_quality_over_early_profit():
    candidate = create_candidate(default_strategy_spec(StrategyKind.TREND))
    failed = BacktestResult(
        initial_equity=Decimal("100"),
        final_equity=Decimal("103"),
        net_profit=Decimal("3"),
        return_percent=Decimal("3"),
        max_drawdown_percent=Decimal("10"),
        win_rate_percent=Decimal("40"),
        profit_factor=Decimal("1.10"),
        sharpe=Decimal("0.2"),
        total_trades=300,
        trades=[],
        equity_curve=[Decimal("100"), Decimal("103")],
        payoff_ratio=Decimal("1.80"),
        stress_return_percent=Decimal("-1"),
        trade_sample_floor=80,
    )
    candidate = attach_backtest(candidate, "ds_failed_trend", failed)

    _, revisions = generate_failure_driven_revisions(candidate, max_revisions=1)

    assert [item.label for item in revisions] == ["提高联合信号质量"]
    assert "minimum_efficiency_ratio" in revisions[0].changed_parameters
    assert "first_target_reward_risk" not in revisions[0].changed_parameters


def test_campaign_revision_changes_parameters_used_by_the_entry_model():
    entry = library_entry("builtin_stochastic_21_v2")
    candidate = create_candidate(entry.validated_spec)
    aggregate = CampaignCandidateAggregate(
        library_entry_id=entry.id,
        strategy_fingerprint=entry.validated_spec.fingerprint,
        family="mean_reversion",
        archetype=entry.archetype,
        market_count=3,
        robust_market_count=0,
        positive_stress_market_count=3,
        robust_market_ratio=Decimal("0"),
        positive_stress_ratio=Decimal("1"),
        median_return_percent=Decimal("3"),
        median_profit_factor=Decimal("1.2"),
        median_win_rate_percent=Decimal("39"),
        median_payoff_ratio=Decimal("2"),
        worst_drawdown_percent=Decimal("10"),
        cross_market_passed=False,
        rank_score=Decimal("10"),
    )
    cells = [
        ResearchCampaignCell(
            library_entry_id=entry.id,
            strategy_fingerprint=entry.validated_spec.fingerprint,
            family="mean_reversion",
            archetype=entry.archetype,
            dataset_id=f"dataset_{index}",
            symbol=f"symbol_{index}",
            timeframe="1h",
            total_trades=100,
            return_percent=Decimal("3"),
            stress_return_percent=Decimal("1"),
            profit_factor=Decimal("1.2"),
            win_rate_percent=Decimal("39"),
            payoff_ratio=Decimal("2"),
            max_drawdown_percent=Decimal("10"),
            robustness_passed=False,
            statistical_uncertainty_passed=False,
            temporal_stability_passed=False,
            fold_returns=[Decimal("1")] * 4,
            backtest_fingerprint=f"bt_{index}",
        )
        for index in range(3)
    ]

    attribution, revisions = generate_campaign_failure_revisions(
        candidate,
        aggregate,
        cells,
    )

    selective = next(item for item in revisions if item.label == "提高联合信号质量")
    time_stop = next(item for item in revisions if item.label == "限制均值回归持仓时限")
    assert "low_win_rate" in attribution.codes
    assert time_stop.changed_parameters == {"maximum_holding_bars": 40}
    assert selective.changed_parameters == {
        "oscillator_oversold": "15",
        "oscillator_overbought": "85",
    }
    assert "zscore_entry" not in selective.changed_parameters
    assert selective.candidate.backtest is None


def test_bollinger_reentry_quality_revision_changes_the_live_entry_dimension():
    entry = library_entry("builtin_bollinger_50_reentry_v1")
    candidate = create_candidate(entry.validated_spec)
    aggregate = CampaignCandidateAggregate(
        library_entry_id=entry.id,
        strategy_fingerprint=entry.validated_spec.fingerprint,
        family="mean_reversion",
        archetype=entry.archetype,
        market_count=18,
        robust_market_count=0,
        positive_stress_market_count=15,
        robust_market_ratio=Decimal("0"),
        positive_stress_ratio=Decimal("0.833"),
        median_return_percent=Decimal("1.86"),
        median_profit_factor=Decimal("1.64"),
        median_win_rate_percent=Decimal("48.08"),
        median_payoff_ratio=Decimal("1.74"),
        worst_drawdown_percent=Decimal("2.80"),
        cross_market_passed=False,
        rank_score=Decimal("43.97"),
    )
    cells = [
        ResearchCampaignCell(
            library_entry_id=entry.id,
            strategy_fingerprint=entry.validated_spec.fingerprint,
            family="mean_reversion",
            archetype=entry.archetype,
            dataset_id=f"dataset_{index}",
            exchange=("binance", "bybit", "okx")[index % 3],
            symbol=f"symbol_{index % 6}",
            timeframe="1h",
            total_trades=9,
            return_percent=Decimal("1.86"),
            stress_return_percent=Decimal("1"),
            profit_factor=Decimal("1.64"),
            win_rate_percent=Decimal("48.08"),
            payoff_ratio=Decimal("1.74"),
            max_drawdown_percent=Decimal("2.80"),
            robustness_passed=False,
            statistical_uncertainty_passed=False,
            temporal_stability_passed=False,
            fold_returns=[Decimal("0.1")] * 4,
            backtest_fingerprint=f"bt_{index}",
        )
        for index in range(18)
    ]

    _, revisions = generate_campaign_failure_revisions(candidate, aggregate, cells)

    selective = next(item for item in revisions if item.label == "提高联合信号质量")
    assert selective.changed_parameters == {"band_deviations": "2.75"}
    assert "zscore_entry" not in selective.changed_parameters


def test_mean_reversion_revision_uses_mean_completion_after_time_limit_exists():
    spec = default_strategy_spec(StrategyKind.MEAN_REVERSION)
    payload = spec.model_dump(mode="json", exclude={"fingerprint"})
    payload["parameters"]["maximum_holding_bars"] = 40
    candidate = create_candidate(payload)
    aggregate = CampaignCandidateAggregate(
        library_entry_id="candidate",
        strategy_fingerprint=candidate.spec_fingerprint,
        family="mean_reversion",
        archetype="agent_created_candidate",
        market_count=3,
        robust_market_count=0,
        positive_stress_market_count=3,
        robust_market_ratio=Decimal("0"),
        positive_stress_ratio=Decimal("1"),
        median_return_percent=Decimal("3"),
        median_profit_factor=Decimal("1.3"),
        median_win_rate_percent=Decimal("40"),
        median_payoff_ratio=Decimal("2"),
        worst_drawdown_percent=Decimal("10"),
        cross_market_passed=False,
        rank_score=Decimal("10"),
    )
    cells = [
        ResearchCampaignCell(
            library_entry_id="candidate",
            strategy_fingerprint=candidate.spec_fingerprint,
            family="mean_reversion",
            archetype="agent_created_candidate",
            dataset_id=f"dataset_{index}",
            symbol=f"symbol_{index}",
            timeframe="1h",
            total_trades=100,
            return_percent=Decimal("3"),
            stress_return_percent=Decimal("1"),
            profit_factor=Decimal("1.3"),
            win_rate_percent=Decimal("40"),
            payoff_ratio=Decimal("2"),
            max_drawdown_percent=Decimal("10"),
            robustness_passed=False,
            statistical_uncertainty_passed=False,
            temporal_stability_passed=False,
            fold_returns=[Decimal("1")] * 4,
            backtest_fingerprint=f"bt_{index}",
        )
        for index in range(3)
    ]

    _, revisions = generate_campaign_failure_revisions(candidate, aggregate, cells)

    completion = next(item for item in revisions if item.label == "触及统计均值先减仓")
    assert completion.changed_parameters == {"mean_completion_mode": "mean_touch"}
    structural_entry = next(
        item for item in revisions if item.label == "等待统计偏离重返确认"
    )
    assert structural_entry.changed_parameters == {"entry_model": "zscore_reentry"}


def test_campaign_revision_can_raise_frequency_when_joint_point_quality_is_met():
    entry = library_entry("builtin_adx_range_bollinger_20_v1")
    candidate = create_candidate(entry.validated_spec)
    aggregate = CampaignCandidateAggregate(
        library_entry_id=entry.id,
        strategy_fingerprint=entry.validated_spec.fingerprint,
        family="mean_reversion",
        archetype=entry.archetype,
        market_count=18,
        robust_market_count=0,
        positive_stress_market_count=12,
        robust_market_ratio=Decimal("0"),
        positive_stress_ratio=Decimal("0.667"),
        median_return_percent=Decimal("0.52"),
        median_profit_factor=Decimal("1.56"),
        median_win_rate_percent=Decimal("50"),
        median_payoff_ratio=Decimal("1.63"),
        worst_drawdown_percent=Decimal("10"),
        cross_market_passed=False,
        rank_score=Decimal("40"),
    )
    cells = [
        ResearchCampaignCell(
            library_entry_id=entry.id,
            strategy_fingerprint=entry.validated_spec.fingerprint,
            family="mean_reversion",
            archetype=entry.archetype,
            dataset_id=f"dataset_{index}",
            symbol=f"symbol_{index % 6}",
            exchange=("binance", "bybit", "okx")[index % 3],
            timeframe="1h",
            total_trades=5,
            return_percent=Decimal("0.5"),
            stress_return_percent=Decimal("0.1"),
            profit_factor=Decimal("1.56"),
            win_rate_percent=Decimal("50"),
            payoff_ratio=Decimal("1.63"),
            max_drawdown_percent=Decimal("5"),
            robustness_passed=False,
            statistical_uncertainty_passed=False,
            temporal_stability_passed=False,
            fold_returns=[Decimal("0.1")] * 4,
            backtest_fingerprint=f"bt_{index}",
        )
        for index in range(18)
    ]

    attribution, revisions = generate_campaign_failure_revisions(
        candidate,
        aggregate,
        cells,
    )

    frequency = next(
        item
        for item in revisions
        if item.label == "increase_signal_frequency_with_quality_floor"
    )
    assert "insufficient_trade_evidence" in attribution.codes
    assert frequency.changed_parameters == {"band_deviations": "1.80"}
    assert len(frequency.changed_parameters) == 1


def test_confirmed_reentry_quality_floor_generates_three_bounded_frequency_paths():
    entry = library_entry("builtin_zscore_20_reentry_minimum_r_v1")
    candidate = create_candidate(entry.validated_spec)
    aggregate = CampaignCandidateAggregate(
        library_entry_id=entry.id,
        strategy_fingerprint=entry.validated_spec.fingerprint,
        family="mean_reversion",
        archetype=entry.archetype,
        market_count=6,
        robust_market_count=0,
        positive_stress_market_count=5,
        high_quality_market_count=0,
        robust_market_ratio=Decimal("0"),
        positive_stress_ratio=Decimal("0.833"),
        high_quality_market_ratio=Decimal("0"),
        median_return_percent=Decimal("0.21"),
        median_profit_factor=Decimal("2.55"),
        median_win_rate_percent=Decimal("70.84"),
        median_payoff_ratio=Decimal("1.40"),
        worst_drawdown_percent=Decimal("1.20"),
        cross_market_passed=False,
        rank_score=Decimal("55.30"),
    )
    cells = [
        ResearchCampaignCell(
            library_entry_id=entry.id,
            strategy_fingerprint=entry.validated_spec.fingerprint,
            family="mean_reversion",
            archetype=entry.archetype,
            dataset_id=f"dataset_{index}",
            exchange=("binance", "okx")[index // 3],
            symbol=("XLM/USDT", "MANA/USDT", "SAND/USDT")[index % 3],
            timeframe="1h",
            total_trades=5,
            return_percent=Decimal("0.21"),
            stress_return_percent=Decimal("0.10") if index < 5 else Decimal("-0.10"),
            profit_factor=Decimal("2.55"),
            win_rate_percent=Decimal("70.84"),
            payoff_ratio=Decimal("1.40"),
            max_drawdown_percent=Decimal("1.20"),
            robustness_passed=False,
            statistical_uncertainty_passed=False,
            temporal_stability_passed=False,
            fold_returns=[Decimal("0.1")] * 4,
            backtest_fingerprint=f"bt_{index}",
        )
        for index in range(6)
    ]

    attribution, revisions = generate_campaign_failure_revisions(
        candidate,
        aggregate,
        cells,
    )

    assert "insufficient_trade_evidence" in attribution.codes
    assert [item.label for item in revisions] == [
        "increase_signal_frequency_with_quality_floor",
        "放宽统计极端门槛以增加样本",
        "扩大可交易震荡状态",
    ]
    assert [item.changed_parameters for item in revisions] == [
        {"reentry_window": 3},
        {"zscore_entry": "1.58"},
        {"adx_threshold": "27.50"},
    ]
    assert all(len(item.changed_parameters) == 1 for item in revisions)


def test_mean_touch_low_payoff_revision_restores_minimum_reward_exit():
    entry = library_entry("builtin_adx_range_vwap_30_mean_touch_exit_v1")
    candidate = create_candidate(entry.validated_spec)
    aggregate = CampaignCandidateAggregate(
        library_entry_id=entry.id,
        strategy_fingerprint=entry.validated_spec.fingerprint,
        family="mean_reversion",
        archetype=entry.archetype,
        market_count=6,
        robust_market_count=0,
        positive_stress_market_count=4,
        high_quality_market_count=0,
        robust_market_ratio=Decimal("0"),
        positive_stress_ratio=Decimal("0.667"),
        high_quality_market_ratio=Decimal("0"),
        median_return_percent=Decimal("3.07"),
        median_profit_factor=Decimal("1.29"),
        median_win_rate_percent=Decimal("56.79"),
        median_payoff_ratio=Decimal("0.93"),
        worst_drawdown_percent=Decimal("3.84"),
        cross_market_passed=False,
        rank_score=Decimal("37.48"),
    )
    cells = [
        ResearchCampaignCell(
            library_entry_id=entry.id,
            strategy_fingerprint=entry.validated_spec.fingerprint,
            family="mean_reversion",
            archetype=entry.archetype,
            dataset_id=f"dataset_{index}",
            symbol=("BCH/USDT", "ETC/USDT", "ATOM/USDT")[index % 3],
            exchange=("binance", "okx")[index // 3],
            timeframe="1h",
            total_trades=60,
            return_percent=Decimal("3"),
            stress_return_percent=Decimal("1"),
            profit_factor=Decimal("1.29"),
            win_rate_percent=Decimal("56.79"),
            payoff_ratio=Decimal("0.93"),
            max_drawdown_percent=Decimal("4"),
            robustness_passed=False,
            statistical_uncertainty_passed=False,
            temporal_stability_passed=index < 2,
            fold_returns=[Decimal("1")] * 4,
            backtest_fingerprint=f"bt_{index}",
        )
        for index in range(6)
    ]

    attribution, revisions = generate_campaign_failure_revisions(
        candidate,
        aggregate,
        cells,
    )

    rebalance = next(item for item in revisions if item.label == "恢复最低收益风险目标")
    assert "low_payoff" in attribution.codes
    assert rebalance.changed_parameters == {"mean_completion_mode": "minimum_reward"}
    assert len(rebalance.changed_parameters) == 1


def test_explicit_target_low_payoff_revision_changes_primary_exit():
    entry = library_entry("builtin_zscore_30_reentry_minimum_r_v1")
    candidate = create_candidate(entry.validated_spec)
    failed = BacktestResult(
        initial_equity=Decimal("100"),
        final_equity=Decimal("101"),
        net_profit=Decimal("1"),
        return_percent=Decimal("1"),
        max_drawdown_percent=Decimal("6"),
        win_rate_percent=Decimal("47"),
        profit_factor=Decimal("1.05"),
        sharpe=Decimal("0.1"),
        total_trades=400,
        trades=[],
        equity_curve=[Decimal("100"), Decimal("101")],
        payoff_ratio=Decimal("1.20"),
        stress_return_percent=Decimal("-1"),
        trade_sample_floor=80,
    )
    candidate = attach_backtest(candidate, "ds_low_payoff", failed)

    _, revisions = generate_failure_driven_revisions(candidate, max_revisions=5)

    target = next(item for item in revisions if item.label == "提高主要收益风险目标")
    assert target.changed_parameters == {"first_target_reward_risk": "1.50"}
    assert "second_target_extension" not in target.changed_parameters
