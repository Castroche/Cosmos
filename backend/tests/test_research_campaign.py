import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from app_fixture import services as api
from cosmos_quant.api_contracts import (
    CampaignRevisionRequest,
    CausalPolicyBacktestRequest,
    CausalPolicyCampaignSpec,
    CausalPolicyRevisionRequest,
    ResearchCampaignRequest,
)
from cosmos_quant.demo import demo_candles
from cosmos_quant.research import build_research_dataset
from cosmos_quant.research_campaign import (
    CAMPAIGN_ENGINE_VERSION,
    ResearchCampaignCell,
    ResearchCampaignJob,
    ResearchCampaignReport,
    build_campaign_execution_certificate,
    campaign_cell_cache_key,
    campaign_request_fingerprint,
    causal_policy_definitions_fingerprint,
    finalize_campaign,
    historical_feature_eligibility,
    ineligible_campaign_cell,
    research_experiment_fingerprint,
    research_lineage_id,
    research_trial_family_id,
    validate_campaign_execution_certificate,
)
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_library import library_entry
from fastapi import HTTPException


def _cell(
    entry_id: str,
    dataset_id: str,
    *,
    robust: bool,
    fold_returns: list[str],
    exchange: str | None = None,
    symbol: str | None = None,
    timeframe: str = "1h",
    profit_factor: str | None = None,
    win_rate_percent: str = "55",
    payoff_ratio: str = "1.5",
    p_value: str | None = None,
) -> ResearchCampaignCell:
    total_trades = 120
    wins = round(total_trades * float(win_rate_percent) / 100)
    return ResearchCampaignCell(
        library_entry_id=entry_id,
        strategy_fingerprint=f"fp_{entry_id}",
        family="trend",
        archetype="time_series_momentum",
        dataset_id=dataset_id,
        exchange=exchange or f"venue_{dataset_id}",
        symbol=symbol or dataset_id,
        timeframe=timeframe,
        total_trades=total_trades,
        return_percent=Decimal("4") if robust else Decimal("-1"),
        stress_return_percent=Decimal("2") if robust else Decimal("-2"),
        profit_factor=Decimal(profit_factor or ("1.4" if robust else "0.8")),
        win_rate_percent=Decimal(win_rate_percent),
        payoff_ratio=Decimal(payoff_ratio),
        max_drawdown_percent=Decimal("8"),
        robustness_passed=robust,
        statistical_uncertainty_passed=robust,
        temporal_stability_passed=robust,
        fold_returns=[Decimal(value) for value in fold_returns],
        backtest_fingerprint=f"bt_{entry_id}_{dataset_id}",
        win_count=wins,
        loss_count=total_trades - wins,
        one_sided_sharpe_p_value=Decimal(p_value or ("0.0001" if robust else "0.50")),
        one_sided_win_rate_p_value=Decimal(p_value or ("0.0001" if robust else "0.50")),
        deflated_sharpe_probability=Decimal("0.99" if robust else "0.10"),
        expected_r=Decimal("0.25" if robust else "-0.10"),
        cost_adjusted_expectancy=Decimal("12" if robust else "-4"),
        expected_r_confidence_lower_bound=Decimal("0.05" if robust else "-0.20"),
        expected_r_p_value=Decimal(p_value or ("0.0001" if robust else "0.50")),
        expected_r_testable=True,
        expected_r_passed=robust,
        expected_r_observation_count=total_trades,
    )


def test_campaign_request_budget_covers_full_eighty_template_library_and_revisions():
    request = ResearchCampaignRequest(
        dataset_ids=[f"development_{index}" for index in range(6)],
        blind_dataset_ids=[f"blind_{index}" for index in range(6)],
        library_entry_ids=[f"library_{index}" for index in range(80)],
        candidate_ids=[f"candidate_{index}" for index in range(6)],
        max_backtests=522,
    )

    assert len(request.library_entry_ids) == 80
    assert request.max_backtests == 522
    with pytest.raises(ValueError):
        ResearchCampaignRequest(
            dataset_ids=["development"],
            max_backtests=6001,
        )
    with pytest.raises(ValueError):
        CampaignRevisionRequest(max_revisions=4)


