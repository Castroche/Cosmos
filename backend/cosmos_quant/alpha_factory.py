from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from typing import Literal
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, Field, model_validator

from .strategy_backends import NumpyFastVectorBackend
from .strategy_dsl import CanonicalStrategyIR

ALPHA_PROFILE = "crypto_5m_v1"
ALPHA_HORIZONS = (3, 6, 12)
ALPHA_MARKETS = (
    "binance:BTC/USDT",
    "binance:ETH/USDT",
    "binance:SOL/USDT",
    "okx:BTC/USDT",
    "okx:ETH/USDT",
    "okx:SOL/USDT",
)
REQUIRED_REGIME_GATES = (
    "trend",
    "volatility",
    "liquidity",
    "funding",
    "open_interest",
    "basis",
    "order_flow",
)

_REGIME_GATE_SEMANTIC_SOURCES = {
    "trend": {"open", "high", "low", "close", "returns", "log_returns", "roc"},
    "volatility": {"rolling_std", "realized_volatility", "atr"},
    "liquidity": {"volume", "order_flow_imbalance"},
    "funding": {"funding", "funding_rate"},
    "open_interest": {"open_interest", "open_interest_change"},
    "basis": {"basis", "basis_rate", "spread"},
    "order_flow": {"orderflow_imbalance", "order_flow_imbalance", "trade_imbalance"},
}


def _fingerprint(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode()).hexdigest()


def dependency_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("numpy", "scikit-learn", "lightgbm"):
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = "unavailable"
    return result


def alpha_transaction_cost_fingerprint(
    *,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    impact_model_version: str = "linear_volume_participation_v1",
) -> str:
    return _fingerprint(
        {
            "fee_bps": str(fee_bps),
            "slippage_bps": str(slippage_bps),
            "spread_source": "current_closed_5m_market_snapshot",
            "impact_model_version": impact_model_version,
            "round_trip": True,
        }
    )


def validate_crypto_5m_ir_semantics(ir: CanonicalStrategyIR) -> None:
    """Prove each named regime gate depends on its registered market evidence."""

    nodes = {node.id: node for node in ir.nodes}

    def lineage(reference: str, visited: set[str] | None = None) -> set[str]:
        visited = set(visited or ())
        if reference in visited:
            return set()
        visited.add(reference)
        node = nodes.get(reference)
        if node is None:
            return {reference}
        result = {node.operator}
        for source in node.inputs:
            result.update(lineage(source, visited))
        return result

    gates = dict(ir.regime_gates)
    missing = set(REQUIRED_REGIME_GATES) - gates.keys()
    if missing:
        raise ValueError(f"canonical IR is missing regime gates: {sorted(missing)}")
    invalid = {
        name: sorted(lineage(gates[name]))
        for name, accepted in _REGIME_GATE_SEMANTIC_SOURCES.items()
        if lineage(gates[name]).isdisjoint(accepted)
    }
    if invalid:
        raise ValueError(f"canonical IR regime gates lack semantic evidence: {invalid}")


def _aligned_label_cost(value: np.ndarray, count: int) -> np.ndarray | float:
    return value if value.ndim == 0 else value[1 : 1 + count]


class Crypto5mMarketReadiness(BaseModel, frozen=True):
    market: str
    status: Literal["READY", "NOT_READY", "UNTESTABLE"]
    dataset_id: str | None = None
    days: Decimal = Decimal("0")
    candle_count: int = 0
    expected_candle_count: int = 210_240
    completeness: Decimal = Decimal("0")
    largest_gap_bars: int = 0
    segment_count: int = 0
    required_feature_coverage: Decimal = Decimal("0")
    feature_coverage: dict[str, Decimal] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()


class Crypto5mDataContractV1(BaseModel, frozen=True):
    id: Literal["crypto_5m_v1"] = ALPHA_PROFILE
    version: Literal[1] = 1
    timeframe: Literal["5m"] = "5m"
    minimum_days: Literal[730] = 730
    minimum_completeness: Decimal = Decimal("0.999")
    minimum_required_feature_coverage: Decimal = Decimal("0.95")
    maximum_contiguous_gap_bars: Literal[3] = 3
    markets: tuple[Crypto5mMarketReadiness, ...]
    horizons: tuple[int, int, int] = ALPHA_HORIZONS
    required_features: tuple[str, ...] = REQUIRED_REGIME_GATES
    status: Literal["READY", "NOT_READY", "UNTESTABLE"]
    legacy_v1_read_only: Literal[True] = True
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"assessed_at"}))


class CanonicalAlphaIRV1(BaseModel, frozen=True):
    version: Literal[1] = 1
    hypothesis: str = Field(min_length=8, max_length=2_000)
    feature_nodes: tuple[dict, ...] = Field(min_length=1, max_length=128)
    regime_gates: dict[str, dict]
    horizons: tuple[int, int, int] = ALPHA_HORIZONS
    position_access: Literal[False] = False
    order_access: Literal[False] = False
    return_access: Literal[False] = False

    @model_validator(mode="after")
    def require_explicit_regime_gates(self):
        missing = set(REQUIRED_REGIME_GATES) - set(self.regime_gates)
        if missing:
            raise ValueError(f"canonical IR is missing regime gates: {sorted(missing)}")
        if tuple(self.horizons) != ALPHA_HORIZONS:
            raise ValueError("crypto_5m_v1 horizons are frozen at 3/6/12 bars")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json"))


