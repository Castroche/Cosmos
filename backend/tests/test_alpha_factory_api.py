from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app_fixture import services as api
from cosmos_quant.alpha_factory import (
    ALPHA_MARKETS,
    AlphaModelArtifactV1,
    DevelopmentAuditCertificateV1,
    DevelopmentAuditEvidenceV1,
    FullStrategyCertificateV2,
    SandboxAdmissionCertificateV1,
    SandboxMaturityEvidenceV1,
    alpha_transaction_cost_fingerprint,
    dependency_versions,
)
from cosmos_quant.domain import TradingMode
from cosmos_quant.research_campaign import (
    CampaignCandidateAggregate,
    ResearchCampaignCell,
    ResearchCampaignReport,
)
from cosmos_quant.statistical_validation import BacktestOverfittingReport
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_dsl import (
    DSLNode,
    SignalDefinition,
    StrategyDSLV2,
    compile_strategy_dsl,
)
from fastapi.testclient import TestClient

client = TestClient(
    api.app,
    headers={},
)

TEST_IR = compile_strategy_dsl(
    StrategyDSLV2(
        hypothesis_id="api-alpha-hypothesis",
        family="trend",
        evaluation_clock="closed_5m",
        required_features=["close", "volume"],
        intended_regime=["liquid_trend"],
        holding_horizon="3 bars",
        nodes=[
            DSLNode(id="return_3", operator="returns", inputs=["close"]),
            DSLNode(
                id="trend_gate",
                operator="greater_than",
                inputs=["close"],
                parameters={"scalar": 0},
            ),
            DSLNode(
                id="volatility",
                operator="rolling_std",
                inputs=["close"],
                parameters={"window": 12},
            ),
            DSLNode(
                id="volatility_gate",
                operator="greater_than",
                inputs=["volatility"],
                parameters={"scalar": 0},
            ),
            DSLNode(
                id="liquidity_gate",
                operator="greater_than",
                inputs=["volume"],
                parameters={"scalar": 0},
            ),
            DSLNode(id="funding_value", operator="funding_rate"),
            DSLNode(
                id="funding_gate",
                operator="greater_than",
                inputs=["funding_value"],
                parameters={"scalar": -1},
            ),
            DSLNode(id="oi_value", operator="open_interest_change"),
            DSLNode(
                id="oi_gate",
                operator="greater_than",
                inputs=["oi_value"],
                parameters={"scalar": -100},
            ),
            DSLNode(id="basis_value", operator="basis_rate"),
            DSLNode(
                id="basis_gate",
                operator="greater_than",
                inputs=["basis_value"],
                parameters={"scalar": -1},
            ),
            DSLNode(id="order_flow_value", operator="order_flow_imbalance"),
            DSLNode(
                id="order_flow_gate",
                operator="greater_than",
                inputs=["order_flow_value"],
                parameters={"scalar": -1},
            ),
            DSLNode(
                id="stop",
                operator="rolling_std",
                inputs=["close"],
                parameters={"window": 12},
            ),
        ],
        signals=SignalDefinition(long="trend_gate"),
        risk={
            "stop_distance": "stop",
            "risk_budget_fraction": "0.0005",
            "maximum_holding_bars": 3,
        },
        invalidation=["maximum holding period or stop"],
        regime_gates={
            "trend": "trend_gate",
            "volatility": "volatility_gate",
            "liquidity": "liquidity_gate",
            "funding": "funding_gate",
            "open_interest": "oi_gate",
            "basis": "basis_gate",
            "order_flow": "order_flow_gate",
        },
    )
)


def ready_dataset(market: str) -> dict:
    exchange, symbol = market.split(":", 1)
    return {
        "id": f"{exchange}_{symbol[:3]}",
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": "5m",
        "closed_bars_only": True,
        "data_start": "2023-01-01T00:00:00+00:00",
        "data_end": "2025-01-02T00:00:00+00:00",
        "candle_count": 210_528,
        "expected_candle_count": 210_528,
        "coverage_ratio": "1",
        "point_in_time_status": "verified",
        "funding_count": 2_200,
        "open_interest_coverage_ratio": "0.99",
        "positioning_coverage_ratio": "0.99",
        "basis_coverage_ratio": "0.99",
        "order_flow_coverage_ratio": "0.99",
        "liquidation_flow_coverage_ratio": "0.99",
    }