def test_causal_policy_endpoint_binds_public_library_to_immutable_dataset(
    monkeypatch,
    tmp_path,
):
    store = SqliteStore(tmp_path / "causal-policy.db")
    monkeypatch.setattr(api, "store", store)
    candles = demo_candles(500)
    dataset = build_research_dataset(
        candles,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store.save_research_dataset(dataset, candles)
    entry_ids = [
        "builtin_ema_12_26_trend_v2",
        "builtin_sma_20_100_trend_v2",
        "builtin_zscore_20_rsi_v2",
    ]

    result = asyncio.run(
        api.backtest_causal_strategy_policy(
            CausalPolicyBacktestRequest(
                dataset_id=dataset.id,
                library_entry_ids=entry_ids,
                maximum_candidates_per_bar=3,
                abstain_on_ambiguous_regime=False,
            )
        )
    )

    assert result.dataset_id == dataset.id
    assert result.data_fingerprint == dataset.data_fingerprint
    assert result.strategy_fingerprint == result.cost_model[
        "causal_library_policy_fingerprint"
    ]
    assert result.total_trades > 0
    assert all(item.algorithm.startswith("library:builtin_") for item in result.trades)
    assert result.integrity_checks["immutable_dataset_binding"] is True
    assert result.integrity_checks["causal_library_policy_return_inputs_forbidden"] is True
    with pytest.raises(ValueError, match="must be unique"):
        CausalPolicyBacktestRequest(
            dataset_id=dataset.id,
            library_entry_ids=[entry_ids[0], entry_ids[0]],
        )


def test_causal_policy_campaign_runs_frozen_meta_hypotheses(monkeypatch, tmp_path):
    store = SqliteStore(tmp_path / "causal-policy-campaign.db")
    monkeypatch.setattr(api, "store", store)
    candles = demo_candles(500)
    dataset = build_research_dataset(
        candles,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store.save_research_dataset(dataset, candles)
    entry_ids = [
        "builtin_ema_12_26_trend_v2",
        "builtin_sma_20_100_trend_v2",
        "builtin_zscore_20_rsi_v2",
    ]
    request = ResearchCampaignRequest(
        dataset_ids=[dataset.id],
        causal_policy_specs=[
            CausalPolicyCampaignSpec(
                id="selective",
                library_entry_ids=entry_ids,
                maximum_candidates_per_bar=1,
            ),
            CausalPolicyCampaignSpec(
                id="balanced",
                library_entry_ids=entry_ids,
                maximum_candidates_per_bar=2,
            ),
        ],
        max_backtests=2,
    )

    report = asyncio.run(api.execute_research_campaign(request))

    assert len(report.cells) == 2
    assert len(report.causal_policy_definitions) == 2
    assert report.causal_policy_definitions_fingerprint == (
        causal_policy_definitions_fingerprint(report.causal_policy_definitions)
    )
    assert {item.family for item in report.cells} == {"agent_policy"}
    assert {item.strategy_fingerprint for item in report.cells} == {
        item["policy_fingerprint"] for item in report.causal_policy_definitions
    }
    assert all(item.backtest_fingerprint for item in report.cells)
    assert report.blind_consumption_id is None
    assert report.autonomous_execution_eligible is False

    replay = asyncio.run(api.execute_research_campaign(request))
    assert replay.id == report.id
    assert replay.research_experiment_id == report.research_experiment_id

    revision = asyncio.run(
        api.revise_campaign_causal_policy(
            report.id,
            CausalPolicyRevisionRequest(max_revisions=3),
        )
    )
    assert 1 <= len(revision.revisions) <= 3
    frozen_parent = ResearchCampaignReport.model_validate(
        store.load_research_campaign(report.id)
    )
    assert frozen_parent.authorized_policy_revision_fingerprints
    successor_request = request.model_copy(
        update={
            "causal_policy_specs": [
                *request.causal_policy_specs,
                *revision.revisions,
            ],
            "max_backtests": 2 + len(revision.revisions),
            "parent_campaign_id": report.id,
        }
    )
    successor = asyncio.run(api.execute_research_campaign(successor_request))
    assert successor.parent_campaign_id == report.id
    assert successor.experiment_kind == "bounded_policy_revision"
    assert set(frozen_parent.authorized_policy_revision_fingerprints).issubset(
        {item.strategy_fingerprint for item in successor.cells}
    )
    with pytest.raises(HTTPException, match="only one successor"):
        asyncio.run(
            api.execute_research_campaign(
                successor_request.model_copy(
                    update={
                        "causal_policy_specs": successor_request.causal_policy_specs[:-1]
                    }
                )
            )
        )


def test_causal_policy_campaign_request_rejects_fake_hypothesis_sets():
    policy = CausalPolicyCampaignSpec(
        id="selective",
        library_entry_ids=[
            "builtin_ema_12_26_trend_v2",
            "builtin_sma_20_100_trend_v2",
        ],
    )
    with pytest.raises(ValueError, match="at least two hypotheses"):
        ResearchCampaignRequest(
            dataset_ids=["dataset"],
            causal_policy_specs=[policy],
        )
    with pytest.raises(ValueError, match="cannot be mixed"):
        ResearchCampaignRequest(
            dataset_ids=["dataset"],
            library_entry_ids=["builtin_ema_12_26_trend_v2"],
            causal_policy_specs=[
                policy,
                policy.model_copy(update={"id": "balanced"}),
            ],
        )
    with pytest.raises(ValueError, match="structurally unique"):
        ResearchCampaignRequest(
            dataset_ids=["dataset"],
            causal_policy_specs=[
                policy,
                policy.model_copy(update={"id": "duplicate_rules"}),
            ],
            max_backtests=2,
        )


def test_causal_policy_campaign_requires_every_frozen_prior_feature():
    entry = api.build_causal_policy_campaign_entry(
        CausalPolicyCampaignSpec(
            id="derivative_aware",
            library_entry_ids=[
                "builtin_ema_12_26_trend_v2",
                "builtin_funding_aware_roc_24_v3",
            ],
        )
    )

    assert set(entry.required_data) == {"ohlcv", "funding_rate"}


def test_main_campaign_freezes_one_bounded_successor(monkeypatch, tmp_path):
    store = SqliteStore(tmp_path / "bounded-successor.db")
    monkeypatch.setattr(api, "store", store)
    candles = demo_candles(1200)
    dataset = build_research_dataset(
        candles,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store.save_research_dataset(dataset, candles)
    entry = library_entry("builtin_zscore_30_standard_horizon_v1")
    failed_cell = _cell(
        entry.id,
        dataset.id,
        robust=False,
        fold_returns=["-1", "1", "-1", "1"],
        win_rate_percent="35",
        payoff_ratio="2.2",
        profit_factor="1.3",
    ).model_copy(
        update={
            "strategy_fingerprint": entry.validated_spec.fingerprint,
            "family": entry.family.value,
            "archetype": entry.archetype,
        }
    )
    parent = finalize_campaign(
        dataset_ids=[dataset.id],
        library_entry_ids=[entry.id],
        cells=[failed_cell],
        research_lineage_id_value=research_lineage_id([dataset]),
        research_trial_family_id_value=research_trial_family_id([dataset]),
    )
    store.save_research_campaign(parent)

    revision = asyncio.run(
        api.revise_campaign_strategy(
            parent.id,
            CampaignRevisionRequest(
                library_entry_id=entry.id,
                max_revisions=3,
            ),
        )
    )
    frozen_parent = ResearchCampaignReport.model_validate(
        store.load_research_campaign(parent.id)
    )
    child_ids = [item.candidate.id for item in revision.revisions]
    assert 1 <= len(child_ids) <= 3
    assert frozen_parent.authorized_revision_candidate_ids == child_ids

    request = ResearchCampaignRequest(
        dataset_ids=[dataset.id],
        library_entry_ids=[entry.id],
        candidate_ids=child_ids,
        max_backtests=1 + len(child_ids),
        parent_campaign_id=parent.id,
    )
    successor = asyncio.run(api.execute_research_campaign(request))
    assert successor.parent_campaign_id == parent.id
    assert successor.experiment_kind == "bounded_revision"

    replay = asyncio.run(api.execute_research_campaign(request))
    assert replay.id == successor.id
    assert replay.parent_campaign_id == parent.id

    unparented_replay = asyncio.run(
        api.execute_research_campaign(
            request.model_copy(update={"parent_campaign_id": None})
        )
    )
    assert unparented_replay.id == successor.id
    assert unparented_replay.parent_campaign_id == parent.id

    with pytest.raises(HTTPException, match="only one successor"):
        asyncio.run(
            api.execute_research_campaign(
                request.model_copy(
                    update={
                        "candidate_ids": child_ids[:1],
                        "max_backtests": 2,
                    }
                )
            )
        )


def test_required_feature_must_cover_entire_dataset_without_large_gaps():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    dataset = SimpleNamespace(data_start=start, data_end=start + timedelta(days=10))
    complete = [
        SimpleNamespace(timestamp=start + timedelta(hours=8 * index)) for index in range(31)
    ]
    sparse = [
        SimpleNamespace(timestamp=start + timedelta(days=8)),
        SimpleNamespace(timestamp=start + timedelta(days=9)),
    ]

    assert (
        historical_feature_eligibility(
            dataset=dataset,
            required_data=["ohlcv", "funding_rate"],
            funding=complete,
            open_interest=[],
            positioning=[],
            basis=[],
            order_flow=[],
        )
        == []
    )
    reasons = historical_feature_eligibility(
        dataset=dataset,
        required_data=["ohlcv", "open_interest"],
        funding=[],
        open_interest=sparse,
        positioning=[],
        basis=[],
        order_flow=[],
    )
    assert any("after dataset start" in reason for reason in reasons)


def test_data_ineligible_cells_never_enter_campaign_ranking():
    entry = SimpleNamespace(
        id="requires_oi",
        validated_spec=SimpleNamespace(fingerprint="fp_requires_oi"),
        family=SimpleNamespace(value="trend"),
        archetype="oi_confirmed",
    )
    dataset = SimpleNamespace(
        id="market_missing_oi",
        data_fingerprint="dataset_fp",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    cell = ineligible_campaign_cell(
        library_entry=entry,
        dataset=dataset,
        reasons=["open_interest: insufficient history"],
        dataset_role="research",
    )

    report = finalize_campaign(
        dataset_ids=[dataset.id],
        library_entry_ids=[entry.id],
        cells=[cell],
    )

    assert cell.data_eligible is False
    assert report.candidates == []
    assert report.autonomous_execution_eligible is False


def test_campaign_requires_cross_market_evidence_and_persists(tmp_path):
    cells = [
        _cell("stable", f"market_{index}", robust=True, fold_returns=["1"] * 4)
        for index in range(3)
    ] + [
        _cell(
            "fragile",
            f"market_{index}",
            robust=index == 0,
            fold_returns=["5", "-5", "5", "-5"],
        )
        for index in range(3)
    ]
    report = finalize_campaign(
        dataset_ids=[f"market_{index}" for index in range(3)],
        library_entry_ids=["stable", "fragile"],
        cells=cells,
    )

    stable = next(item for item in report.candidates if item.library_entry_id == "stable")
    fragile = next(item for item in report.candidates if item.library_entry_id == "fragile")
    assert stable.cross_market_passed is True
    assert fragile.cross_market_passed is False

    store = SqliteStore(tmp_path / "campaign.db")
    store.save_research_campaign(report)
    loaded = store.load_research_campaign(report.id)
    assert loaded is not None
    assert loaded["trial_count"] == 6
    assert report.autonomous_execution_eligible is False
    assert report.portfolio_allocations == []
    assert any("盲测" in reason for reason in report.selection_reasons)


def test_campaign_requires_two_independent_exchanges_for_promotion():
    same_exchange = [
        _cell(
            "stable",
            f"same_{index}",
            exchange="binance",
            symbol=f"COIN{index}/USDT",
            robust=True,
            fold_returns=["1"] * 4,
        )
        for index in range(3)
    ]
    same_report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in same_exchange],
        library_entry_ids=["stable"],
        cells=same_exchange,
    )

    replicated = [
        _cell(
            "stable",
            f"{exchange}_{index}",
            exchange=exchange,
            symbol=f"COIN{index}/USDT",
            robust=True,
            fold_returns=["1"] * 4,
        )
        for exchange in ("binance", "okx")
        for index in range(3)
    ]
    replicated_report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in replicated],
        library_entry_ids=["stable"],
        cells=replicated,
    )

    same_candidate = same_report.candidates[0]
    replicated_candidate = replicated_report.candidates[0]
    assert same_candidate.unique_exchange_count == 1
    assert same_candidate.cross_market_passed is False
    assert any("independent exchange count" in reason for reason in same_candidate.reasons)
    assert replicated_candidate.unique_exchange_count == 2
    assert replicated_candidate.cross_market_passed is True


