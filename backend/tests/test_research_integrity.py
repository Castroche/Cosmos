from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.domain import Candle
from cosmos_quant.research import build_research_dataset
from cosmos_quant.research_cache import RollingFeatureCube, SignalCache
from cosmos_quant.research_campaign import (
    CampaignCandidateAggregate,
    ResearchCampaignCell,
    ResearchCampaignReport,
    redact_campaign_for_research_agent,
)
from cosmos_quant.research_integrity import (
    OnlineFDRController,
    PermanentTrialRecord,
    ResearchDisposition,
    TrialResult,
    moving_block_expected_r,
)
from cosmos_quant.statistical_validation import BacktestOverfittingReport
from cosmos_quant.storage import SqliteStore


def test_expected_r_allows_low_win_rate_asymmetric_positive_expectancy():
    pnls = ([Decimal("500"), Decimal("-100"), Decimal("-100")] * 12) + [Decimal("-100")] * 4
    evidence = moving_block_expected_r(
        pnls,
        [Decimal("100")] * len(pnls),
        samples=500,
        seed_material="low-win-positive-r",
    )

    assert Decimal(12) / Decimal(40) < Decimal("0.50")
    assert evidence.testable is True
    assert evidence.expected_r > 0
    assert evidence.confidence_lower_bound > 0
    assert evidence.passed is True


def test_expected_r_rejects_high_win_rate_negative_expectancy_and_missing_risk():
    negative = moving_block_expected_r(
        [Decimal("10")] * 36 + [Decimal("-200")] * 4,
        [Decimal("100")] * 40,
        samples=500,
        seed_material="high-win-negative-r",
    )
    missing_risk = moving_block_expected_r(
        [Decimal("10")] * 20,
        [Decimal("0")] * 20,
        samples=500,
    )

    assert negative.expected_r < 0
    assert negative.passed is False
    assert missing_risk.testable is False


def test_permanent_trial_is_idempotent_and_survives_store_restart(tmp_path):
    path = tmp_path / "trials.db"
    store = SqliteStore(path)
    trial = PermanentTrialRecord.empirical(
        research_event_id="event_1",
        campaign_id="campaign_1",
        hypothesis_id="hypothesis_1",
        family_id="family_1",
        candidate_id="candidate_1",
        dataset_fingerprint="dataset_fp",
        validation_fingerprint="validation_fp",
        artifact_fingerprint="artifact_fp",
    )

    first = store.reserve_research_trial(trial)
    replay = store.reserve_research_trial(trial.model_copy(update={"id": "trial_replay"}))
    completed = store.append_research_trial_result(
        first["id"],
        result=str(TrialResult.PASS),
        p_value=Decimal("0.001"),
        adjusted_p_value=Decimal("0.01"),
        dsr=Decimal("0.95"),
        pbo=Decimal("0.10"),
    )

    restarted = SqliteStore(path)
    persisted = restarted.load_research_trial(first["id"])
    assert replay["id"] == first["id"]
    assert completed["result"] == "pass"
    assert persisted is not None
    assert persisted["p_value"] == "0.001"
    assert len(restarted.list_research_trials(family_id="family_1")) == 1


def test_online_fdr_allocation_is_persistent_idempotent_and_cannot_roll_back(tmp_path):
    path = tmp_path / "fdr.db"
    store = SqliteStore(path)
    controller = OnlineFDRController(store)
    first = controller.allocate_alpha(
        "global",
        scope="global",
        idempotency_key="candidate-validation-0001",
    )
    replay = controller.allocate_alpha(
        "global",
        scope="global",
        idempotency_key="candidate-validation-0001",
    )
    snapshot = controller.record_result(first, p_value=Decimal("0.0001"))

    restarted = OnlineFDRController(SqliteStore(path))
    second = restarted.allocate_alpha(
        "global",
        scope="global",
        idempotency_key="candidate-validation-0002",
    )
    assert replay.sequence == first.sequence
    assert snapshot.rejection_sequences == [1]
    assert second.sequence == 2
    assert second.alpha > Decimal("0")
    with pytest.raises(RuntimeError, match="cannot roll back"):
        restarted.restore(snapshot.model_copy(update={"next_sequence": 1}))


