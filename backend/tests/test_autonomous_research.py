from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import numpy as np
import pytest
from cosmos_quant.autonomous_research import (
    AutonomousResearchMode,
    AutonomousResearchRunRequest,
    CandidateOrigin,
    DeadlineContext,
    FastGateEngine,
    MarketSnapshotV2,
    ResearchDisposition,
    ShadowEvidence,
    SlowCertificationEngine,
    SlowCertificationInput,
    generate_research_proposals,
)
from cosmos_quant.backtest import run_canonical_ir_exact_replay
from cosmos_quant.build_identity import RuntimeBuildIdentity
from cosmos_quant.research_certificates import (
    CertificateRevalidationContext,
    CertificateRevocationEvent,
    ExecutionEligibilityGrant,
    GeneratorEnvelopeCertificate,
    GeneratorProvenance,
    auto_revoke_certificate,
)
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_dsl import (
    DSLNode,
    ResearchHypothesisV2,
    SignalDefinition,
    StrategyDSLV2,
    compile_strategy_dsl,
)


def _identity(*, dirty: bool = False) -> RuntimeBuildIdentity:
    return RuntimeBuildIdentity(
        git_commit_sha="a" * 40,
        source_tree_hash="b" * 64,
        working_tree_dirty=dirty,
        captured_at=datetime.now(UTC),
    )


def _hypothesis() -> ResearchHypothesisV2:
    return ResearchHypothesisV2(
        id="hyp_fast_trend",
        family_id="family_fast_trend",
        observation="Closed-bar prices exhibit persistent positive directional strength.",
        economic_rationale="Persistent demand imbalance creates short-lived trend continuation.",
        causal_mechanism="Closed-bar price strength attracts delayed execution over the next bars.",
        prediction="The following bars should retain positive net risk-adjusted expectancy.",
        expected_direction="long",
        intended_regime=["trend"],
        holding_horizon="1-8 bars",
        required_features=["close"],
        falsification_criteria=["net expected R confidence bound is not positive"],
        invalidation_conditions=["directional persistence disappears"],
        created_at=datetime(2026, 8, 22, tzinfo=UTC),
    )


def _dsl() -> StrategyDSLV2:
    return StrategyDSLV2(
        hypothesis_id="hyp_fast_trend",
        family="trend",
        evaluation_clock="closed_bar",
        required_features=["close"],
        intended_regime=["trend"],
        holding_horizon="1-8 bars",
        nodes=[
            DSLNode(
                id="long",
                operator="greater_than",
                inputs=["close"],
                parameters={"scalar": 1},
            ),
            DSLNode(
                id="stop",
                operator="rolling_std",
                inputs=["close"],
                parameters={"window": 8},
            ),
        ],
        signals=SignalDefinition(long="long"),
        risk={
            "stop_distance": "stop",
            "risk_budget_fraction": "0.005",
            "maximum_holding_bars": 8,
        },
        invalidation=["maximum holding period or stop"],
    )


def _snapshot() -> MarketSnapshotV2:
    close = np.exp(np.linspace(np.log(100), np.log(400), 240))
    event = np.arange(240, dtype=np.int64)
    return MarketSnapshotV2(
        id="snapshot_fast",
        dataset_fingerprint="d" * 64,
        dataset_schema_version=9,
        availability_policy="closed_bar_v1",
        market_scope=("binance:BTC/USDT:1h",),
        timeframe_seconds=3600,
        features={
            "open": close.tolist(),
            "high": (close * 1.001).tolist(),
            "low": (close * 0.999).tolist(),
            "close": close.tolist(),
            "volume": np.full(240, 1_000_000.0).tolist(),
        },
        event_time_ns=event.tolist(),
        available_time_ns=(event + 1).tolist(),
        decision_time_ns=(event + 1).tolist(),
        ready_at=datetime.now(UTC),
    )