def test_pbo_uses_largest_exact_dataset_cohort_when_feature_eligibility_differs():
    full_scope = [
        _cell(
            entry_id,
            f"{exchange}_{index}",
            exchange=exchange,
            symbol=f"COIN{index}/USDT",
            robust=True,
            fold_returns=["1", "2", "1", "2"],
        )
        for entry_id in ("full_a", "full_b")
        for exchange in ("binance", "okx")
        for index in range(3)
    ]
    partial_scope = [
        _cell(
            "derivatives_only",
            f"binance_{index}",
            exchange="binance",
            symbol=f"COIN{index}/USDT",
            robust=True,
            fold_returns=["1", "2", "1", "2"],
        )
        for index in range(3)
    ]

    report = finalize_campaign(
        dataset_ids=sorted({cell.dataset_id for cell in [*full_scope, *partial_scope]}),
        library_entry_ids=["full_a", "full_b", "derivatives_only"],
        cells=[*full_scope, *partial_scope],
    )

    assert len(report.candidates) == 3
    assert report.pbo.candidate_count == 2
    assert report.pbo.fold_count == 24


def test_campaign_does_not_use_win_rate_payoff_or_profit_factor_as_universal_gates():
    cells = [
        _cell(
            "many_small_wins",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            win_rate_percent="62",
            payoff_ratio="0.9",
            profit_factor="1.4",
        )
        for index in range(3)
    ] + [
        _cell(
            "few_large_wins",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            win_rate_percent="41",
            payoff_ratio="2.2",
            profit_factor="1.5",
        )
        for index in range(3)
    ]

    report = finalize_campaign(
        dataset_ids=[f"market_{index}" for index in range(3)],
        library_entry_ids=["many_small_wins", "few_large_wins"],
        cells=cells,
    )

    for candidate in report.candidates:
        assert candidate.cross_market_passed is True
        assert candidate.high_quality_market_ratio == Decimal("1.000")
        assert all("胜率" not in reason and "盈亏比" not in reason for reason in candidate.reasons)