class AlphaModelArtifactV1(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"alpha_model_{uuid4().hex}")
    profile: Literal["crypto_5m_v1"] = ALPHA_PROFILE
    model_family: Literal[
        "elastic_net_regression",
        "logistic_classification",
        "lightgbm_regression",
        "lightgbm_classification",
        "ensemble_50_50",
    ]
    task: Literal["regression", "classification", "ensemble"]
    horizon_bars: Literal[3, 6, 12]
    feature_names: tuple[str, ...]
    feature_cube_fingerprint: str
    label_fingerprint: str
    training_window: tuple[datetime, datetime]
    validation_windows: tuple[tuple[datetime, datetime], ...]
    purge_bars: int = Field(default=12, ge=12)
    embargo_bars: Literal[12] = 12
    random_seed: int
    dependency_versions: dict[str, str]
    serialization_format: Literal["json_coefficients", "lightgbm_text", "manifest_json"]
    model_payload: str
    cost_model_fingerprint: str
    ir_fingerprint: str
    canonical_ir_payload: dict | None = None
    regime_gates: tuple[str, ...] = REQUIRED_REGIME_GATES
    required_data_features: tuple[str, ...] = REQUIRED_REGIME_GATES
    prediction_scale: Decimal = Field(default=Decimal("1"), gt=0)
    walk_forward_scores: tuple[Decimal, ...] = ()
    walk_forward_passed: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def prohibit_pickle(self):
        lowered = self.serialization_format.lower() + self.model_payload[:100].lower()
        if "pickle" in lowered or "joblib" in lowered:
            raise ValueError("pickle/joblib model serialization is prohibited")
        if set(self.regime_gates) != set(REQUIRED_REGIME_GATES):
            raise ValueError("alpha artifacts require all seven canonical IR regime gates")
        if self.canonical_ir_payload is not None:
            ir = CanonicalStrategyIR.model_validate(self.canonical_ir_payload)
            if ir.fingerprint != self.ir_fingerprint:
                raise ValueError("canonical IR payload does not match ir_fingerprint")
            if set(dict(ir.regime_gates)) != set(REQUIRED_REGIME_GATES):
                raise ValueError("canonical IR payload is missing explicit regime gates")
            validate_crypto_5m_ir_semantics(ir)
            registered = set(ir.required_features) | {node.id for node in ir.nodes}
            if not set(self.feature_names).issubset(registered):
                raise ValueError("model feature_names are not registered in the Canonical IR")
            if ir.maximum_holding_bars != self.horizon_bars:
                raise ValueError("model horizon does not match the Canonical IR holding period")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"id", "created_at"}))


class DevelopmentAuditEvidenceV1(BaseModel, frozen=True):
    expected_r_bootstrap_lower: Decimal
    profit_factor: Decimal
    stress_net_return: Decimal
    maximum_drawdown_fraction: Decimal
    pbo_95_upper: Decimal
    online_fdr_passed: bool
    passing_markets: tuple[str, ...]

    @property
    def passed(self) -> bool:
        exchanges = {item.split(":", 1)[0] for item in self.passing_markets}
        coins = {item.split(":", 1)[1].split("/", 1)[0] for item in self.passing_markets}
        return (
            self.expected_r_bootstrap_lower > 0
            and self.profit_factor >= Decimal("1.15")
            and self.stress_net_return > 0
            and self.maximum_drawdown_fraction <= Decimal("0.20")
            and self.pbo_95_upper <= Decimal("0.20")
            and self.online_fdr_passed
            and len(set(self.passing_markets)) >= 4
            and coins == {"BTC", "ETH", "SOL"}
            and exchanges == {"binance", "okx"}
        )


class DevelopmentAuditCertificateV1(BaseModel, frozen=True):
    """Server-issued binding from persisted campaign cells to one model trio."""

    id: str = Field(default_factory=lambda: f"development_audit_{uuid4().hex}")
    strategy_ir_hash: str
    artifact_fingerprint: str
    data_contract_fingerprint: str
    model_artifact_ids: tuple[str, ...] = Field(min_length=3)
    campaign_id: str
    campaign_fingerprint: str
    market_evaluation_fingerprints: dict[str, str]
    evidence: DevelopmentAuditEvidenceV1
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def enforce_server_audit_scope(self):
        if set(self.market_evaluation_fingerprints) != set(ALPHA_MARKETS):
            raise ValueError("development audit must evaluate all six frozen markets")
        if not self.evidence.passed:
            raise ValueError("development audit evidence does not meet admission thresholds")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"id", "issued_at"}))


class SandboxAdmissionCertificateV1(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"sandbox_admission_{uuid4().hex}")
    strategy_ir_hash: str
    artifact_fingerprint: str
    data_contract_fingerprint: str
    model_artifact_ids: tuple[str, ...] = Field(min_length=2)
    development_audit_certificate_id: str = ""
    audit_evidence: DevelopmentAuditEvidenceV1
    exchange: Literal["okx"] = "okx"
    trading_mode: Literal["sandbox"] = "sandbox"
    maximum_risk_per_trade: Decimal = Decimal("0.0005")
    maximum_active_instances: Literal[1] = 1
    allow_scale_in: Literal[False] = False
    allow_pyramiding: Literal[False] = False
    allow_automatic_leverage_increase: Literal[False] = False
    strategy_frozen: bool = False
    blind_test_consumed: bool = False
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def enforce_admission(self):
        if not self.audit_evidence.passed:
            raise ValueError("development audit does not qualify for sandbox admission")
        if self.maximum_risk_per_trade > Decimal("0.0005"):
            raise ValueError("sandbox admission risk exceeds 0.05%")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"id", "issued_at"}))