def _request(origin: CandidateOrigin, provenance=None) -> AutonomousResearchRunRequest:
    return AutonomousResearchRunRequest(
        idempotency_key=f"request-{origin}-0000000001",
        research_event_id="market-event-20260822-0001",
        hypothesis=_hypothesis(),
        dsl=_dsl(),
        origin=origin,
        snapshot=_snapshot(),
        validation_config_fingerprint="v" * 64,
        generator_provenance=provenance,
        transaction_cost_bps=Decimal("0"),
        market_state_valid=True,
        current_spread_bps=Decimal("1"),
        maximum_spread_bps=Decimal("10"),
        liquidity_passed=True,
        portfolio_evidence={
            "component_expected_return": "1",
            "marginal_sharpe": "1",
            "cvar_after": "0.05",
            "cvar_limit": "0.10",
            "maximum_drawdown_after": "0.05",
            "maximum_drawdown_limit": "0.10",
            "signal_overlap": "0.2",
            "pnl_overlap": "0.1",
            "trade_overlap": "0.2",
            "exposure_overlap": "0.2",
        },
    )


def test_free_dsl_is_shadow_only_and_public_result_has_no_performance_metrics(tmp_path):
    store = SqliteStore(tmp_path / "research.db")
    result = FastGateEngine(store, _identity()).run(_request(CandidateOrigin.FREE_DSL))
    assert result.disposition == ResearchDisposition.SHADOW_ONLY
    assert "free_dsl_requires_shadow" in result.reason_codes
    payload = result.model_dump(mode="json")
    assert not {"pnl", "sharpe", "expected_r", "blind_candidate"}.intersection(payload)
    assert len(store.list_research_trials()) == 1


def test_crypto_5m_cost_turnover_gate_rejects_before_empirical_return_trial(tmp_path):
    store = SqliteStore(tmp_path / "research.db")
    gate_names = (
        "trend",
        "volatility",
        "liquidity",
        "funding",
        "open_interest",
        "basis",
        "order_flow",
    )
    dsl = _dsl().model_copy(
        update={
            "regime_gates": {name: "long" for name in gate_names},
            "risk": _dsl().risk.model_copy(update={"maximum_holding_bars": 6}),
        }
    )
    snapshot = _snapshot().model_copy(
        update={
            "timeframe_seconds": 300,
            "market_scope": (
                "binance:BTC/USDT",
                "binance:ETH/USDT",
                "binance:SOL/USDT",
                "okx:BTC/USDT",
                "okx:ETH/USDT",
                "okx:SOL/USDT",
            ),
        }
    )
    request = _request(CandidateOrigin.FREE_DSL).model_copy(
        update={
            "idempotency_key": "crypto-cost-gate-00000001",
            "research_profile": "crypto_5m_v1",
            "dsl": dsl,
            "snapshot": snapshot,
            "model_artifact_ids": ("linear", "gbdt", "ensemble"),
            "transaction_cost_bps": Decimal("8"),
        }
    )

    result = FastGateEngine(store, _identity()).run(request)

    assert result.disposition == ResearchDisposition.REJECTED
    assert {
        "round_trip_cost_above_0_25r",
        "portfolio_turnover_above_six_per_day",
    }.intersection(result.reason_codes)
    assert store.list_research_trials() == []
    assert any("return_data_accessed\":\"false" in item for item in result.evidence_references)