def test_campaign_fdr_uses_cumulative_cross_campaign_family_size():
    cells = [
        _cell(
            "apparent_winner",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            p_value="0.001",
        )
        for index in range(3)
    ]

    report = finalize_campaign(
        dataset_ids=[f"market_{index}" for index in range(3)],
        library_entry_ids=["apparent_winner"],
        cells=cells,
        cumulative_hypothesis_count=100,
        effective_trial_count=100,
    )

    candidate = report.candidates[0]
    assert candidate.multiple_testing_adjusted_p_value == Decimal("0.1000")
    assert candidate.strategy_statistical_passed is False
    assert candidate.cross_market_passed is False


def test_campaign_requires_three_distinct_symbols_not_three_timeframes():
    cells = [
        _cell(
            "single_symbol",
            f"btc_{timeframe}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol="BTC/USDT",
            timeframe=timeframe,
        )
        for timeframe in ("15m", "1h", "4h")
    ]

    report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in cells],
        library_entry_ids=["single_symbol"],
        cells=cells,
    )

    candidate = report.candidates[0]
    assert candidate.market_count == 3
    assert candidate.unique_symbol_count == 1
    assert candidate.unique_timeframe_count == 3
    assert candidate.cross_market_passed is False
    assert any("独立币种" in reason for reason in candidate.reasons)


def test_strategy_level_evidence_replaces_repeated_cell_global_penalty():
    cells = [
        _cell(
            "consistent",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol=f"COIN{index}/USDT",
            win_rate_percent="58",
            p_value="0.01",
        ).model_copy(
            update={
                "robustness_passed": False,
                "statistical_uncertainty_passed": False,
            }
        )
        for index in range(3)
    ]

    report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in cells],
        library_entry_ids=["consistent"],
        cells=cells,
    )

    candidate = report.candidates[0]
    assert candidate.robust_market_count == 0
    assert candidate.strategy_statistical_passed is True
    assert candidate.cross_market_passed is True


def test_strategy_level_bh_rejects_quality_without_combined_significance():
    cells = [
        _cell(
            "lucky",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol=f"COIN{index}/USDT",
            win_rate_percent="58",
            p_value="0.40",
        )
        for index in range(3)
    ]

    report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in cells],
        library_entry_ids=["lucky"],
        cells=cells,
    )

    candidate = report.candidates[0]
    assert candidate.high_quality_market_ratio == Decimal("1.000")
    assert candidate.strategy_statistical_passed is False
    assert candidate.cross_market_passed is False
    assert any("BH adjusted" in reason for reason in candidate.reasons)


def test_joint_quality_requires_metrics_stress_and_time_stability_in_same_markets():
    cells = []
    for index in range(6):
        cell = _cell(
            "disjoint",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol=f"COIN{index}/USDT",
            win_rate_percent="55" if index < 3 else "45",
            p_value="0.00001",
        )
        if index < 2:
            cell = cell.model_copy(update={"stress_return_percent": Decimal("-1")})
        if index >= 3:
            cell = cell.model_copy(
                update={
                    "expected_r": Decimal("-0.1"),
                    "cost_adjusted_expectancy": Decimal("-2"),
                    "expected_r_confidence_lower_bound": Decimal("-0.2"),
                    "expected_r_passed": False,
                }
            )
        cells.append(cell)

    report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in cells],
        library_entry_ids=["disjoint"],
        cells=cells,
    )

    candidate = report.candidates[0]
    assert candidate.positive_stress_ratio == Decimal("0.667")
    assert candidate.high_quality_market_ratio == Decimal("0.167")
    assert candidate.cross_market_passed is False


def test_correlated_timeframes_are_corrected_within_symbol_before_combination():
    single_timeframe = [
        _cell(
            "single",
            f"single_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol=f"COIN{index}/USDT",
            p_value="0.02",
        )
        for index in range(3)
    ]
    duplicated_timeframes = [
        _cell(
            "duplicated",
            f"duplicated_{index}_{timeframe}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol=f"COIN{index}/USDT",
            timeframe=timeframe,
            p_value="0.02",
        )
        for index in range(3)
        for timeframe in ("1h", "4h")
    ]

    single_report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in single_timeframe],
        library_entry_ids=["single"],
        cells=single_timeframe,
    )
    duplicated_report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in duplicated_timeframes],
        library_entry_ids=["duplicated"],
        cells=duplicated_timeframes,
    )

    assert (
        duplicated_report.candidates[0].strategy_p_value
        >= single_report.candidates[0].strategy_p_value
    )


def test_campaign_hard_rejects_excessive_worst_market_drawdown():
    cells = [
        _cell(
            "deep_drawdown",
            f"market_{index}",
            robust=True,
            fold_returns=["1"] * 4,
            symbol=f"COIN{index}/USDT",
        ).model_copy(update={"max_drawdown_percent": Decimal("25")})
        for index in range(3)
    ]

    report = finalize_campaign(
        dataset_ids=[cell.dataset_id for cell in cells],
        library_entry_ids=["deep_drawdown"],
        cells=cells,
    )

    candidate = report.candidates[0]
    assert candidate.cross_market_passed is False
    assert any("worst drawdown" in reason for reason in candidate.reasons)