def test_point_in_time_metadata_is_persisted_and_closed_bar_availability_is_derived(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(3)
    ]
    dataset = build_research_dataset(
        candles,
        exchange="test",
        market_type="spot",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store = SqliteStore(tmp_path / "pit.db")
    store.save_research_dataset(dataset, candles)

    loaded = store.load_research_dataset_candles(dataset.id)
    assert loaded[0]["event_time"] == start.isoformat()
    assert loaded[0]["available_time"] == (start + timedelta(hours=1)).isoformat()
    assert loaded[0]["revision_policy"] == "append_only_closed_bar"


def test_autonomous_research_schema_migration_is_additive_and_integrity_checked(tmp_path):
    store = SqliteStore(tmp_path / "migrations.db")
    with store.connect() as connection:
        migrations = dict(
            connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        )
        candle_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(research_dataset_candles)").fetchall()
        }
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert migrations[4] == "autonomous_research_v2_artifacts"
    assert migrations[5] == "autonomous_research_persistent_budgets"
    assert migrations[6] == "autonomous_research_novelty_index"
    assert {
        "event_time",
        "available_time",
        "source_time",
        "ingestion_time",
        "revision_policy",
    }.issubset(candle_columns)
    assert {
        "autonomous_research_events",
        "research_trials",
        "online_fdr_streams",
        "generator_envelopes",
        "certificate_revocations",
        "autonomous_research_budgets",
        "research_novelty_index",
    }.issubset(tables)
    assert store.integrity_check() == "ok"


def test_research_cache_namespaces_share_complete_artifact_identity_without_collision(
    tmp_path,
):
    path = tmp_path / "cache.db"
    store = SqliteStore(path)
    RollingFeatureCube(store).put("artifact-v2", {"features": [1, 2, 3]})
    SignalCache(store).put("artifact-v2", {"signals": [0, 1, 0]})
    restarted = SqliteStore(path)
    assert RollingFeatureCube(restarted).get("artifact-v2") == {"features": [1, 2, 3]}
    assert SignalCache(restarted).get("artifact-v2") == {"signals": [0, 1, 0]}
    with pytest.raises(RuntimeError, match="immutable"):
        SignalCache(restarted).put("artifact-v2", {"signals": [1, 1, 1]})


def test_research_agent_campaign_redaction_cannot_leak_blind_scope_or_metrics():
    development = _campaign_cell("development", "research")
    blind = _campaign_cell("blind-secret", "blind")
    candidate = _candidate()
    report = ResearchCampaignReport(
        id="campaign_redaction",
        engine_version=18,
        dataset_ids=["development", "blind-secret"],
        research_dataset_ids=["development"],
        blind_dataset_ids=["blind-secret"],
        blind_set_id="blindset_secret",
        blind_consumption_id="blinduse_secret",
        library_entry_ids=["strategy"],
        trial_count=2,
        cells=[development, blind],
        candidates=[candidate],
        pbo=BacktestOverfittingReport(
            candidate_count=2,
            fold_count=4,
            split_count=1,
            overfit_splits=0,
            probability_of_backtest_overfitting=Decimal("0"),
            passed=True,
        ),
        blind_selected_candidate_passed=False,
        blind_candidate=candidate,
        research_disposition=ResearchDisposition.REJECTED,
    )

    redacted = redact_campaign_for_research_agent(report)
    serialized = str(redacted)
    assert "blind-secret" not in serialized
    assert "blindset_secret" not in serialized
    assert "blinduse_secret" not in serialized
    assert redacted["blind_certification"]["outcome"] == "fail"
    assert len(redacted["cells"]) == 1


def _campaign_cell(dataset_id: str, role: str) -> ResearchCampaignCell:
    return ResearchCampaignCell(
        library_entry_id="strategy",
        strategy_fingerprint="strategy_fp",
        family="trend",
        archetype="test",
        dataset_id=dataset_id,
        exchange="test",
        symbol="BTC/USDT",
        timeframe="1h",
        total_trades=100,
        return_percent=Decimal("1"),
        stress_return_percent=Decimal("1"),
        profit_factor=Decimal("1.2"),
        win_rate_percent=Decimal("40"),
        payoff_ratio=Decimal("2"),
        max_drawdown_percent=Decimal("5"),
        robustness_passed=True,
        statistical_uncertainty_passed=True,
        temporal_stability_passed=True,
        backtest_fingerprint=f"backtest_{dataset_id}",
        dataset_role=role,
        expected_r=Decimal("0.2"),
        expected_r_confidence_lower_bound=Decimal("0.05"),
        expected_r_testable=True,
        expected_r_passed=True,
    )


def _candidate() -> CampaignCandidateAggregate:
    return CampaignCandidateAggregate(
        library_entry_id="strategy",
        strategy_fingerprint="strategy_fp",
        family="trend",
        archetype="test",
        market_count=3,
        robust_market_count=3,
        positive_stress_market_count=3,
        robust_market_ratio=Decimal("1"),
        positive_stress_ratio=Decimal("1"),
        median_return_percent=Decimal("1"),
        median_profit_factor=Decimal("1.2"),
        median_win_rate_percent=Decimal("40"),
        median_payoff_ratio=Decimal("2"),
        worst_drawdown_percent=Decimal("5"),
        cross_market_passed=True,
        rank_score=Decimal("1"),
        median_expected_r=Decimal("0.2"),
        median_expected_r_confidence_lower_bound=Decimal("0.05"),
    )