class SandboxExecutionRoundEvidenceV1(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"sandbox_round_{uuid4().hex}")
    sandbox_certificate_id: str
    episode_id: str
    run_id: str
    expected_round_trip_cost_bps: Decimal = Field(ge=0)
    realized_round_trip_cost_bps: Decimal = Field(ge=0)
    expected_slippage_bps: Decimal = Field(ge=0)
    realized_slippage_bps: Decimal = Field(ge=0)
    native_protection_verified: bool
    reconciled: bool
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def aligned(self) -> bool:
        return (
            self.reconciled
            and self.native_protection_verified
            and self.realized_round_trip_cost_bps
            <= max(self.expected_round_trip_cost_bps * Decimal("1.25"), Decimal("1"))
            and self.realized_slippage_bps
            <= max(self.expected_slippage_bps * Decimal("1.25"), Decimal("1"))
        )


class SandboxMaturityEvidenceV1(BaseModel, frozen=True):
    completed_episodes: int
    independent_runs: int
    recent_episodes: int
    profit_factor: Decimal
    expected_r: Decimal
    maximum_drawdown_r: Decimal
    maximum_consecutive_losses: int
    execution_cost_aligned: bool
    slippage_aligned: bool
    native_protection_aligned: bool

    @property
    def passed(self) -> bool:
        return (
            self.completed_episodes >= 30
            and self.independent_runs >= 20
            and self.recent_episodes >= 10
            and self.profit_factor >= Decimal("1.10")
            and self.expected_r >= Decimal("0.05")
            and self.maximum_drawdown_r <= Decimal("6")
            and self.maximum_consecutive_losses <= 8
            and self.execution_cost_aligned
            and self.slippage_aligned
            and self.native_protection_aligned
        )


class FullStrategyCertificateV2(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"full_strategy_v2_{uuid4().hex}")
    sandbox_admission_certificate_id: str
    strategy_ir_hash: str
    artifact_fingerprint: str
    sealed_blind_evidence_reference: str
    sealed_blind_passed: Literal[True] = True
    sandbox_maturity: SandboxMaturityEvidenceV1
    configuration_fingerprint: str
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def enforce_full_certificate(self):
        if not self.sandbox_maturity.passed:
            raise ValueError("OKX sandbox maturity requirements are not met")
        return self

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.model_dump(mode="json", exclude={"id", "issued_at"}))


class LiveStrategySelection(BaseModel):
    certificate_id: str
    selected: bool = False
    selected_by: str
    certificate_fingerprint: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PersistentLiveAuthorizationGrant(BaseModel):
    id: str = Field(default_factory=lambda: f"persistent_live_{uuid4().hex}")
    enabled: bool
    operator: str
    account_fingerprint: str
    exchange: Literal["okx", "binance"]
    symbols: tuple[str, ...]
    timeframe: Literal["5m"] = "5m"
    entry_profile: str
    maximum_risk_per_trade: Decimal = Field(gt=0, le=Decimal("0.005"))
    maximum_daily_loss: Decimal = Field(gt=0, le=Decimal("0.02"))
    maximum_open_positions: int = Field(gt=0, le=3)
    scope_fingerprint: str
    # Empty only for a legacy persisted grant; revalidation revokes it rather
    # than failing to deserialize the append-only authorization ledger.
    strategy_selection_fingerprint: str = ""
    configuration_fingerprint: str
    authorized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    disabled_at: datetime | None = None
    disabled_reason: str | None = None


class CostGateEvidence(BaseModel, frozen=True):
    expected_gross_edge_r: Decimal
    estimated_round_trip_cost_r: Decimal
    estimated_entries_per_symbol_day: Decimal
    estimated_portfolio_entries_day: Decimal
    accepted: bool
    rejection_reasons: tuple[str, ...]


class AlphaTrainingResultV1(BaseModel, frozen=True):
    linear: AlphaModelArtifactV1
    gbdt: AlphaModelArtifactV1
    ensemble: AlphaModelArtifactV1
    linear_validation_score: Decimal
    gbdt_validation_score: Decimal


class AlphaInferenceResultV1(BaseModel, frozen=True):
    horizon_bars: Literal[3, 6, 12]
    linear_prediction: Decimal
    gbdt_prediction: Decimal
    blended_prediction: Decimal
    estimated_round_trip_cost: Decimal
    signal: Literal[-1, 0, 1]
    canonical_ir_direction: Literal[-1, 0, 1] = 0
    regime_gates_passed: bool = False
    linear_artifact_fingerprint: str
    gbdt_artifact_fingerprint: str
    ensemble_artifact_fingerprint: str


