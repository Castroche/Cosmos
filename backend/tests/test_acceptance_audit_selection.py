import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from scripts.acceptance_audit import (  # noqa: E402
    _campaign_job_replay_key,
    _campaign_profitability_evidence,
    _canonical_campaign_key,
    _causal_policy_specs_from_report,
    _qualified_materialization_group,
    _select_campaign_replay_job,
)


def test_causal_campaign_replay_reconstructs_frozen_policy_hypotheses():
    report = SimpleNamespace(
        causal_policy_definitions=[
            {
                "policy_id": "selective_v1",
                "maximum_candidates_per_bar": 3,
                "abstain_on_ambiguous_regime": True,
                "minimum_regime_confidence": "0.55",
                "minimum_signal_confidence": "0.65",
                "minimum_directional_consensus": 2,
                "priors": [{"id": "prior_a"}, {"id": "prior_b"}],
            }
        ]
    )

    specs = _causal_policy_specs_from_report(report)

    assert len(specs) == 1
    assert specs[0].id == "selective_v1"
    assert specs[0].library_entry_ids == ["prior_a", "prior_b"]
    assert specs[0].minimum_regime_confidence == Decimal("0.55")
    assert specs[0].minimum_signal_confidence == Decimal("0.65")
    assert specs[0].minimum_directional_consensus == 2


def test_acceptance_derives_champion_metrics_from_development_and_blind_evidence():
    candidate = SimpleNamespace(
        library_entry_id="champion",
        strategy_fingerprint="fp_champion",
        positive_stress_ratio=Decimal("0.80"),
        robust_market_ratio=Decimal("0.70"),
        temporal_stable_market_ratio=Decimal("0.60"),
        cross_market_passed=True,
        unique_exchange_count=2,
        unique_symbol_count=3,
        median_win_rate_percent=Decimal("55"),
        median_payoff_ratio=Decimal("1.60"),
        median_profit_factor=Decimal("1.40"),
        pooled_win_rate_lower_bound_percent=Decimal("47"),
        multiple_testing_adjusted_p_value=Decimal("0.01"),
    )
    campaign = SimpleNamespace(
        selected_library_entry_id="champion",
        autonomous_execution_eligible=True,
        execution_certificate=object(),
        candidates=[candidate],
        blind_candidate=candidate,
        portfolio_allocations=[
            {"library_entry_id": "champion", "weight": "0.35"}
        ],
    )

    evidence = _campaign_profitability_evidence(campaign)

    assert evidence["cost_stress_passed"] is True
    assert evidence["parameter_stable"] is True
    assert evidence["cross_market"] is True
    assert evidence["win_payoff_passed"] is True
    assert evidence["qualified_portfolio"] is False


