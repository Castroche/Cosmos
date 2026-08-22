import asyncio

from cosmos_quant import api
from cosmos_quant.acceptance import build_product_acceptance_report


def test_profitability_score_is_hard_gated_without_robust_champion():
    report = build_product_acceptance_report(
        {
            "latest_campaign_pbo_passed": True,
            "champion_cost_stress_passed": True,
            "champion_parameter_stable": True,
            "champion_cross_market": True,
            "champion_win_payoff_passed": True,
            "qualified_portfolio_present": True,
            "causal_live_evidence_passed": True,
            "robust_champion_present": False,
        }
    )

    profitability = next(
        item for item in report.dimensions if item.code == "profitability_evidence"
    )
    assert profitability.score == 0
    assert profitability.hard_gate_passed is False
    assert report.profitability_guaranteed is False
    assert report.strategy_certified is False
    assert report.strategy_certification_status == "hold"


def test_acceptance_scores_only_verified_checks():
    evidence = {
        "full_regression_passed": True,
        "latest_campaign_completed": True,
        "campaign_cache_complete": True,
        "campaign_datasets_verified": True,
        "research_process_isolated": True,
        "no_stuck_research_jobs": True,
    }

    report = build_product_acceptance_report(evidence)

    infrastructure = next(
        item for item in report.dimensions if item.code == "research_infrastructure"
    )
    assert infrastructure.score == 100
    assert report.engineering_score < 100
    assert report.capability_score < 100
    assert report.profitability_evidence_score == 0


def test_capability_score_excludes_strategy_discovery_outcomes():
    evidence = {
        "full_regression_passed": True,
        "latest_campaign_completed": True,
        "campaign_cache_complete": True,
        "campaign_datasets_verified": True,
        "research_process_isolated": True,
        "no_stuck_research_jobs": True,
        "market_sensing_complete": True,
        "strategy_library_present": True,
        "agent_candidates_present": True,
        "failure_revision_present": True,
        "independent_evaluator_present": True,
        "research_budget_enforced": True,
        "negative_strategy_memory": True,
        "revision_effective": False,
        "strategy_family_target_met": True,
        "honest_abstention": True,
        "core_universe_complete": True,
        "point_in_time_universe_complete": True,
        "survivorship_bias_controlled": True,
        "core_candles_verified": True,
        "funding_coverage_complete": True,
        "open_interest_coverage_complete": True,
        "multi_exchange_data": True,
        "crypto_native_depth_complete": True,
        "strategy_template_target_met": True,
        "campaign_matrix_complete": True,
        "latest_campaign_pbo_passed": False,
        "robust_champion_present": False,
        "strategy_certificate_enforced": True,
        "execution_reconciled": True,
        "idempotent_order_ledger": True,
        "native_protection_verified": True,
        "fault_injection_passed": True,
        "restart_recovery_passed": True,
    }

    report = build_product_acceptance_report(evidence)

    assert report.capability_score == 100
    assert report.strategy_certification_status == "hold"
    assert report.profitability_evidence_score == 0


def test_strategy_certification_requires_champion_pbo_and_enforcement():
    report = build_product_acceptance_report(
        {
            "robust_champion_present": True,
            "latest_campaign_pbo_passed": True,
            "strategy_certificate_enforced": True,
        }
    )

    assert report.strategy_certified is True
    assert report.strategy_certification_status == "certified"


def test_product_acceptance_endpoint_reads_verified_artifact(tmp_path, monkeypatch):
    report = build_product_acceptance_report({})
    artifact = tmp_path / "latest.json"
    artifact.write_text(report.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(api, "PRODUCT_ACCEPTANCE_REPORT_PATH", artifact)

    loaded = asyncio.run(api.product_acceptance())

    assert loaded == report
    assert loaded.profitability_guaranteed is False