def evaluate_cost_gate(
    *,
    expected_gross_edge_r: Decimal,
    estimated_round_trip_cost_r: Decimal,
    estimated_entries_per_symbol_day: Decimal,
    estimated_portfolio_entries_day: Decimal,
) -> CostGateEvidence:
    reasons: list[str] = []
    if expected_gross_edge_r - estimated_round_trip_cost_r <= 0:
        reasons.append("non_positive_expected_net_edge")
    if estimated_round_trip_cost_r > Decimal("0.25"):
        reasons.append("round_trip_cost_above_0_25r")
    if estimated_entries_per_symbol_day > 2:
        reasons.append("symbol_turnover_above_two_per_day")
    if estimated_portfolio_entries_day > 6:
        reasons.append("portfolio_turnover_above_six_per_day")
    return CostGateEvidence(
        expected_gross_edge_r=expected_gross_edge_r,
        estimated_round_trip_cost_r=estimated_round_trip_cost_r,
        estimated_entries_per_symbol_day=estimated_entries_per_symbol_day,
        estimated_portfolio_entries_day=estimated_portfolio_entries_day,
        accepted=not reasons,
        rejection_reasons=tuple(reasons),
    )


def purged_walk_forward_splits(
    sample_count: int,
    *,
    folds: int = 5,
    minimum_train: int = 2_000,
    validation_size: int = 1_000,
    purge_bars: int = 12,
    embargo_bars: int = 12,
) -> list[tuple[np.ndarray, np.ndarray]]:
    if min(sample_count, folds, minimum_train, validation_size) <= 0:
        raise ValueError("walk-forward parameters must be positive")
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for fold in range(folds):
        validation_start = minimum_train + fold * validation_size
        validation_end = min(validation_start + validation_size, sample_count)
        train_end = validation_start - purge_bars
        if train_end <= 0 or validation_end - validation_start < 2:
            continue
        train = np.arange(0, train_end, dtype=np.int64)
        validation = np.arange(validation_start, validation_end, dtype=np.int64)
        if validation_end + embargo_bars > sample_count and fold < folds - 1:
            break
        splits.append((train, validation))
    return splits


def frozen_net_return_labels(
    close: np.ndarray,
    *,
    entry_cost_fraction: np.ndarray | float,
    exit_cost_fraction: np.ndarray | float,
    impact_fraction: np.ndarray | float,
) -> dict[int, np.ndarray]:
    prices = np.asarray(close, dtype=np.float64)
    if prices.ndim != 1 or len(prices) < max(ALPHA_HORIZONS) + 2:
        raise ValueError("at least 14 ordered close prices are required")
    entry = np.asarray(entry_cost_fraction, dtype=np.float64)
    exit_cost = np.asarray(exit_cost_fraction, dtype=np.float64)
    impact = np.asarray(impact_fraction, dtype=np.float64)
    result: dict[int, np.ndarray] = {}
    for horizon in ALPHA_HORIZONS:
        labels = np.full(len(prices), np.nan, dtype=np.float64)
        entry_price = prices[1 : len(prices) - horizon]
        exit_price = prices[1 + horizon :]
        gross = exit_price / entry_price - 1.0
        gross_count = len(gross)

        labels[: len(gross)] = (
            gross
            - _aligned_label_cost(entry, gross_count)
            - _aligned_label_cost(exit_cost, gross_count)
            - _aligned_label_cost(impact, gross_count)
        )
        result[horizon] = labels
    return result


def ensemble_signal(
    linear_prediction: float,
    gbdt_prediction: float,
    *,
    estimated_round_trip_cost: float,
) -> int:
    if linear_prediction == 0 or gbdt_prediction == 0:
        return 0
    if np.sign(linear_prediction) != np.sign(gbdt_prediction):
        return 0
    prediction = 0.5 * linear_prediction + 0.5 * gbdt_prediction
    if abs(prediction) <= 2 * estimated_round_trip_cost:
        return 0
    return 1 if prediction > 0 else -1


@lru_cache(maxsize=64)
def _load_lightgbm_booster(model_payload: str):
    try:
        from lightgbm import Booster
    except ImportError as exc:  # pragma: no cover - dependency is mandatory in production
        raise RuntimeError("lightgbm is required for alpha inference") from exc
    return Booster(model_str=model_payload)


def _signed_model_prediction(
    artifact: AlphaModelArtifactV1,
    matrix: np.ndarray,
) -> float:
    if artifact.serialization_format == "json_coefficients":
        payload = json.loads(artifact.model_payload)
        coefficients = np.asarray(payload["coefficients"], dtype=np.float64).reshape(-1)
        mean = np.asarray(payload["scaler_mean"], dtype=np.float64).reshape(-1)
        scale = np.asarray(payload["scaler_scale"], dtype=np.float64).reshape(-1)
        scale = np.where(scale == 0, 1.0, scale)
        intercept = float(np.asarray(payload["intercept"], dtype=np.float64).reshape(-1)[0])
        raw = float(np.dot((matrix[0] - mean) / scale, coefficients) + intercept)
        if artifact.task == "classification":
            raw = (2.0 / (1.0 + np.exp(-np.clip(raw, -50, 50))) - 1.0) * float(
                artifact.prediction_scale
            )
        return raw
    if artifact.serialization_format == "lightgbm_text":
        # Online inference can run on any long-lived asyncio executor thread.
        # Bound LightGBM's per-caller OpenMP team to prevent native threads from
        # accumulating for the lifetime of the API process on Windows.
        raw = float(
            np.asarray(
                _load_lightgbm_booster(artifact.model_payload).predict(
                    matrix,
                    num_threads=1,
                )
            )[0]
        )
        if artifact.task == "classification":
            raw = (2.0 * raw - 1.0) * float(artifact.prediction_scale)
        return raw
    raise ValueError("only native linear and LightGBM artifacts can perform inference")