def test_campaign_uses_blind_markets_only_after_research_selection():
    research_ids = [f"research_{index}" for index in range(3)]
    blind_ids = [f"blind_{index}" for index in range(3)]
    cells = [
        *[
            _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
            for dataset_id in research_ids
        ],
        *[
            _cell("fragile", dataset_id, robust=False, fold_returns=["2", "-2"] * 2)
            for dataset_id in research_ids
        ],
        *[
            _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
            for dataset_id in blind_ids
        ],
        *[
            _cell("fragile", dataset_id, robust=True, fold_returns=["9"] * 4)
            for dataset_id in blind_ids
        ],
    ]

    report = finalize_campaign(
        dataset_ids=[*research_ids, *blind_ids],
        blind_dataset_ids=blind_ids,
        library_entry_ids=["stable", "fragile"],
        cells=cells,
        universe_snapshot_id="universe_test",
        universe_snapshot_fingerprint="universe_fingerprint_test",
        point_in_time_selection_passed=True,
        historical_survivorship_complete=True,
        survivorship_bias_control_passed=True,
    )

    assert report.candidates[0].library_entry_id == "stable"
    assert report.blind_candidate is not None
    assert report.blind_candidate.library_entry_id == "stable"
    assert report.blind_selected_candidate_passed is True
    assert report.autonomous_execution_eligible is True
    assert report.portfolio_allocations
    assert {
        item["library_entry_id"] for item in report.portfolio_allocations
    } == {report.selected_library_entry_id}


def test_campaign_cannot_be_execution_eligible_without_survivorship_control():
    research_ids = [f"research_{index}" for index in range(3)]
    blind_ids = [f"blind_{index}" for index in range(3)]
    report = finalize_campaign(
        dataset_ids=[*research_ids, *blind_ids],
        blind_dataset_ids=blind_ids,
        library_entry_ids=["stable"],
        cells=[
            *[
                _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
                for dataset_id in research_ids
            ],
            *[
                _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
                for dataset_id in blind_ids
            ],
        ],
    )

    assert report.blind_selected_candidate_passed is True
    assert report.autonomous_execution_eligible is False
    assert report.execution_certificate is None
    assert any("点时市场宇宙" in reason for reason in report.selection_reasons)


def test_sealed_blind_opening_requires_survivorship_control():
    selected = SimpleNamespace(library_entry_id="stable")
    passed_pbo = SimpleNamespace(passed=True)

    assert not api.campaign_blind_opening_allowed(
        provisional_selected=selected,
        research_pbo=passed_pbo,
        blind_dataset_ids=["blind_one"],
        survivorship_bias_control_passed=False,
    )
    assert api.campaign_blind_opening_allowed(
        provisional_selected=selected,
        research_pbo=passed_pbo,
        blind_dataset_ids=["blind_one"],
        survivorship_bias_control_passed=True,
    )