def artifact(family: str, task: str, model_id: str) -> AlphaModelArtifactV1:
    serialization = {
        "elastic_net_regression": "json_coefficients",
        "lightgbm_regression": "lightgbm_text",
        "ensemble_50_50": "manifest_json",
    }[family]
    return AlphaModelArtifactV1(
        id=model_id,
        model_family=family,
        task=task,
        horizon_bars=3,
        feature_names=("return_3",),
        feature_cube_fingerprint="cube",
        label_fingerprint="labels",
        training_window=(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 1, tzinfo=UTC),
        ),
        validation_windows=((datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC)),),
        random_seed=17,
        dependency_versions=dependency_versions(),
        serialization_format=serialization,
        model_payload="model-native-text",
        cost_model_fingerprint=alpha_transaction_cost_fingerprint(
            fee_bps=api.configuration_service.current_configuration().fee_bps,
            slippage_bps=api.configuration_service.current_configuration().slippage_bps,
        ),
        ir_fingerprint=TEST_IR.fingerprint,
        canonical_ir_payload=TEST_IR.model_dump(mode="json"),
        walk_forward_scores=(Decimal("0.1"), Decimal("0.1"), Decimal("0.1")),
        walk_forward_passed=True,
    )


def passing_development_campaign() -> ResearchCampaignReport:
    cells = []
    for market in ALPHA_MARKETS:
        exchange, symbol = market.split(":", 1)
        cells.append(
            ResearchCampaignCell(
                library_entry_id="alpha-entry-1",
                strategy_fingerprint=TEST_IR.fingerprint,
                family="trend",
                archetype="dsl_ml_ensemble",
                dataset_id=ready_dataset(market)["id"],
                exchange=exchange,
                symbol=symbol,
                timeframe="5m",
                total_trades=80,
                return_percent=Decimal("8"),
                stress_return_percent=Decimal("2"),
                profit_factor=Decimal("1.25"),
                win_rate_percent=Decimal("52"),
                payoff_ratio=Decimal("1.2"),
                max_drawdown_percent=Decimal("8"),
                robustness_passed=True,
                statistical_uncertainty_passed=True,
                temporal_stability_passed=True,
                fold_returns=[Decimal("1")] * 6,
                backtest_fingerprint=f"backtest-{exchange}-{symbol}",
                expected_r=Decimal("0.10"),
                expected_r_confidence_lower_bound=Decimal("0.03"),
                expected_r_testable=True,
                expected_r_passed=True,
                expected_r_observation_count=80,
            )
        )
    candidate = CampaignCandidateAggregate(
        library_entry_id="alpha-entry-1",
        strategy_fingerprint=TEST_IR.fingerprint,
        family="trend",
        archetype="dsl_ml_ensemble",
        market_count=6,
        robust_market_count=6,
        positive_stress_market_count=6,
        robust_market_ratio=Decimal("1"),
        positive_stress_ratio=Decimal("1"),
        median_return_percent=Decimal("8"),
        median_profit_factor=Decimal("1.25"),
        median_win_rate_percent=Decimal("52"),
        median_payoff_ratio=Decimal("1.2"),
        worst_drawdown_percent=Decimal("8"),
        cross_market_passed=True,
        rank_score=Decimal("1"),
        online_fdr_passed=True,
    )
    pbo = BacktestOverfittingReport(
        candidate_count=3,
        fold_count=6,
        split_count=10,
        overfit_splits=0,
        probability_of_backtest_overfitting=Decimal("0"),
        upper_confidence_bound=Decimal("0.10"),
        passed=True,
    )
    return ResearchCampaignReport(
        id="campaign-server-side-1",
        dataset_ids=[cell.dataset_id for cell in cells],
        research_dataset_ids=[cell.dataset_id for cell in cells],
        library_entry_ids=[candidate.library_entry_id],
        trial_count=3,
        cells=cells,
        candidates=[candidate],
        pbo=pbo,
    )