def predict_alpha_ensemble(
    linear: AlphaModelArtifactV1,
    gbdt: AlphaModelArtifactV1,
    ensemble: AlphaModelArtifactV1,
    *,
    feature_values: dict[str, float],
    estimated_round_trip_cost: float,
    verify_dependency_versions: bool = True,
) -> AlphaInferenceResultV1:
    """Run the frozen 5m inference path without fitting models or invoking an LLM."""

    if linear.model_family not in {"elastic_net_regression", "logistic_classification"}:
        raise ValueError("linear artifact family is invalid")
    if gbdt.model_family not in {"lightgbm_regression", "lightgbm_classification"}:
        raise ValueError("LightGBM artifact family is invalid")
    if ensemble.model_family != "ensemble_50_50":
        raise ValueError("ensemble artifact family is invalid")
    if linear.task != gbdt.task or linear.task not in {"regression", "classification"}:
        raise ValueError("linear and LightGBM tasks differ")
    frozen_values = {
        (
            item.profile,
            item.horizon_bars,
            item.feature_names,
            item.ir_fingerprint,
            item.cost_model_fingerprint,
        )
        for item in (linear, gbdt, ensemble)
    }
    if len(frozen_values) != 1:
        raise ValueError("alpha artifact profile, horizon, features, IR, or cost model drifted")
    manifest = json.loads(ensemble.model_payload)
    if (
        manifest.get("linear_model_id") != linear.id
        or manifest.get("gbdt_model_id") != gbdt.id
        or manifest.get("weights") != [0.5, 0.5]
        or manifest.get("direction_agreement_required") is not True
        or manifest.get("minimum_edge_cost_multiple") != 2
    ):
        raise ValueError("ensemble manifest does not bind the supplied frozen model pair")
    if verify_dependency_versions:
        installed = dependency_versions()
        for artifact in (linear, gbdt, ensemble):
            if any(
                installed.get(name) != value for name, value in artifact.dependency_versions.items()
            ):
                raise ValueError("alpha model dependency version drift detected")
    missing = [name for name in linear.feature_names if name not in feature_values]
    if missing:
        raise ValueError(f"alpha inference features missing: {missing}")
    matrix = np.asarray(
        [[float(feature_values[name]) for name in linear.feature_names]], dtype=np.float64
    )
    if not np.all(np.isfinite(matrix)):
        raise ValueError("alpha inference features must be finite")
    linear_prediction = _signed_model_prediction(linear, matrix)
    gbdt_prediction = _signed_model_prediction(gbdt, matrix)
    signal = ensemble_signal(
        linear_prediction,
        gbdt_prediction,
        estimated_round_trip_cost=estimated_round_trip_cost,
    )
    blended = 0.5 * linear_prediction + 0.5 * gbdt_prediction
    return AlphaInferenceResultV1(
        horizon_bars=linear.horizon_bars,
        linear_prediction=Decimal(str(linear_prediction)),
        gbdt_prediction=Decimal(str(gbdt_prediction)),
        blended_prediction=Decimal(str(blended)),
        estimated_round_trip_cost=Decimal(str(estimated_round_trip_cost)),
        signal=signal,
        linear_artifact_fingerprint=linear.fingerprint,
        gbdt_artifact_fingerprint=gbdt.fingerprint,
        ensemble_artifact_fingerprint=ensemble.fingerprint,
    )


def predict_alpha_from_feature_history(
    linear: AlphaModelArtifactV1,
    gbdt: AlphaModelArtifactV1,
    ensemble: AlphaModelArtifactV1,
    *,
    raw_features: dict[str, np.ndarray],
    estimated_round_trip_cost: float,
    verify_dependency_versions: bool = True,
) -> AlphaInferenceResultV1:
    """Compile registered features and explicit state gates, then run inference."""

    if linear.canonical_ir_payload is None:
        raise ValueError("linear artifact does not contain a frozen Canonical IR")
    ir = CanonicalStrategyIR.model_validate(linear.canonical_ir_payload)
    if any(item.canonical_ir_payload != linear.canonical_ir_payload for item in (gbdt, ensemble)):
        raise ValueError("model trio Canonical IR payloads differ")
    arrays = {name: np.asarray(values, dtype=np.float64) for name, values in raw_features.items()}
    intent = NumpyFastVectorBackend().evaluate(ir, arrays)
    available = {**arrays, **intent.feature_values}
    feature_values: dict[str, float] = {}
    for name in linear.feature_names:
        if name not in available:
            raise ValueError(f"compiled alpha inference feature is missing: {name}")
        values = np.asarray(available[name], dtype=np.float64)
        if not len(values) or not np.isfinite(values[-1]):
            raise ValueError(f"latest alpha feature is unavailable or non-finite: {name}")
        feature_values[name] = float(values[-1])
    result = predict_alpha_ensemble(
        linear,
        gbdt,
        ensemble,
        feature_values=feature_values,
        estimated_round_trip_cost=estimated_round_trip_cost,
        verify_dependency_versions=verify_dependency_versions,
    )
    canonical_direction = int(intent.entry_intent[-1]) if len(intent.entry_intent) else 0
    final_signal = result.signal if result.signal == canonical_direction else 0
    return result.model_copy(
        update={
            "signal": final_signal,
            "canonical_ir_direction": canonical_direction,
            "regime_gates_passed": canonical_direction != 0,
        }
    )