def test_certified_generator_can_receive_one_bar_low_risk_grant(tmp_path):
    store = SqliteStore(tmp_path / "research.db")
    identity = _identity()
    ir = compile_strategy_dsl(_dsl())
    envelope = GeneratorEnvelopeCertificate(
        generator_id="certified_trend",
        generator_version="1",
        policy_version="policy-v1",
        allowed_families=("trend",),
        allowed_operators=("greater_than", "rolling_std"),
        maximum_nodes=8,
        maximum_lookback=100,
        build_identity=identity,
    )
    hypothesis_hash = sha256(
        json.dumps(
            _hypothesis().model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    generation = {
        "generator_id": envelope.generator_id,
        "generator_version": envelope.generator_version,
        "policy_version": envelope.policy_version,
        "hypothesis_hash": hypothesis_hash,
        "random_seed": 7,
        "output_ir_hash": ir.fingerprint,
    }
    provenance = GeneratorProvenance(
        **generation,
        generation_fingerprint=sha256(
            json.dumps(generation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        envelope_fingerprint=envelope.fingerprint,
    )
    result = FastGateEngine(store, identity, envelope=envelope).run(
        _request(CandidateOrigin.CERTIFIED_GENERATOR, provenance)
    )
    assert result.disposition == ResearchDisposition.EXECUTION_ELIGIBLE
    status = store.load_research_certificate_status(result.eligibility_grant_id)
    grant = ExecutionEligibilityGrant.model_validate(status["certificate"])
    assert grant.expires_at - grant.issued_at == timedelta(hours=1)
    assert grant.risk_budget_fraction <= Decimal("0.001")
    revocation = CertificateRevocationEvent(
        certificate_id=grant.id,
        reason_code="spread_model_changed",
        evidence_reference="market-state:1",
    )
    store.revoke_research_certificate(revocation)
    assert store.load_research_certificate_status(grant.id)["revoked"]


def test_certificate_revalidation_appends_revocation_without_mutating_certificate(tmp_path):
    store = SqliteStore(tmp_path / "certificate.db")
    grant = ExecutionEligibilityGrant.issue(
        strategy_ir_hash="i" * 64,
        artifact_fingerprint="a" * 64,
        generator_envelope_fingerprint="e" * 64,
        target_timeframe_seconds=3600,
        requested_risk_budget=Decimal("0.001"),
        mature_reference_risk_budget=Decimal("0.02"),
        build_identity=_identity(),
    )
    store.save_research_certificate(grant, "execution_eligibility_grant")
    original = store.load_research_certificate_status(grant.id)["certificate"]
    event = auto_revoke_certificate(
        store,
        certificate_id=grant.id,
        context=CertificateRevalidationContext(
            compiler_unchanged=True,
            feature_semantics_unchanged=True,
            data_source_unchanged=True,
            cost_model_unchanged=True,
            risk_model_unchanged=True,
            regime_valid=True,
            spread_within_limit=False,
            slippage_within_limit=True,
            forward_evidence_valid=True,
        ),
        evidence_reference="market-state:spread",
    )
    status = store.load_research_certificate_status(grant.id)
    assert event is not None and "spread_deteriorated" in event.reason_code
    assert status["certificate"] == original
    assert status["revoked"]


def test_exact_ir_replay_applies_latency_costs_stops_and_partial_fill():
    request = _request(CandidateOrigin.FREE_DSL)
    ir = compile_strategy_dsl(request.dsl)
    from cosmos_quant.autonomous_research import _snapshot_candles

    candles = [
        candle.model_copy(update={"volume": Decimal("0.0001")})
        for candle in _snapshot_candles(request.snapshot)
    ]
    replay = run_canonical_ir_exact_replay(
        ir,
        candles,
        feature_overrides={
            name: np.asarray(values) for name, values in request.snapshot.features.items()
        },
        fee_bps=Decimal("4"),
        spread_bps=Decimal("2"),
        slippage_bps=Decimal("2"),
    )
    assert replay.trades
    assert all(trade.entry_index > 0 for trade in replay.trades)
    assert all(trade.initial_risk_amount > 0 for trade in replay.trades)
    assert all(trade.fees > 0 and trade.slippage_cost > 0 for trade in replay.trades)
    assert all(trade.partial_fill for trade in replay.trades)


def test_dirty_build_never_receives_fast_eligibility(tmp_path):
    store = SqliteStore(tmp_path / "research.db")
    result = FastGateEngine(store, _identity(dirty=True)).run(
        _request(CandidateOrigin.CERTIFIED_GENERATOR)
    )
    assert result.disposition != ResearchDisposition.EXECUTION_ELIGIBLE
    with pytest.raises(ValueError, match="dirty build identity"):
        GeneratorEnvelopeCertificate(
            generator_id="dirty",
            generator_version="1",
            policy_version="1",
            allowed_families=("trend",),
            allowed_operators=("greater_than",),
            maximum_nodes=4,
            maximum_lookback=20,
            build_identity=_identity(dirty=True),
        )


def test_deadline_commits_untestable_without_refunding_trial_budget(tmp_path):
    store = SqliteStore(tmp_path / "research.db")
    result = FastGateEngine(store, _identity()).run(
        _request(CandidateOrigin.FREE_DSL),
        deadline=DeadlineContext(total_seconds=0),
    )
    assert result.disposition == ResearchDisposition.UNTESTABLE
    assert result.deadline_exceeded
    assert "deadline_exceeded" in result.reason_codes


def test_slow_certification_requires_evidence_not_wall_clock():
    engine = SlowCertificationEngine()
    insufficient = engine.evaluate(
        ShadowEvidence(
            effective_sample_size=29,
            independent_signal_episodes=100,
            trade_count=100,
            regime_coverage=("trend",),
            stress_coverage=("high_vol",),
            cost_error_bps=1,
            slippage_error_bps=1,
            signal_decay_ratio=Decimal("0.9"),
            calibrated=True,
        )
    )
    assert not insufficient.passed
    assert "ess_below_30" in insufficient.reason_codes


def test_slow_independent_evidence_issues_full_strategy_certificate():
    engine = SlowCertificationEngine()
    evidence = ShadowEvidence(
        effective_sample_size=50,
        independent_signal_episodes=45,
        trade_count=60,
        regime_coverage=("trend", "range"),
        stress_coverage=("high_vol", "wide_spread"),
        cost_error_bps=1,
        slippage_error_bps=1,
        signal_decay_ratio=Decimal("0.9"),
        calibrated=True,
        cross_market_passed=True,
        purged_walk_forward_passed=True,
        embargo_passed=True,
        out_of_fold_passed=True,
        block_bootstrap_passed=True,
        deflated_sharpe_probability=Decimal("0.95"),
        pbo_upper_bound=Decimal("0.10"),
        online_fdr_passed=True,
        cost_stress_passed=True,
        parameter_stability_passed=True,
        independent_blind_reference="sealed-audit:blind-1",
        forward_evidence_reference="shadow-forward:1",
    )
    certificate = engine.certify(
        strategy_ir_hash="i" * 64,
        artifact_fingerprint="a" * 64,
        slow_validation_fingerprint="s" * 64,
        evidence=evidence,
        generator_provenance=None,
        build_identity=_identity(),
    )
    assert certificate.independent_blind_evidence_reference == "sealed-audit:blind-1"
    assert certificate.forward_shadow_evidence_reference == "shadow-forward:1"


def test_slow_engine_computes_5000_sample_cross_market_evidence():
    engine = SlowCertificationEngine()
    market_pnls = [Decimal("500"), Decimal("-100"), Decimal("-100"), Decimal("-100")] * 10
    evidence, decision = engine.evaluate_empirical(
        SlowCertificationInput(
            market_pnls={"BTC": market_pnls, "ETH": market_pnls},
            market_initial_risks={
                "BTC": [Decimal("100")] * 40,
                "ETH": [Decimal("100")] * 40,
            },
            purged_oof_expected_rs=(
                Decimal("0.1"),
                Decimal("0.2"),
                Decimal("0.1"),
                Decimal("0.2"),
            ),
            stressed_expected_rs=(Decimal("0.05"), Decimal("0.04")),
            parameter_variant_expected_rs=(Decimal("0.08"), Decimal("0.12")),
            embargo_bars=8,
            independent_signal_episodes=60,
            trade_count=80,
            regime_coverage=("trend", "range"),
            stress_coverage=("high_vol", "wide_spread"),
            cost_error_bps=Decimal("1"),
            slippage_error_bps=Decimal("1"),
            signal_decay_ratio=Decimal("0.9"),
            calibrated=True,
            online_fdr_passed=True,
            independent_blind_reference="sealed-audit:blind-2",
            forward_evidence_reference="shadow-forward:2",
        ),
        artifact_fingerprint="slow-artifact-v2",
    )
    assert evidence.block_bootstrap_passed
    assert evidence.cross_market_passed
    assert decision.passed


def test_gateway_fault_injection_degrades_to_certified_generator_without_trading_side_effects():
    async def failed_gateway():
        raise TimeoutError("injected LLM gateway timeout")

    outcome = asyncio.run(
        generate_research_proposals(
            failed_gateway,
            lambda: [{"generator_id": "certified_trend", "mutation": "bounded"}],
        )
    )
    assert outcome.mode == AutonomousResearchMode.DEGRADED_AGENT
    assert outcome.proposals[0]["generator_id"] == "certified_trend"
    assert outcome.reason_codes == ("llm_gateway_unavailable",)
