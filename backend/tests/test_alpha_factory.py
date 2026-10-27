from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest
from cosmos_quant.alpha_factory import (
    ALPHA_MARKETS,
    AlphaModelArtifactV1,
    CanonicalAlphaIRV1,
    DevelopmentAuditEvidenceV1,
    FullStrategyCertificateV2,
    LiveStrategySelection,
    SandboxMaturityEvidenceV1,
    assess_crypto_5m_contract,
    causal_asof_join,
    ensemble_signal,
    evaluate_cost_gate,
    frozen_net_return_labels,
    predict_alpha_ensemble,
    predict_alpha_from_feature_history,
    purged_walk_forward_splits,
    split_closed_bars_on_gaps,
    train_alpha_model_pair,
)
from cosmos_quant.alpha_pipeline import build_frozen_alpha_feature_cube
from cosmos_quant.alpha_runtime import build_causal_alpha_feature_history
from cosmos_quant.domain import Candle, MarketSnapshot
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_dsl import (
    DSLNode,
    SignalDefinition,
    StrategyDSLV2,
    compile_strategy_dsl,
)


def canonical_strategy_ir():
    return compile_strategy_dsl(
        StrategyDSLV2(
            hypothesis_id="alpha-training-hypothesis",
            family="trend",
            evaluation_clock="closed_5m",
            required_features=["close", "volume"],
            intended_regime=["liquid_trend"],
            holding_horizon="3 bars",
            nodes=[
                DSLNode(
                    id="return_3",
                    operator="returns",
                    inputs=["close"],
                ),
                DSLNode(
                    id="volatility_12",
                    operator="rolling_std",
                    inputs=["close"],
                    parameters={"window": 12},
                ),
                DSLNode(
                    id="order_flow_imbalance",
                    operator="order_flow_imbalance",
                ),
                DSLNode(
                    id="trend_gate",
                    operator="greater_than",
                    inputs=["close"],
                    parameters={"scalar": 0},
                ),
                DSLNode(
                    id="volatility_gate",
                    operator="greater_than",
                    inputs=["volatility_12"],
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
                DSLNode(
                    id="order_flow_gate",
                    operator="greater_than",
                    inputs=["order_flow_imbalance"],
                    parameters={"scalar": -1},
                ),
                DSLNode(
                    id="stop",
                    operator="rolling_std",
                    inputs=["close"],
                    parameters={"window": 12},
                ),
            ],
            signals=SignalDefinition(long="trend_gate", short=None),
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
    end = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "id": f"{exchange}_{symbol[:3]}_5m",
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": "5m",
        "closed_bars_only": True,
        "data_start": end - timedelta(days=731),
        "data_end": end,
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
        "largest_gap_bars": 0,
        "segment_count": 1,
    }


def test_crypto_5m_contract_requires_all_six_markets_and_full_feature_coverage():
    complete = assess_crypto_5m_contract([ready_dataset(market) for market in ALPHA_MARKETS])
    assert complete.status == "READY"
    assert len(complete.markets) == 6
    assert len(complete.fingerprint) == 64

    missing = assess_crypto_5m_contract([ready_dataset(ALPHA_MARKETS[0])])
    assert missing.status == "NOT_READY"

    datasets = [ready_dataset(market) for market in ALPHA_MARKETS]
    datasets[0]["order_flow_coverage_ratio"] = "0.90"
    untestable = assess_crypto_5m_contract(datasets)
    assert untestable.status == "UNTESTABLE"
    assert untestable.markets[0].status == "UNTESTABLE"


def test_canonical_ir_requires_every_explicit_regime_gate():
    gates = {
        name: {"operator": "gte", "threshold": "registered_feature"}
        for name in (
            "trend",
            "volatility",
            "liquidity",
            "funding",
            "open_interest",
            "basis",
            "order_flow",
        )
    }
    ir = CanonicalAlphaIRV1(
        hypothesis="Momentum persists only in a liquid trend regime.",
        feature_nodes=({"feature": "return_12"},),
        regime_gates=gates,
    )
    assert ir.horizons == (3, 6, 12)
    assert ir.position_access is False
    with pytest.raises(ValueError, match="missing regime gates"):
        CanonicalAlphaIRV1(
            hypothesis="Momentum persists only in a liquid trend regime.",
            feature_nodes=({"feature": "return_12"},),
            regime_gates={"trend": gates["trend"]},
        )


def test_labels_use_next_bar_entry_and_include_all_costs():
    close = np.arange(100.0, 120.0)
    labels = frozen_net_return_labels(
        close,
        entry_cost_fraction=0.001,
        exit_cost_fraction=0.002,
        impact_fraction=0.003,
    )
    expected = close[4] / close[1] - 1 - 0.006
    assert labels[3][0] == pytest.approx(expected)
    assert np.isnan(labels[12][-1])


def test_point_in_time_join_and_gap_segmentation_are_causal():
    joined = causal_asof_join(
        np.array([5, 10, 15, 20]),
        np.array([7, 15]),
        np.array([1.0, 2.0]),
    )
    assert np.isnan(joined[0])
    assert joined.tolist()[1:] == [1.0, 2.0, 2.0]

    seconds = 1_000_000_000
    bars = np.array([0, 300, 600, 900, 2_400], dtype=np.int64) * seconds
    segments = split_closed_bars_on_gaps(bars)
    assert [len(item) for item in segments] == [4, 1]


def test_runtime_feature_history_uses_closed_bars_available_time_and_fresh_snapshot():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal(str(100 + index / 100)),
            volume=Decimal("10"),
        )
        for index in range(400)
    ]
    decision = candles[-1].timestamp + timedelta(minutes=5)
    snapshot = MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=decision,
        bid=Decimal("103.98"),
        ask=Decimal("104.00"),
        last=Decimal("103.99"),
        funding_rate=Decimal("0.0001"),
        funding_time=decision,
        open_interest_change_24h=Decimal("0.02"),
        open_interest_time=decision,
        basis_rate=Decimal("0.001"),
        basis_time=decision,
        order_flow_imbalance=Decimal("0.25"),
        order_flow_time=decision,
    )
    tail = {
        "order_flow": [
            {
                "timestamp": (start + timedelta(minutes=5)).isoformat(),
                "available_time": (start + timedelta(minutes=10)).isoformat(),
                "base_volume": "10",
                "delta_base_volume": "2",
            },
            {
                "timestamp": decision.isoformat(),
                "available_time": (decision + timedelta(minutes=5)).isoformat(),
                "base_volume": "10",
                "delta_base_volume": "9",
            },
        ]
    }
    raw = build_causal_alpha_feature_history(candles, tail, snapshot, canonical_strategy_ir())
    assert raw["order_flow_imbalance"][-2] == pytest.approx(0.2)
    assert raw["order_flow_imbalance"][-1] == pytest.approx(0.25)

    stale = snapshot.model_copy(update={"order_flow_time": decision - timedelta(minutes=6)})
    with pytest.raises(ValueError, match="stale"):
        build_causal_alpha_feature_history(candles, tail, stale, canonical_strategy_ir())


