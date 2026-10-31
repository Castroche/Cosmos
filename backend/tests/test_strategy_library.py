import asyncio
from decimal import Decimal

import pytest
from app_fixture import services as api
from cosmos_quant.carry import CARRY_STRATEGY_LIBRARY
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.demo import demo_candles
from cosmos_quant.relative_value import PAIR_STRATEGY_LIBRARY
from cosmos_quant.research import build_research_dataset
from cosmos_quant.research_campaign import historical_feature_eligibility
from cosmos_quant.research_lifecycle import CandidateStatus, StrategyCandidate, create_candidate
from cosmos_quant.signals import score_market_state
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_library import (
    infer_required_data,
    list_strategy_library,
    rank_strategy_library,
    seed_strategy_library_candidates,
    strategy_library_summary,
)
from cosmos_quant.strategy_spec import validate_strategy_spec
from fastapi import HTTPException


def test_builtin_strategy_library_is_non_empty_and_covers_every_family():
    entries = list_strategy_library()
    summary = strategy_library_summary()

    assert summary["non_empty"] is True
    assert len(entries) >= 90
    assert {entry.family for entry in entries} == set(StrategyKind)
    assert summary["archetype_count"] >= 30
    assert all(entry.source_url.startswith("https://") for entry in entries)
    assert all(validate_strategy_spec(entry.spec, entry.family) for entry in entries)
    assert len({entry.validated_spec.fingerprint for entry in entries}) == len(entries)
    assert sum("standard_horizon" in entry.id for entry in entries) == 30
    composite = next(item for item in entries if item.id == "builtin_multifactor_roc_24_v1")
    assert set(composite.required_data) == {
        "ohlcv",
        "open_interest",
        "funding_rate",
        "order_flow",
    }
    crypto_native = [
        item
        for item in entries
        if item.id.startswith("builtin_")
        and any(
            feature in item.required_data
            for feature in {"funding_rate", "open_interest", "positioning", "basis", "order_flow"}
        )
    ]
    assert len(crypto_native) >= 20
    assert len({item.archetype for item in crypto_native}) >= 15


def test_mean_reversion_exit_priors_are_structural_and_predeclared():
    entries = [item for item in list_strategy_library() if "mean_touch_exit" in item.id]

    assert len(entries) == 12
    assert all(item.family == StrategyKind.MEAN_REVERSION for item in entries)
    assert all(item.required_data == ["ohlcv"] for item in entries)
    assert all(item.expected_profile == "selective_high_quality" for item in entries)
    for entry in entries:
        parameters = entry.validated_spec.parameters
        assert parameters.mean_completion_mode == "mean_touch"
        assert parameters.first_target_fraction in {
            Decimal("0.60"),
            Decimal("0.75"),
            Decimal("0.80"),
        }
        assert parameters.maximum_holding_bars is not None
        assert parameters.trailing_stop_atr is not None
        assert parameters.trailing_activation == "first_target"
    break_even = [entry for entry in entries if "breakeven" in entry.id]
    assert len(break_even) == 4
    assert all(
        entry.validated_spec.parameters.break_even_after_first_target
        for entry in break_even
    )


def test_confirmed_statistical_reversal_priors_are_structural_and_predeclared():
    entries = [
        item
        for item in list_strategy_library()
        if "zscore_" in item.id and "reentry_minimum_r" in item.id
    ]

    assert len(entries) == 4
    assert all(item.family == StrategyKind.MEAN_REVERSION for item in entries)
    assert all(item.required_data == ["ohlcv"] for item in entries)
    assert all(item.expected_profile == "selective_high_quality" for item in entries)
    assert all(
        item.validated_spec.parameters.entry_model == "zscore_reentry"
        for item in entries
    )
    assert all(
        item.validated_spec.parameters.mean_completion_mode == "minimum_reward"
        for item in entries
    )
    assert all(
        item.validated_spec.parameters.first_target_reward_risk
        >= Decimal("1.30")
        for item in entries
    )
    assert all(
        item.validated_spec.parameters.maximum_holding_bars is not None
        for item in entries
    )


def test_offline_campaign_preflight_uses_only_development_data_availability():
    candles = demo_candles(300)
    datasets = [
        build_research_dataset(
            candles,
            exchange=exchange,
            market_type="swap",
            symbol=symbol,
            timeframe="1h",
        )
        for exchange in ("binance", "okx")
        for symbol in ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    ]
    features = {dataset.id: ([], [], [], [], [], []) for dataset in datasets}

    selected: list[str] = []
    excluded: dict[str, str] = {}
    for entry in list_strategy_library():
        supported = []
        for dataset in datasets:
            funding, open_interest, positioning, basis, order_flow, liquidation_flow = (
                features[dataset.id]
            )
            reasons = historical_feature_eligibility(
                dataset=dataset,
                required_data=entry.required_data,
                funding=funding,
                open_interest=open_interest,
                positioning=positioning,
                basis=basis,
                order_flow=order_flow,
                liquidation_flow=liquidation_flow,
            )
            if not reasons:
                supported.append(dataset)
        if len({item.exchange for item in supported}) >= 2 and len(
            {item.symbol for item in supported}
        ) >= 3:
            selected.append(entry.id)
        else:
            excluded[entry.id] = "missing_required_historical_features"

    entries = {entry.id: entry for entry in list_strategy_library()}
    assert selected
    assert all(entries[entry_id].required_data == ["ohlcv"] for entry_id in selected)
    assert "builtin_multifactor_roc_24_v1" in excluded
    assert "builtin_roc_24_liquidation_confirm_v1" in excluded
    assert "builtin_ema_12_26_trend_v2" in selected