def causal_asof_join(
    decision_times: np.ndarray,
    available_times: np.ndarray,
    values: np.ndarray,
) -> np.ndarray:
    decisions = np.asarray(decision_times)
    availability = np.asarray(available_times)
    facts = np.asarray(values, dtype=np.float64)
    if availability.ndim != 1 or facts.ndim != 1 or len(availability) != len(facts):
        raise ValueError("availability and values must be equal-length vectors")
    if np.any(availability[1:] < availability[:-1]):
        raise ValueError("available_time must be ordered")
    positions = np.searchsorted(availability, decisions, side="right") - 1
    result = np.full(len(decisions), np.nan, dtype=np.float64)
    mask = positions >= 0
    result[mask] = facts[positions[mask]]
    return result


def split_closed_bars_on_gaps(
    timestamps_ns: np.ndarray,
    *,
    bar_seconds: int = 300,
    maximum_gap_bars: int = 3,
) -> list[np.ndarray]:
    timestamps = np.asarray(timestamps_ns, dtype=np.int64)
    if np.any(timestamps[1:] <= timestamps[:-1]):
        raise ValueError("closed bar timestamps must be strictly increasing")
    distance = np.diff(timestamps)
    split_at = np.flatnonzero(distance > bar_seconds * (maximum_gap_bars + 1) * 1_000_000_000) + 1
    return [part for part in np.split(np.arange(len(timestamps)), split_at) if len(part)]