def test_point_in_time_materialization_accepts_one_verified_sealed_split_pair():
    as_of = datetime(2024, 1, 1, tzinfo=UTC)
    boundary = datetime(2025, 1, 1, tzinfo=UTC)
    common = {
        "gap_count": 0,
        "invalid_count": 0,
        "blind_set_id": "blindset_test",
        "source_dataset_id": "source_test",
        "split_boundary": boundary,
        "embargo_bars": 42,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    development = SimpleNamespace(
        **common,
        research_role="development",
        candle_count=5000,
        data_start=as_of,
        data_end=boundary - timedelta(days=8),
        blind_sealed=False,
    )
    blind = SimpleNamespace(
        **common,
        research_role="blind",
        candle_count=1200,
        data_start=boundary,
        data_end=datetime(2026, 1, 1, tzinfo=UTC),
        blind_sealed=True,
    )

    qualified = _qualified_materialization_group(
        [development, blind],
        required_bars=6000,
        minimum_coverage_ratio=Decimal("0.95"),
        as_of=as_of,
    )

    assert qualified == [development, blind]


def test_canonical_campaign_prefers_sealed_full_matrix_over_new_exploration():
    now = datetime.now(UTC)
    full = SimpleNamespace(
        autonomous_execution_eligible=False,
        blind_consumption_id=None,
        blind_dataset_ids=["blind"] * 6,
        dataset_ids=["dataset"] * 12,
        library_entry_ids=["strategy"] * 100,
        cells=[
            SimpleNamespace(
                dataset_role="research",
                data_eligible=True,
                exchange="binance" if index % 2 else "okx",
            )
            for index in range(600)
        ],
        created_at=now,
    )
    exploration = SimpleNamespace(
        autonomous_execution_eligible=False,
        blind_consumption_id=None,
        blind_dataset_ids=[],
        dataset_ids=["dataset"] * 6,
        library_entry_ids=["strategy"] * 8,
        cells=[
            SimpleNamespace(
                dataset_role="research",
                data_eligible=True,
                exchange="binance",
            )
            for _ in range(24)
        ],
        created_at=now + timedelta(hours=1),
    )

    assert max([full, exploration], key=_canonical_campaign_key) is full


def test_listed_but_unopened_blind_ids_do_not_replace_full_cross_exchange_report():
    now = datetime.now(UTC)
    full = SimpleNamespace(
        autonomous_execution_eligible=False,
        blind_consumption_id=None,
        dataset_ids=["development"] * 6,
        library_entry_ids=["strategy"] * 90,
        cells=[
            SimpleNamespace(
                dataset_role="research",
                data_eligible=True,
                exchange="binance" if index % 2 else "okx",
            )
            for index in range(540)
        ],
        created_at=now,
    )
    unopened = SimpleNamespace(
        autonomous_execution_eligible=False,
        blind_consumption_id=None,
        dataset_ids=["development"] * 6 + ["blind"] * 6,
        library_entry_ids=["strategy"] * 9,
        cells=[
            SimpleNamespace(
                dataset_role="research",
                data_eligible=True,
                exchange="binance",
            )
            for _ in range(54)
        ],
        created_at=now + timedelta(hours=1),
    )

    assert max([full, unopened], key=_canonical_campaign_key) is full


def test_campaign_replay_prefers_broad_completed_job_over_new_small_job():
    now = datetime.now(UTC)
    full = SimpleNamespace(
        total_cells=600,
        request={
            "max_backtests": 606,
            "dataset_ids": ["development"] * 6,
            "blind_dataset_ids": ["blind"] * 6,
        },
        updated_at=now,
    )
    exploration = SimpleNamespace(
        total_cells=24,
        request={"max_backtests": 24, "dataset_ids": ["development"] * 6},
        updated_at=now + timedelta(hours=1),
    )

    assert max([full, exploration], key=_campaign_job_replay_key) is full


def test_campaign_replay_binds_to_canonical_report_job_before_matrix_size():
    now = datetime.now(UTC)
    old_broad = SimpleNamespace(
        report_id="old_campaign",
        total_cells=600,
        request={"max_backtests": 606, "dataset_ids": ["binance"] * 6},
        updated_at=now,
    )
    canonical = SimpleNamespace(
        report_id="canonical_campaign",
        total_cells=540,
        request={"max_backtests": 540, "dataset_ids": ["cross_exchange"] * 6},
        updated_at=now + timedelta(hours=1),
    )

    selected = _select_campaign_replay_job(
        [old_broad, canonical],
        SimpleNamespace(
            id="canonical_campaign",
            trial_count=540,
            research_dataset_ids=["cross_exchange"] * 6,
            blind_dataset_ids=[],
        ),
    )

    assert selected is canonical


def test_campaign_replay_matches_direct_report_to_equivalent_job_matrix():
    now = datetime.now(UTC)
    obsolete = SimpleNamespace(
        report_id="old_campaign",
        total_cells=600,
        request={"max_backtests": 606, "dataset_ids": ["binance"] * 6},
        updated_at=now,
    )
    equivalent = SimpleNamespace(
        report_id="source_campaign",
        total_cells=540,
        request={
            "max_backtests": 540,
            "dataset_ids": ["cross_a", "cross_b"],
            "blind_dataset_ids": [],
        },
        updated_at=now,
    )
    direct_report = SimpleNamespace(
        id="direct_replay_without_job",
        trial_count=540,
        research_dataset_ids=["cross_b", "cross_a"],
        blind_dataset_ids=[],
    )

    selected = _select_campaign_replay_job(
        [obsolete, equivalent],
        direct_report,
    )

    assert selected is equivalent