def test_unified_research_library_exposes_offline_single_pair_and_carry_priors():
    payload = api.unified_research_strategy_library()

    assert payload["counts"] == {
        "single_leg": len(list_strategy_library()),
        "pair_research": len(PAIR_STRATEGY_LIBRARY),
        "carry_research": len(CARRY_STRATEGY_LIBRARY),
    }
    assert payload["entry_count"] == sum(payload["counts"].values())
    assert payload["profitability_claim"] is False
    assert {
        "single_leg_research_only",
        "pair_research_only",
        "spot_perpetual_research_only",
    } <= {entry["scope"] for entry in payload["entries"]}

    carry = asyncio.run(api.builtin_strategy_library("carry"))
    assert carry["counts"]["carry_research"] == len(CARRY_STRATEGY_LIBRARY)
    assert carry["counts"]["single_leg"] == 0
    assert all(entry["execution_eligible"] is False for entry in carry["entries"])

    with pytest.raises(HTTPException, match="strategy family must be"):
        asyncio.run(api.builtin_strategy_library("unknown"))


def test_library_ranking_uses_current_regime_without_backtest_results():
    assessment = score_market_state(demo_candles(300))
    ranked = rank_strategy_library(list_strategy_library(), assessment)

    assert len(ranked) == len(list_strategy_library())
    assert ranked[0][1]["score"] >= ranked[-1][1]["score"]
    assert all(item[1]["role"] == "pre_backtest_prior_only" for item in ranked)


def test_agent_strategy_data_dependencies_are_inferred_from_parameters():
    assert infer_required_data(
        {
            "parameters": {
                "entry_model": "roc_momentum",
                "open_interest_mode": "confirm_expansion",
                "funding_mode": "avoid_paying_extreme",
                "order_flow_mode": "confirm_direction",
            }
        }
    ) == ["funding_rate", "ohlcv", "open_interest", "order_flow"]


def test_agent_strategy_data_dependencies_ignore_serialized_inactive_defaults():
    spec = validate_strategy_spec(
        {
            "family": "mean_reversion",
            "parameters": {
                "entry_model": "zscore_rsi",
                "mean_window": 20,
            },
        }
    )

    assert infer_required_data(spec.model_dump(mode="json")) == ["ohlcv"]


def test_agent_strategy_data_dependencies_follow_entry_model_and_active_filters():
    assert infer_required_data(
        {
            "parameters": {
                "entry_model": "positioning_contrarian",
                "funding_mode": "ignore",
                "basis_mode": "avoid_crowded_direction",
                "order_flow_mode": "confirm_direction",
            }
        }
    ) == ["basis", "ohlcv", "order_flow", "positioning"]
    assert infer_required_data(
        {
            "parameters": {
                "entry_model": "roc_momentum",
                "liquidation_flow_mode": "confirm_direction",
            }
        }
    ) == ["liquidation_flow", "ohlcv"]


def test_liquidation_context_priors_are_filters_not_entry_generators():
    entries = [
        item
        for item in list_strategy_library()
        if "liquidation" in item.id
    ]

    assert len(entries) == 4
    assert all(item.required_data == ["ohlcv", "liquidation_flow"] for item in entries)
    assert all(
        item.validated_spec.parameters.liquidation_flow_mode
        in {"confirm_direction", "avoid_opposed_extreme"}
        for item in entries
    )
    assert all(
        item.validated_spec.parameters.entry_model
        in {"roc_momentum", "donchian_volume", "ema_crossover", "bollinger_rsi"}
        for item in entries
    )


def test_library_ranking_demotes_hypotheses_with_missing_current_data():
    assessment = score_market_state(demo_candles(300))
    ranked = rank_strategy_library(
        list_strategy_library(StrategyKind.MEAN_REVERSION),
        assessment,
        {"ohlcv"},
    )
    positioning = next(
        item for item in ranked if item[0].id == "builtin_positioning_contrarian_15_v1"
    )
    basis = next(item for item in ranked if item[0].id == "builtin_basis_contrarian_15bp_v1")

    assert positioning[1]["score"] <= -100
    assert positioning[1]["missing_data"] == ["positioning"]
    assert basis[1]["missing_data"] == ["basis"]


def test_builtin_strategy_candidates_seed_idempotently(tmp_path):
    store = SqliteStore(tmp_path / "library.db")

    first = seed_strategy_library_candidates(store)
    second = seed_strategy_library_candidates(store)

    assert first == len(list_strategy_library())
    assert second == 0
    assert len(store.list_strategy_candidates(200)) == first


def test_seed_retires_superseded_builtin_candidate_without_deleting_audit(tmp_path):
    store = SqliteStore(tmp_path / "library.db")
    stale = create_candidate(list_strategy_library()[0].validated_spec)
    stale.id = "cand_builtin_legacy_v1"
    stale.decision_reasons = ["legacy"]
    store.save_strategy_candidate(stale)

    seeded = seed_strategy_library_candidates(store)
    retired = StrategyCandidate.model_validate(
        store.load_strategy_candidate(stale.id),
    )

    assert seeded == len(list_strategy_library())
    assert retired.status == CandidateStatus.RETIRED
    assert retired.decision_reasons == [
        "该内置模板版本已被当前公开策略库替代并自动归档",
        "历史候选仅保留审计记录，不再参与研究排序或执行",
    ]