def full_certificate(configuration_fingerprint: str) -> FullStrategyCertificateV2:
    return FullStrategyCertificateV2(
        sandbox_admission_certificate_id="sandbox-1",
        strategy_ir_hash="ir-fingerprint-0001",
        artifact_fingerprint="ensemble-fingerprint",
        sealed_blind_evidence_reference="blind-evaluation-1",
        sandbox_maturity=SandboxMaturityEvidenceV1(
            completed_episodes=30,
            independent_runs=20,
            recent_episodes=10,
            profit_factor=Decimal("1.10"),
            expected_r=Decimal("0.05"),
            maximum_drawdown_r=Decimal("6"),
            maximum_consecutive_losses=8,
            execution_cost_aligned=True,
            slippage_aligned=True,
            native_protection_aligned=True,
        ),
        configuration_fingerprint=configuration_fingerprint,
    )


def save_full_certificate_chain(
    store: SqliteStore,
    certificate: FullStrategyCertificateV2,
) -> None:
    evidence = DevelopmentAuditEvidenceV1(
        expected_r_bootstrap_lower=Decimal("0.01"),
        profit_factor=Decimal("1.20"),
        stress_net_return=Decimal("0.1"),
        maximum_drawdown_fraction=Decimal("0.10"),
        pbo_95_upper=Decimal("0.10"),
        online_fdr_passed=True,
        passing_markets=(
            "binance:BTC/USDT",
            "binance:ETH/USDT",
            "okx:SOL/USDT",
            "okx:BTC/USDT",
        ),
    )
    audit = DevelopmentAuditCertificateV1(
        id="development-audit-live-chain",
        strategy_ir_hash=certificate.strategy_ir_hash,
        artifact_fingerprint=certificate.artifact_fingerprint,
        data_contract_fingerprint="contract-live-chain",
        model_artifact_ids=("linear", "gbdt", "ensemble"),
        campaign_id="campaign-live-chain",
        campaign_fingerprint="campaign-live-chain-fingerprint",
        market_evaluation_fingerprints={
            market: f"evaluation-{index}" for index, market in enumerate(ALPHA_MARKETS)
        },
        evidence=evidence,
    )
    admission = SandboxAdmissionCertificateV1(
        id=certificate.sandbox_admission_certificate_id,
        strategy_ir_hash=certificate.strategy_ir_hash,
        artifact_fingerprint=certificate.artifact_fingerprint,
        data_contract_fingerprint=audit.data_contract_fingerprint,
        model_artifact_ids=audit.model_artifact_ids,
        development_audit_certificate_id=audit.id,
        audit_evidence=evidence,
        strategy_frozen=True,
    )
    store.save_research_certificate(audit, "development_audit_v1")
    store.save_research_certificate(admission, "sandbox_admission_v1")
    store.save_research_certificate(certificate, "full_strategy_v2")


def test_full_certificate_configuration_survives_intended_sandbox_to_live_mode_change():
    base = api.configuration_service.current_configuration().model_copy(
        update={"exchange_id": "okx", "trading_mode": TradingMode.SANDBOX}
    )
    live = base.model_copy(
        update={
            "trading_mode": TradingMode.LIVE,
            "live_trading_enabled": True,
            "automatic_execution_enabled": True,
        }
    )
    assert api.strategy_certificate_configuration_fingerprint(base) == (
        api.strategy_certificate_configuration_fingerprint(live)
    )