def test_frozen_feature_cube_generates_all_horizons_without_crossing_data_gaps():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    candles = []
    for index in range(3_300):
        gap = timedelta(hours=2) if index >= 2_000 else timedelta(0)
        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * index) + gap,
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100") + Decimal(index) / Decimal("1000"),
                volume=Decimal("10"),
            )
        )
    cube = build_frozen_alpha_feature_cube(
        candles,
        {},
        canonical_strategy_ir(),
        feature_names=("return_3", "volatility_12"),
        dataset_fingerprint="immutable-dataset",
        fee_bps=Decimal("4"),
        slippage_bps=Decimal("2"),
    )
    assert set(cube.labels) == {3, 6, 12}
    assert np.isnan(cube.labels[12][1_995])
    assert np.isfinite(cube.labels[12][2_000])
    assert len(cube.fingerprint) == 64


def test_purged_walk_forward_embargo_and_ensemble_direction_gate():
    splits = purged_walk_forward_splits(8_000)
    assert splits
    for train, validation in splits:
        assert validation[0] - train[-1] == 13
    assert ensemble_signal(0.03, 0.02, estimated_round_trip_cost=0.01) == 1
    assert ensemble_signal(0.03, -0.02, estimated_round_trip_cost=0.001) == 0
    assert ensemble_signal(0.01, 0.01, estimated_round_trip_cost=0.006) == 0


def test_cost_gate_rejects_negative_edge_cost_and_turnover_before_backtest():
    evidence = evaluate_cost_gate(
        expected_gross_edge_r=Decimal("0.20"),
        estimated_round_trip_cost_r=Decimal("0.26"),
        estimated_entries_per_symbol_day=Decimal("2.1"),
        estimated_portfolio_entries_day=Decimal("6.1"),
    )
    assert evidence.accepted is False
    assert set(evidence.rejection_reasons) == {
        "non_positive_expected_net_edge",
        "round_trip_cost_above_0_25r",
        "symbol_turnover_above_two_per_day",
        "portfolio_turnover_above_six_per_day",
    }