def test_execution_certificate_binds_plan_and_detects_campaign_tampering(
    monkeypatch,
):
    research_ids = [f"research_{index}" for index in range(3)]
    blind_ids = [f"blind_{index}" for index in range(3)]
    cells = [
        *[
            _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
            for dataset_id in research_ids
        ],
        *[
            _cell("fragile", dataset_id, robust=False, fold_returns=["2", "-2"] * 2)
            for dataset_id in research_ids
        ],
        *[
            _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
            for dataset_id in blind_ids
        ],
    ]
    report = finalize_campaign(
        dataset_ids=[*research_ids, *blind_ids],
        blind_dataset_ids=blind_ids,
        blind_set_id="blindset_certificate",
        blind_consumption_id="blind_consumption_certificate",
        library_entry_ids=["stable", "fragile"],
        cells=cells,
        universe_snapshot_id="universe_test",
        universe_snapshot_fingerprint="universe_fingerprint_test",
        point_in_time_selection_passed=True,
        historical_survivorship_complete=True,
        survivorship_bias_control_passed=True,
    )
    dataset = build_research_dataset(
        demo_candles(1200),
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    fingerprints = {
        dataset_id: dataset.data_fingerprint for dataset_id in [*research_ids, *blind_ids]
    }
    report.execution_certificate = build_campaign_execution_certificate(
        report,
        dataset_fingerprints=fingerprints,
    )
    certificate = report.execution_certificate
    assert certificate is not None
    assert certificate.research_aggregate_fingerprint
    assert certificate.blind_aggregate_fingerprint
    del monkeypatch
    validate_campaign_execution_certificate(
        report,
        certificate,
        dataset_fingerprints=fingerprints,
    )

    tampered = report.model_dump(mode="json")
    tampered["candidates"][0]["median_win_rate_percent"] = "99"
    with pytest.raises(ValueError):
        validate_campaign_execution_certificate(
            ResearchCampaignReport.model_validate(tampered),
            certificate,
            dataset_fingerprints=fingerprints,
        )

    tampered_blind = report.model_dump(mode="json")
    tampered_blind["blind_candidate"]["worst_drawdown_percent"] = "99"
    with pytest.raises(ValueError):
        validate_campaign_execution_certificate(
            ResearchCampaignReport.model_validate(tampered_blind),
            certificate,
            dataset_fingerprints=fingerprints,
        )

    tampered_ledger = report.model_dump(mode="json")
    tampered_ledger["effective_trial_count"] += 1
    with pytest.raises(ValueError):
        validate_campaign_execution_certificate(
            ResearchCampaignReport.model_validate(tampered_ledger),
            certificate,
            dataset_fingerprints=fingerprints,
        )


def test_execution_certificate_binds_full_causal_policy_definition():
    entry_ids = [
        "builtin_ema_12_26_trend_v2",
        "builtin_sma_20_100_trend_v2",
        "builtin_zscore_20_rsi_v2",
    ]
    stable = api.build_causal_policy_campaign_entry(
        CausalPolicyCampaignSpec(
            id="selective",
            library_entry_ids=entry_ids,
            maximum_candidates_per_bar=1,
        )
    )
    fragile = api.build_causal_policy_campaign_entry(
        CausalPolicyCampaignSpec(
            id="balanced",
            library_entry_ids=entry_ids,
            maximum_candidates_per_bar=2,
        )
    )
    research_ids = [f"research_{index}" for index in range(3)]
    blind_ids = [f"blind_{index}" for index in range(3)]

    def policy_cell(entry, dataset_id: str, *, robust: bool) -> ResearchCampaignCell:
        return _cell(
            entry.id,
            dataset_id,
            robust=robust,
            fold_returns=["1"] * 4 if robust else ["-1", "1"] * 2,
        ).model_copy(
            update={
                "strategy_fingerprint": entry.policy_fingerprint,
                "family": "agent_policy",
                "archetype": entry.archetype,
            }
        )

    report = finalize_campaign(
        dataset_ids=[*research_ids, *blind_ids],
        blind_dataset_ids=blind_ids,
        blind_set_id="blindset_policy_certificate",
        blind_consumption_id="blind_consumption_policy_certificate",
        library_entry_ids=[stable.id, fragile.id],
        causal_policy_definitions=[
            stable.frozen_definition,
            fragile.frozen_definition,
        ],
        cells=[
            *[policy_cell(stable, dataset_id, robust=True) for dataset_id in research_ids],
            *[policy_cell(fragile, dataset_id, robust=False) for dataset_id in research_ids],
            *[policy_cell(stable, dataset_id, robust=True) for dataset_id in blind_ids],
        ],
        universe_snapshot_id="universe_policy_test",
        universe_snapshot_fingerprint="universe_policy_fingerprint_test",
        point_in_time_selection_passed=True,
        historical_survivorship_complete=True,
        survivorship_bias_control_passed=True,
    )
    fingerprints = {
        dataset_id: f"fingerprint_{dataset_id}"
        for dataset_id in [*research_ids, *blind_ids]
    }

    certificate = build_campaign_execution_certificate(
        report,
        dataset_fingerprints=fingerprints,
    )

    assert certificate.strategy_fingerprint == stable.policy_fingerprint
    assert certificate.selected_strategy_definition_fingerprint
    assert certificate.causal_policy_definitions_fingerprint == (
        report.causal_policy_definitions_fingerprint
    )
    assert certificate.portfolio_allocations_fingerprint
    tampered = report.model_copy(deep=True)
    tampered.causal_policy_definitions[0]["maximum_candidates_per_bar"] = 3
    with pytest.raises(ValueError, match="definition"):
        validate_campaign_execution_certificate(
            tampered,
            certificate,
            dataset_fingerprints=fingerprints,
        )
    tampered_allocation = report.model_copy(deep=True)
    tampered_allocation.portfolio_allocations[0]["weight"] = "0.01"
    with pytest.raises(ValueError, match="does not match"):
        validate_campaign_execution_certificate(
            tampered_allocation,
            certificate,
            dataset_fingerprints=fingerprints,
        )

def test_campaign_rejects_research_winner_that_fails_blind_markets():
    research_ids = [f"research_{index}" for index in range(3)]
    blind_ids = [f"blind_{index}" for index in range(3)]
    cells = [
        *[
            _cell("stable", dataset_id, robust=True, fold_returns=["1"] * 4)
            for dataset_id in research_ids
        ],
        *[
            _cell("fragile", dataset_id, robust=False, fold_returns=["2", "-2"] * 2)
            for dataset_id in research_ids
        ],
        *[
            _cell("stable", dataset_id, robust=False, fold_returns=["-1"] * 4)
            for dataset_id in blind_ids
        ],
    ]

    report = finalize_campaign(
        dataset_ids=[*research_ids, *blind_ids],
        blind_dataset_ids=blind_ids,
        library_entry_ids=["stable", "fragile"],
        cells=cells,
    )

    assert report.blind_selected_candidate_passed is False
    assert report.autonomous_execution_eligible is False
    assert any("盲测" in reason for reason in report.selection_reasons)


def test_campaign_without_blind_evidence_remains_offline_only():
    cells = [
        _cell("stable", f"market_{index}", robust=True, fold_returns=["1"] * 4)
        for index in range(3)
    ]
    report = finalize_campaign(
        dataset_ids=[f"market_{index}" for index in range(3)],
        library_entry_ids=["stable"],
        cells=cells,
    )
    assert report.autonomous_execution_eligible is False
    assert report.execution_certificate is None


def test_campaign_rejects_unsealed_or_mismatched_blind_datasets(monkeypatch, tmp_path):
    store = SqliteStore(tmp_path / "campaign.db")
    monkeypatch.setattr(api, "store", store)
    candles = demo_candles(1200)
    generic = build_research_dataset(
        candles,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    development = build_research_dataset(
        candles[:900],
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        research_role="development",
        source_dataset_id=generic.id,
        blind_set_id="blindset_test",
        split_boundary=candles[950].timestamp,
        embargo_bars=50,
    )
    blind = build_research_dataset(
        candles[950:],
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        research_role="blind",
        source_dataset_id=generic.id,
        blind_set_id="blindset_test",
        split_boundary=candles[950].timestamp,
        embargo_bars=50,
        blind_sealed=True,
    )
    for dataset, subset in (
        (generic, candles),
        (development, candles[:900]),
        (blind, candles[950:]),
    ):
        store.save_research_dataset(dataset, subset)

    with pytest.raises(HTTPException, match="role-bound"):
        asyncio.run(
            api.execute_research_campaign(
                ResearchCampaignRequest(
                    dataset_ids=[development.id],
                    blind_dataset_ids=[generic.id],
                    library_entry_ids=["builtin_rsi_14_30_70_v2"],
                    max_backtests=2,
                )
            )
        )
    with pytest.raises(HTTPException, match="development splits"):
        asyncio.run(
            api.execute_research_campaign(
                ResearchCampaignRequest(
                    dataset_ids=[generic.id],
                    blind_dataset_ids=[blind.id],
                    library_entry_ids=["builtin_rsi_14_30_70_v2"],
                    max_backtests=2,
                )
            )
        )


def test_campaign_job_and_cell_cache_persist(tmp_path):
    store = SqliteStore(tmp_path / "campaign.db")
    request = {"dataset_ids": ["dataset"], "max_backtests": 31}
    job = ResearchCampaignJob(
        request=request,
        request_fingerprint=campaign_request_fingerprint(request),
    )
    cell = _cell("stable", "market", robust=True, fold_returns=["1"] * 4)
    cache_key = campaign_cell_cache_key(
        strategy_fingerprint=cell.strategy_fingerprint,
        dataset_fingerprint="data_fp",
        dataset_id=cell.dataset_id,
        trial_count=31,
        research_configuration_fingerprint="config_fp",
    )

    store.save_research_campaign_job(job)
    store.save_research_campaign_cell(
        cache_key=cache_key,
        strategy_fingerprint=cell.strategy_fingerprint,
        dataset_fingerprint="data_fp",
        engine_version=CAMPAIGN_ENGINE_VERSION,
        cell=cell,
    )

    assert store.load_research_campaign_job(job.id)["request_fingerprint"] == (
        job.request_fingerprint
    )
    assert store.load_research_campaign_cell(cache_key)["backtest_fingerprint"] == (
        cell.backtest_fingerprint
    )
    assert (
        campaign_cell_cache_key(
            strategy_fingerprint=cell.strategy_fingerprint,
            dataset_fingerprint="data_fp",
            dataset_id=cell.dataset_id,
            trial_count=32,
            research_configuration_fingerprint="config_fp",
        )
        != cache_key
    )


def test_research_trial_ledger_is_cumulative_idempotent_and_sealed(tmp_path):
    store = SqliteStore(tmp_path / "campaign.db")
    lineage_id = "lineage_test"

    first = store.register_research_campaign_experiment(
        lineage_id=lineage_id,
        experiment_id="experiment_one",
        campaign_id="campaign_one",
        engine_version=11,
        research_configuration_fingerprint="config_one",
        strategy_fingerprints=["strategy_a", "strategy_b"],
        blind_set_id="blindset_test",
    )
    replay = store.register_research_campaign_experiment(
        lineage_id=lineage_id,
        experiment_id="experiment_one",
        campaign_id="campaign_replay",
        engine_version=11,
        research_configuration_fingerprint="config_one",
        strategy_fingerprints=["strategy_a", "strategy_b"],
        blind_set_id="blindset_test",
    )
    second = store.register_research_campaign_experiment(
        lineage_id=lineage_id,
        experiment_id="experiment_two",
        campaign_id="campaign_two",
        engine_version=11,
        research_configuration_fingerprint="config_two",
        strategy_fingerprints=["strategy_b", "strategy_c"],
        blind_set_id="blindset_test",
    )
    third = store.register_research_campaign_experiment(
        lineage_id=lineage_id,
        experiment_id="experiment_three",
        campaign_id="campaign_three",
        engine_version=12,
        research_configuration_fingerprint="config_three",
        strategy_fingerprints=["strategy_a", "strategy_b"],
        blind_set_id="blindset_test",
    )

    assert first["cumulative_hypothesis_count"] == 2
    assert first["effective_trial_count"] == 2
    assert replay["replayed"] is True
    assert replay["effective_trial_count"] == first["effective_trial_count"]
    assert second["new_trial_variant_count"] == 1
    assert second["cumulative_hypothesis_count"] == 3
    assert second["effective_trial_count"] == 4
    assert third["new_trial_variant_count"] == 2
    assert third["cumulative_hypothesis_count"] == 5
    assert third["effective_trial_count"] == 7

    store.mark_research_lineage_blind_opened(
        lineage_id=lineage_id,
        blind_consumption_id="blind_evaluation",
    )
    with pytest.raises(RuntimeError, match="sealed"):
        store.register_research_campaign_experiment(
            lineage_id=lineage_id,
            experiment_id="experiment_four",
            campaign_id="campaign_four",
            engine_version=12,
            research_configuration_fingerprint="config_four",
            strategy_fingerprints=["strategy_d"],
            blind_set_id="blindset_test",
        )


def test_trial_ledger_recovers_blind_seal_if_process_stops_between_commits(tmp_path):
    store = SqliteStore(tmp_path / "campaign.db")
    store.register_research_campaign_experiment(
        lineage_id="lineage_crash_recovery",
        experiment_id="experiment_before_blind",
        campaign_id="campaign_before_blind",
        engine_version=12,
        research_configuration_fingerprint="config",
        strategy_fingerprints=["strategy_a"],
        blind_set_id="blindset_crash_recovery",
    )
    assert store.reserve_blind_set(
        blind_set_id="blindset_crash_recovery",
        strategy_fingerprint="strategy_a",
        evaluation_id="blind_evaluation_crash_recovery",
    )

    with pytest.raises(RuntimeError, match="sealed"):
        store.register_research_campaign_experiment(
            lineage_id="lineage_crash_recovery",
            experiment_id="experiment_after_blind",
            campaign_id="campaign_after_blind",
            engine_version=12,
            research_configuration_fingerprint="config_two",
            strategy_fingerprints=["strategy_b"],
            blind_set_id="blindset_crash_recovery",
        )

    lineage = store.load_research_campaign_lineage("lineage_crash_recovery")
    assert lineage is not None
    assert lineage["status"] == "blind_opened"
    assert lineage["blind_consumption_id"] == "blind_evaluation_crash_recovery"


def test_research_lineage_and_experiment_fingerprints_are_order_independent():
    first_dataset = SimpleNamespace(
        id="dataset_a",
        source_dataset_id="source_a",
        data_fingerprint="fingerprint_a",
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    second_dataset = SimpleNamespace(
        id="dataset_b",
        source_dataset_id="source_b",
        data_fingerprint="fingerprint_b",
        exchange="test",
        market_type="swap",
        symbol="ETH/USDT",
        timeframe="1h",
    )
    lineage = research_lineage_id([first_dataset, second_dataset])
    assert lineage == research_lineage_id([second_dataset, first_dataset])
    assert research_lineage_id([first_dataset], blind_set_id="blindset_one") == research_lineage_id(
        [first_dataset], blind_set_id="blindset_two"
    )
    assert research_experiment_fingerprint(
        lineage_id=lineage,
        engine_version=12,
        research_configuration_fingerprint="config",
        strategy_fingerprints=["b", "a", "a"],
    ) == research_experiment_fingerprint(
        lineage_id=lineage,
        engine_version=12,
        research_configuration_fingerprint="config",
        strategy_fingerprints=["a", "b"],
    )


def test_trial_family_cannot_be_reset_by_refetch_symbol_exchange_or_timeframe():
    original = SimpleNamespace(
        market_type="swap",
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        id="download_one",
        source_dataset_id=None,
        data_fingerprint="first",
    )
    refreshed_and_expanded = [
        SimpleNamespace(
            market_type="swap",
            exchange="bybit",
            symbol="ETH/USDT",
            timeframe="15m",
            id="download_two",
            source_dataset_id=None,
            data_fingerprint="second",
        )
    ]
    spot = SimpleNamespace(market_type="spot")

    assert research_trial_family_id([original]) == research_trial_family_id(
        refreshed_and_expanded
    )
    assert research_trial_family_id([original]) != research_trial_family_id([spot])


def test_trial_family_budget_accumulates_across_evidence_lineages(tmp_path):
    store = SqliteStore(tmp_path / "trial-family.db")
    family_id = "single_leg_swap_family"
    first = store.register_research_campaign_experiment(
        lineage_id="download_lineage_one",
        trial_family_id=family_id,
        experiment_id="experiment_one",
        campaign_id="campaign_one",
        engine_version=15,
        research_configuration_fingerprint="config_one",
        strategy_fingerprints=["strategy_a", "strategy_b"],
    )
    second = store.register_research_campaign_experiment(
        lineage_id="download_lineage_two",
        trial_family_id=family_id,
        experiment_id="experiment_two",
        campaign_id="campaign_two",
        engine_version=15,
        research_configuration_fingerprint="config_two",
        strategy_fingerprints=["strategy_b", "strategy_c"],
    )
    third = store.register_research_campaign_experiment(
        lineage_id="download_lineage_three",
        trial_family_id=family_id,
        experiment_id="experiment_three",
        campaign_id="campaign_three",
        engine_version=15,
        research_configuration_fingerprint="config_three",
        strategy_fingerprints=["strategy_c", "strategy_d"],
    )
    replay_after_family_growth = store.register_research_campaign_experiment(
        lineage_id="download_lineage_two",
        trial_family_id=family_id,
        experiment_id="experiment_two",
        campaign_id="campaign_replay_after_growth",
        engine_version=15,
        research_configuration_fingerprint="config_two",
        strategy_fingerprints=["strategy_b", "strategy_c"],
    )
    replay = store.register_research_campaign_experiment(
        lineage_id="download_lineage_two",
        trial_family_id=family_id,
        experiment_id="experiment_two",
        campaign_id="campaign_replay",
        engine_version=15,
        research_configuration_fingerprint="config_two",
        strategy_fingerprints=["strategy_b", "strategy_c"],
    )

    assert first["effective_trial_count"] == 2
    assert second["cumulative_hypothesis_count"] == 3
    assert second["sequential_experiment_count"] == 2
    assert second["effective_trial_count"] == 4
    assert replay["effective_trial_count"] == 4
    assert replay["replayed"] is True
    assert third["effective_trial_count"] == 6
    assert replay_after_family_growth["cumulative_hypothesis_count"] == 3
    assert replay_after_family_growth["sequential_experiment_count"] == 2
    assert replay_after_family_growth["effective_trial_count"] == 4
    persisted = store.load_research_trial_family_experiment(
        family_id=family_id,
        experiment_id="experiment_two",
    )
    assert persisted is not None
    assert persisted["cumulative_hypothesis_count"] == 3
    assert persisted["effective_trial_count"] == 4


def test_blind_set_seal_can_be_opened_only_once(tmp_path):
    store = SqliteStore(tmp_path / "campaign.db")

    assert store.reserve_blind_set(
        blind_set_id="blindset_one",
        strategy_fingerprint="strategy_one",
        evaluation_id="evaluation_one",
    )
    assert not store.reserve_blind_set(
        blind_set_id="blindset_one",
        strategy_fingerprint="strategy_two",
        evaluation_id="evaluation_two",
    )
    store.complete_blind_set_consumption(
        evaluation_id="evaluation_one",
        campaign_id="campaign_one",
        passed=False,
    )

    consumption = store.load_blind_set_consumption("blindset_one")
    assert consumption is not None
    assert consumption["status"] == "completed"
    assert consumption["campaign_id"] == "campaign_one"
    assert consumption["passed"] == 0


def test_one_source_lineage_cannot_bind_a_second_blind_set(tmp_path):
    store = SqliteStore(tmp_path / "lineage-blind-binding.db")
    store.register_research_campaign_experiment(
        lineage_id="source_lineage",
        experiment_id="experiment_one",
        campaign_id="campaign_one",
        engine_version=1,
        research_configuration_fingerprint="config_one",
        strategy_fingerprints=["strategy_one"],
        blind_set_id="blindset_one",
    )

    with pytest.raises(RuntimeError, match="different blind set"):
        store.register_research_campaign_experiment(
            lineage_id="source_lineage",
            experiment_id="experiment_two",
            campaign_id="campaign_two",
            engine_version=1,
            research_configuration_fingerprint="config_two",
            strategy_fingerprints=["strategy_two"],
            blind_set_id="blindset_two",
        )


def test_campaign_reuses_complete_cell_cache(monkeypatch, tmp_path):
    store = SqliteStore(tmp_path / "campaign.db")
    monkeypatch.setattr(api, "store", store)
    candles = demo_candles(1200)
    dataset = build_research_dataset(
        candles,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store.save_research_dataset(dataset, candles)
    request = ResearchCampaignRequest(
        dataset_ids=[dataset.id],
        library_entry_ids=["builtin_rsi_14_30_70_v2"],
        max_backtests=1,
    )
    progress: list[tuple[int, int, int, int]] = []

    async def capture(completed, total, cache_hits, computed):
        progress.append((completed, total, cache_hits, computed))

    first_report = asyncio.run(api.execute_research_campaign(request, progress_callback=capture))
    first_final = progress[-1]
    progress.clear()
    replay_report = asyncio.run(api.execute_research_campaign(request, progress_callback=capture))

    assert first_final == (1, 1, 0, 1)
    assert progress[-1] == (1, 1, 1, 0)
    assert replay_report.research_experiment_id == first_report.research_experiment_id
    assert replay_report.effective_trial_count == first_report.effective_trial_count == 1