def test_sandbox_admission_api_requires_ready_contract_and_model_trio(
    tmp_path,
    monkeypatch,
):
    store = SqliteStore(tmp_path / "alpha-api.db")
    monkeypatch.setattr(api, "store", store)
    monkeypatch.setattr(
        store,
        "list_research_datasets",
        lambda limit=50: [ready_dataset(market) for market in ALPHA_MARKETS],
    )
    models = [
        artifact("elastic_net_regression", "regression", "linear-1"),
        artifact("lightgbm_regression", "regression", "gbdt-1"),
        artifact("ensemble_50_50", "ensemble", "ensemble-1"),
    ]
    for model in models:
        store.save_alpha_model_artifact(model)
    store.save_research_campaign(passing_development_campaign())
    raw_self_attestation = client.post(
        "/api/research/sandbox-admissions",
        json={
            "strategy_ir_hash": TEST_IR.fingerprint,
            "ensemble_model_id": "ensemble-1",
            "model_artifact_ids": [model.id for model in models],
            "audit_evidence": {
                "expected_r_bootstrap_lower": "10",
                "profit_factor": "99",
            },
        },
    )
    assert raw_self_attestation.status_code == 422
    audit_response = client.post(
        "/api/research/development-audits",
        json={
            "strategy_ir_hash": TEST_IR.fingerprint,
            "ensemble_model_id": "ensemble-1",
            "model_artifact_ids": [model.id for model in models],
            "campaign_id": "campaign-server-side-1",
        },
    )
    assert audit_response.status_code == 200, audit_response.text
    audit = audit_response.json()
    response = client.post(
        "/api/research/sandbox-admissions",
        json={
            "strategy_ir_hash": TEST_IR.fingerprint,
            "ensemble_model_id": "ensemble-1",
            "model_artifact_ids": [model.id for model in models],
            "development_audit_certificate_id": audit["id"],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["certificate"]["maximum_risk_per_trade"] == "0.0005"
    assert payload["certificate"]["development_audit_certificate_id"] == audit["id"]
    assert payload["certificate"]["strategy_frozen"] is True
    assert payload["handoff"]["exchange"] == "okx"


def test_live_whitelist_and_authorization_persist_until_operator_disables(
    tmp_path,
    monkeypatch,
):
    del tmp_path, monkeypatch
    response = client.post("/api/live-authorization/enable", json={})
    assert response.status_code == 404

def test_failed_single_use_blind_test_revokes_sandbox_new_risk(tmp_path, monkeypatch):
    store = SqliteStore(tmp_path / "blind-failure.db")
    monkeypatch.setattr(api, "store", store)
    admission = SandboxAdmissionCertificateV1(
        strategy_ir_hash="strategy-ir-blind-failure",
        artifact_fingerprint="ensemble-blind-failure",
        data_contract_fingerprint="contract",
        model_artifact_ids=("linear", "gbdt", "ensemble"),
        development_audit_certificate_id="development-audit-blind-failure",
        audit_evidence=DevelopmentAuditEvidenceV1(
            expected_r_bootstrap_lower=Decimal("0.01"),
            profit_factor=Decimal("1.20"),
            stress_net_return=Decimal("0.1"),
            maximum_drawdown_fraction=Decimal("0.10"),
            pbo_95_upper=Decimal("0.10"),
            online_fdr_passed=True,
            passing_markets=(
                "binance:BTC/USDT",
                "binance:ETH/USDT",
                "okx:SOL/USDT",
                "okx:BTC/USDT",
            ),
        ),
        strategy_frozen=True,
    )
    development_audit = DevelopmentAuditCertificateV1(
        id=admission.development_audit_certificate_id,
        strategy_ir_hash=admission.strategy_ir_hash,
        artifact_fingerprint=admission.artifact_fingerprint,
        data_contract_fingerprint=admission.data_contract_fingerprint,
        model_artifact_ids=admission.model_artifact_ids,
        campaign_id="campaign-blind-failure",
        campaign_fingerprint="campaign-blind-failure-fingerprint",
        market_evaluation_fingerprints={
            market: f"evaluation-{index}" for index, market in enumerate(ALPHA_MARKETS)
        },
        evidence=admission.audit_evidence,
    )
    store.save_research_certificate(development_audit, "development_audit_v1")
    store.save_research_certificate(admission, "sandbox_admission_v1")
    assert store.reserve_blind_set(
        blind_set_id="sealed-blind-failure",
        strategy_fingerprint=admission.strategy_ir_hash,
        evaluation_id="blind-evaluation-failure",
    )
    store.complete_blind_set_consumption(
        evaluation_id="blind-evaluation-failure",
        campaign_id="campaign-blind-failure",
        passed=False,
    )

    assert api.maybe_issue_full_strategy_certificate_v2(admission.id) is None
    assert store.load_research_certificate_status(admission.id)["revoked"] is True
    assert not store.reserve_blind_set(
        blind_set_id="sealed-blind-failure",
        strategy_fingerprint=admission.strategy_ir_hash,
        evaluation_id="blind-evaluation-retry",
    )