def test_real_linear_and_lightgbm_training_uses_safe_native_artifacts():
    random = np.random.default_rng(17)
    features = random.normal(size=(3_200, 3))
    labels = (
        features[:, 0] * 0.003
        - features[:, 1] * 0.001
        + random.normal(
            scale=0.0005,
            size=3_200,
        )
    )
    start = datetime(2025, 1, 1, tzinfo=UTC)
    ir = canonical_strategy_ir()
    result = train_alpha_model_pair(
        features,
        labels,
        feature_names=("return_3", "volatility_12", "order_flow_imbalance"),
        horizon_bars=3,
        feature_cube_fingerprint="cube",
        label_fingerprint="labels",
        cost_model_fingerprint="cost",
        ir_fingerprint=ir.fingerprint,
        canonical_ir=ir,
        timestamps=tuple(start + timedelta(minutes=5 * index) for index in range(3_200)),
    )
    assert result.linear.serialization_format == "json_coefficients"
    assert result.gbdt.serialization_format == "lightgbm_text"
    assert "linear_model_id" in result.ensemble.model_payload
    assert result.linear.dependency_versions["scikit-learn"] != "unavailable"
    assert result.gbdt.dependency_versions["lightgbm"] != "unavailable"
    inference = predict_alpha_ensemble(
        result.linear,
        result.gbdt,
        result.ensemble,
        feature_values=dict(zip(result.linear.feature_names, features[-1], strict=True)),
        estimated_round_trip_cost=0.0001,
    )
    assert inference.horizon_bars == 3
    assert inference.signal in {-1, 0, 1}
    assert inference.ensemble_artifact_fingerprint == result.ensemble.fingerprint
    runtime_inference = predict_alpha_from_feature_history(
        result.linear,
        result.gbdt,
        result.ensemble,
        raw_features={
            "close": np.linspace(100, 130, 120),
            "volume": np.ones(120),
            "funding_rate": np.zeros(120),
            "open_interest_change": np.zeros(120),
            "basis_rate": np.zeros(120),
            "order_flow_imbalance": np.ones(120),
        },
        estimated_round_trip_cost=0.0001,
    )
    assert runtime_inference.canonical_ir_direction == 1
    assert runtime_inference.regime_gates_passed is True
    assert runtime_inference.signal in {0, 1}

    drifted = result.linear.model_copy(
        update={"dependency_versions": {**result.linear.dependency_versions, "numpy": "0.0"}}
    )
    with pytest.raises(ValueError, match="dependency version drift"):
        predict_alpha_ensemble(
            drifted,
            result.gbdt,
            result.ensemble,
            feature_values=dict(zip(result.linear.feature_names, features[-1], strict=True)),
            estimated_round_trip_cost=0.0001,
        )


def full_certificate() -> FullStrategyCertificateV2:
    maturity = SandboxMaturityEvidenceV1(
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
    )
    return FullStrategyCertificateV2(
        sandbox_admission_certificate_id="sandbox_1",
        strategy_ir_hash="ir_hash",
        artifact_fingerprint="artifact_hash",
        sealed_blind_evidence_reference="sealed_blind_once_1",
        sandbox_maturity=maturity,
        configuration_fingerprint="config_hash",
    )


def test_certificate_thresholds_and_storage_are_append_only(tmp_path):
    weak = DevelopmentAuditEvidenceV1(
        expected_r_bootstrap_lower=Decimal("0.01"),
        profit_factor=Decimal("1.20"),
        stress_net_return=Decimal("1"),
        maximum_drawdown_fraction=Decimal("0.10"),
        pbo_95_upper=Decimal("0.10"),
        online_fdr_passed=True,
        passing_markets=(
            "binance:BTC/USDT",
            "binance:ETH/USDT",
            "okx:BTC/USDT",
            "okx:ETH/USDT",
        ),
    )
    assert weak.passed is False  # SOL coverage is mandatory.

    store = SqliteStore(tmp_path / "alpha.db")
    artifact = AlphaModelArtifactV1(
        model_family="elastic_net_regression",
        task="regression",
        horizon_bars=3,
        feature_names=("return_3",),
        feature_cube_fingerprint="cube",
        label_fingerprint="labels",
        training_window=(datetime(2024, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)),
        validation_windows=((datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 2, 1, tzinfo=UTC)),),
        random_seed=17,
        dependency_versions={"scikit-learn": "1.7", "lightgbm": "4.6"},
        serialization_format="json_coefficients",
        model_payload='{"coefficients":[0.1]}',
        cost_model_fingerprint="cost",
        ir_fingerprint="ir",
    )
    store.save_alpha_model_artifact(artifact)
    assert store.load_alpha_model_artifact(artifact.id)["model_family"] == artifact.model_family

    certificate = full_certificate()
    store.save_research_certificate(certificate, "full_strategy_v2")
    selection = LiveStrategySelection(
        certificate_id=certificate.id,
        selected=True,
        selected_by="operator-a",
        certificate_fingerprint="certificate_hash",
    )
    store.save_live_strategy_selection(selection)
    assert store.list_full_strategy_certificates_v2()[0]["selection"]["selected"] is True
    with store.connect() as connection:
        version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    assert version == 10