def train_alpha_model_pair(
    features: np.ndarray,
    labels: np.ndarray,
    *,
    feature_names: tuple[str, ...],
    horizon_bars: Literal[3, 6, 12],
    feature_cube_fingerprint: str,
    label_fingerprint: str,
    cost_model_fingerprint: str,
    ir_fingerprint: str,
    canonical_ir: CanonicalStrategyIR,
    timestamps: tuple[datetime, ...],
    task: Literal["regression", "classification"] = "regression",
    random_seed: int = 17,
) -> AlphaTrainingResultV1:
    """Offline-only deterministic trainer. It never runs on the decision path."""

    if canonical_ir.fingerprint != ir_fingerprint:
        raise ValueError("canonical IR does not match the declared training fingerprint")
    if set(dict(canonical_ir.regime_gates)) != set(REQUIRED_REGIME_GATES):
        raise ValueError("offline alpha training requires all seven explicit regime gates")
    validate_crypto_5m_ir_semantics(canonical_ir)

    try:
        from lightgbm import LGBMClassifier, LGBMRegressor
        from sklearn.linear_model import ElasticNet, LogisticRegression
        from sklearn.metrics import balanced_accuracy_score, mean_squared_error
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError("scikit-learn and lightgbm are required for offline training") from exc

    matrix = np.asarray(features, dtype=np.float64)
    target = np.asarray(labels, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != len(feature_names):
        raise ValueError("feature matrix does not match feature_names")
    if len(matrix) != len(target) or len(timestamps) != len(target):
        raise ValueError("features, labels, and timestamps must align")
    finite = np.isfinite(target) & np.all(np.isfinite(matrix), axis=1)
    matrix = matrix[finite]
    target = target[finite]
    times = tuple(item for item, keep in zip(timestamps, finite, strict=True) if keep)
    if len(matrix) < 3_000:
        raise ValueError("offline alpha training requires at least 3000 finite samples")
    prediction_scale = Decimal("1")
    if task == "classification":
        nonzero_magnitude = np.abs(target[target != 0])
        prediction_scale = Decimal(
            str(float(np.median(nonzero_magnitude))) if len(nonzero_magnitude) else "1"
        )
        target = (target > 0).astype(np.int8)
        if len(np.unique(target)) < 2:
            raise ValueError("classification labels require both directions")

    validation_size = max(250, len(matrix) // 10)
    splits = purged_walk_forward_splits(
        len(matrix),
        minimum_train=max(2_000, len(matrix) // 2),
        validation_size=validation_size,
    )
    if not splits:
        raise ValueError("not enough samples for purged walk-forward")
    linear_scores: list[float] = []
    gbdt_scores: list[float] = []
    windows: list[tuple[datetime, datetime]] = []
    for train_index, validation_index in splits:
        scaler = StandardScaler().fit(matrix[train_index])
        train_x = scaler.transform(matrix[train_index])
        validation_x = scaler.transform(matrix[validation_index])
        if task == "classification":
            linear_model = LogisticRegression(
                penalty="elasticnet",
                solver="saga",
                l1_ratio=0.5,
                random_state=random_seed,
                max_iter=2_000,
            )
            gbdt_model = LGBMClassifier(
                n_estimators=160,
                max_depth=5,
                learning_rate=0.04,
                random_state=random_seed,
                deterministic=True,
                verbosity=-1,
            )
        else:
            linear_model = ElasticNet(alpha=0.001, l1_ratio=0.5, random_state=random_seed)
            gbdt_model = LGBMRegressor(
                n_estimators=160,
                max_depth=5,
                learning_rate=0.04,
                random_state=random_seed,
                deterministic=True,
                verbosity=-1,
            )
        linear_model.fit(train_x, target[train_index])
        gbdt_model.fit(matrix[train_index], target[train_index])
        linear_prediction = linear_model.predict(validation_x)
        gbdt_prediction = gbdt_model.predict(matrix[validation_index])
        if task == "classification":
            linear_scores.append(
                float(balanced_accuracy_score(target[validation_index], linear_prediction)) - 0.5
            )
            gbdt_scores.append(
                float(balanced_accuracy_score(target[validation_index], gbdt_prediction)) - 0.5
            )
        else:
            baseline = np.full(
                len(validation_index),
                float(np.mean(target[train_index])),
                dtype=np.float64,
            )
            baseline_mse = max(
                float(mean_squared_error(target[validation_index], baseline)),
                np.finfo(np.float64).eps,
            )
            linear_scores.append(
                1
                - float(mean_squared_error(target[validation_index], linear_prediction))
                / baseline_mse
            )
            gbdt_scores.append(
                1
                - float(mean_squared_error(target[validation_index], gbdt_prediction))
                / baseline_mse
            )
        windows.append((times[validation_index[0]], times[validation_index[-1]]))

    scaler = StandardScaler().fit(matrix)
    if task == "classification":
        linear_model = LogisticRegression(
            penalty="elasticnet",
            solver="saga",
            l1_ratio=0.5,
            random_state=random_seed,
            max_iter=2_000,
        )
        gbdt_model = LGBMClassifier(
            n_estimators=160,
            max_depth=5,
            learning_rate=0.04,
            random_state=random_seed,
            deterministic=True,
            verbosity=-1,
        )
        linear_family = "logistic_classification"
        gbdt_family = "lightgbm_classification"
    else:
        linear_model = ElasticNet(alpha=0.001, l1_ratio=0.5, random_state=random_seed)
        gbdt_model = LGBMRegressor(
            n_estimators=160,
            max_depth=5,
            learning_rate=0.04,
            random_state=random_seed,
            deterministic=True,
            verbosity=-1,
        )
        linear_family = "elastic_net_regression"
        gbdt_family = "lightgbm_regression"
    linear_model.fit(scaler.transform(matrix), target)
    gbdt_model.fit(matrix, target)
    versions = dependency_versions()
    common = {
        "profile": ALPHA_PROFILE,
        "task": task,
        "horizon_bars": horizon_bars,
        "feature_names": feature_names,
        "feature_cube_fingerprint": feature_cube_fingerprint,
        "label_fingerprint": label_fingerprint,
        "training_window": (times[0], times[-1]),
        "validation_windows": tuple(windows),
        "random_seed": random_seed,
        "dependency_versions": versions,
        "cost_model_fingerprint": cost_model_fingerprint,
        "ir_fingerprint": ir_fingerprint,
        "canonical_ir_payload": canonical_ir.model_dump(mode="json"),
        "prediction_scale": prediction_scale,
    }
    linear_payload = json.dumps(
        {
            "coefficients": np.asarray(linear_model.coef_).tolist(),
            "intercept": np.asarray(linear_model.intercept_).tolist(),
            "scaler_mean": scaler.mean_.tolist(),
            "scaler_scale": scaler.scale_.tolist(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    linear_artifact = AlphaModelArtifactV1(
        **common,
        model_family=linear_family,
        serialization_format="json_coefficients",
        model_payload=linear_payload,
        walk_forward_scores=tuple(Decimal(str(value)) for value in linear_scores),
        walk_forward_passed=bool(linear_scores) and all(value > 0 for value in linear_scores),
    )
    gbdt_artifact = AlphaModelArtifactV1(
        **common,
        model_family=gbdt_family,
        serialization_format="lightgbm_text",
        model_payload=gbdt_model.booster_.model_to_string(),
        walk_forward_scores=tuple(Decimal(str(value)) for value in gbdt_scores),
        walk_forward_passed=bool(gbdt_scores) and all(value > 0 for value in gbdt_scores),
    )
    ensemble_artifact = AlphaModelArtifactV1(
        **{**common, "task": "ensemble"},
        model_family="ensemble_50_50",
        serialization_format="manifest_json",
        model_payload=json.dumps(
            {
                "linear_model_id": linear_artifact.id,
                "gbdt_model_id": gbdt_artifact.id,
                "weights": [0.5, 0.5],
                "direction_agreement_required": True,
                "minimum_edge_cost_multiple": 2,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        walk_forward_scores=tuple(
            Decimal(str(min(linear_score, gbdt_score)))
            for linear_score, gbdt_score in zip(linear_scores, gbdt_scores, strict=True)
        ),
        walk_forward_passed=(
            bool(linear_scores)
            and all(value > 0 for value in linear_scores)
            and all(value > 0 for value in gbdt_scores)
        ),
    )
    return AlphaTrainingResultV1(
        linear=linear_artifact,
        gbdt=gbdt_artifact,
        ensemble=ensemble_artifact,
        linear_validation_score=Decimal(str(np.mean(linear_scores))),
        gbdt_validation_score=Decimal(str(np.mean(gbdt_scores))),
    )


def _dataset_feature_coverage(
    dataset: dict,
    feature: str,
    *,
    candle_coverage: Decimal,
    days: Decimal,
) -> Decimal:
    normalized = feature.strip().lower().replace("-", "_")
    declared = dataset.get("feature_coverage") or {}
    if normalized in declared:
        return min(Decimal(str(declared[normalized])), Decimal("1"))
    derived_from_ohlcv = {
        "trend",
        "volatility",
        "liquidity",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "return",
        "ema",
        "sma",
        "atr",
        "rsi",
    }
    derived_prefixes = ("return_", "ema_", "sma_", "atr_", "rsi_", "trend_", "volatility_")
    if normalized in derived_from_ohlcv or normalized.startswith(derived_prefixes):
        return candle_coverage
    if "funding" in normalized:
        explicit = dataset.get("funding_coverage_ratio")
        if explicit is not None:
            return min(Decimal(str(explicit)), Decimal("1"))
        expected = max(int(days * Decimal("3")), 1)
        return min(Decimal(int(dataset.get("funding_count", 0))) / Decimal(expected), Decimal("1"))
    if normalized == "oi" or "open_interest" in normalized:
        return min(Decimal(str(dataset.get("open_interest_coverage_ratio", 0))), Decimal("1"))
    coverage_fields = (
        (("positioning", "long_short", "position_ratio"), "positioning_coverage_ratio"),
        (("basis",), "basis_coverage_ratio"),
        (("order_flow", "orderflow", "trade_imbalance"), "order_flow_coverage_ratio"),
        (("liquidation",), "liquidation_flow_coverage_ratio"),
    )
    for aliases, field in coverage_fields:
        if any(alias in normalized for alias in aliases):
            return min(Decimal(str(dataset.get(field, 0))), Decimal("1"))
    return min(Decimal(str(dataset.get(f"{normalized}_coverage_ratio", 0))), Decimal("1"))


def assess_crypto_5m_contract(
    datasets: list[dict],
    *,
    required_features: tuple[str, ...] = REQUIRED_REGIME_GATES,
) -> Crypto5mDataContractV1:
    required_features = tuple(sorted(set(required_features)))
    readiness: list[Crypto5mMarketReadiness] = []
    for market in ALPHA_MARKETS:
        exchange, symbol = market.split(":", 1)
        matches = [
            item
            for item in datasets
            if str(item.get("exchange", "")).lower() == exchange
            and item.get("symbol") == symbol
            and item.get("timeframe") == "5m"
            and bool(item.get("closed_bars_only", True))
        ]
        dataset = max(matches, key=lambda item: int(item.get("candle_count", 0)), default=None)
        if dataset is None:
            readiness.append(
                Crypto5mMarketReadiness(
                    market=market,
                    status="NOT_READY",
                    reasons=("missing_closed_5m_dataset",),
                )
            )
            continue
        start = datetime.fromisoformat(str(dataset["data_start"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(dataset["data_end"]).replace("Z", "+00:00"))
        days = Decimal(str(max((end - start).total_seconds(), 0) / 86_400))
        count = int(dataset.get("candle_count", 0))
        expected = max(int(dataset.get("expected_candle_count", 0)), 210_240)
        completeness = Decimal(str(dataset.get("coverage_ratio", 0)))
        if completeness <= 0:
            completeness = Decimal(count) / Decimal(max(int(days * 288) + 1, 1))
        feature_coverage = {
            name: _dataset_feature_coverage(
                dataset,
                name,
                candle_coverage=min(completeness, Decimal("1")),
                days=days,
            )
            for name in required_features
        }
        required_coverage = min(feature_coverage.values(), default=Decimal("0"))
        reasons: list[str] = []
        if days < 730 or count < 210_240:
            reasons.append("less_than_730_days")
        if completeness < Decimal("0.999"):
            reasons.append("completeness_below_99_9_percent")
        largest_gap_bars = int(dataset.get("largest_gap_bars", 0))
        segment_count = max(int(dataset.get("segment_count", 1)), 1)
        if largest_gap_bars > 3 and segment_count < 2:
            reasons.append("gaps_require_segmented_training")
        if dataset.get("point_in_time_status") != "verified":
            reasons.append("availability_time_not_verified")
        if required_coverage < Decimal("0.95"):
            reasons.append("required_feature_coverage_below_95_percent")
        status: Literal["READY", "NOT_READY", "UNTESTABLE"] = "READY"
        if "required_feature_coverage_below_95_percent" in reasons:
            status = "UNTESTABLE"
        elif reasons:
            status = "NOT_READY"
        readiness.append(
            Crypto5mMarketReadiness(
                market=market,
                status=status,
                dataset_id=str(dataset.get("id") or dataset.get("dataset_id") or ""),
                days=days,
                candle_count=count,
                expected_candle_count=expected,
                completeness=min(completeness, Decimal("1")),
                largest_gap_bars=largest_gap_bars,
                segment_count=segment_count,
                required_feature_coverage=required_coverage,
                feature_coverage=feature_coverage,
                reasons=tuple(reasons),
            )
        )
    status: Literal["READY", "NOT_READY", "UNTESTABLE"] = "READY"
    if any(item.status == "UNTESTABLE" for item in readiness):
        status = "UNTESTABLE"
    elif any(item.status != "READY" for item in readiness):
        status = "NOT_READY"
    return Crypto5mDataContractV1(
        markets=tuple(readiness),
        required_features=required_features,
        status=status,
    )


def disabled_live_grant(grant: PersistentLiveAuthorizationGrant, reason: str):
    return grant.model_copy(
        update={
            "enabled": False,
            "disabled_at": datetime.now(UTC),
            "disabled_reason": reason,
        }
    )


def grant_matches_current_state(
    grant: PersistentLiveAuthorizationGrant,
    *,
    account_fingerprint: str,
    configuration_fingerprint: str,
    risk_halted: bool,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if not grant.enabled:
        reasons.append(grant.disabled_reason or "operator_disabled")
    if grant.account_fingerprint != account_fingerprint:
        reasons.append("account_scope_changed")
    if grant.configuration_fingerprint != configuration_fingerprint:
        reasons.append("configuration_changed")
    if risk_halted:
        reasons.append("hard_risk_halt")
    return not reasons, tuple(reasons)
