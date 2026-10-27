from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, Field, model_validator

from .backtest import run_canonical_ir_exact_replay
from .build_identity import RuntimeBuildIdentity
from .domain import Candle
from .research_cache import (
    BehaviorFingerprintIndex,
    BootstrapReferenceCache,
    CostScenarioCache,
    RollingFeatureCube,
    SignalCache,
)
from .research_certificates import (
    ArtifactFingerprintV2,
    ExecutionEligibilityGrant,
    FullStrategyCertificate,
    GeneratorEnvelopeCertificate,
    GeneratorProvenance,
    ShadowCandidateArtifact,
    validate_generator_provenance,
)
from .research_integrity import (
    OnlineFDRController,
    PermanentTrialRecord,
    ResearchDisposition,
    TrialResult,
    moving_block_expected_r,
)
from .strategy_backends import (
    NumpyFastVectorBackend,
    StrategyIntentSeries,
    assert_backend_intent_parity,
)
from .strategy_dsl import (
    CanonicalStrategyIR,
    ResearchHypothesisV2,
    StrategyDSLV2,
    StrategyDSLValidationError,
    compile_strategy_dsl,
)
from .strategy_integrity import run_integrity_gates

FAST_GATE_SECONDS = 300
COMMIT_RESERVE_SECONDS = 15
MAX_SEMANTIC_PROPOSALS = 12
MAX_EMPIRICAL_TRIALS = 3
MAX_EXACT_FINALISTS = 2
MAX_EVOLUTION_ROUNDS = 1


class AutonomousResearchMode(StrEnum):
    FULL_AGENT = "full_agent"
    DEGRADED_AGENT = "degraded_agent"
    SAFE_MODE = "safe_mode"


class CandidateOrigin(StrEnum):
    CERTIFIED_GENERATOR = "certified_generator"
    FREE_DSL = "free_dsl"
    LEGACY_CERTIFIED_ADAPTER = "legacy_certified_adapter"


class ResearchGenerationOutcome(BaseModel, frozen=True):
    mode: AutonomousResearchMode
    proposals: tuple[dict, ...] = ()
    reason_codes: tuple[str, ...] = ()


async def generate_research_proposals(
    llm_call: Callable[[], Awaitable[list[dict]]],
    certified_generator_call: Callable[[], list[dict]],
    *,
    llm_budget_seconds: float = 8.0,
) -> ResearchGenerationOutcome:
    """One-shot LLM generation with deterministic fail-closed degradation."""
    try:
        proposals = await asyncio.wait_for(llm_call(), timeout=llm_budget_seconds)
        return ResearchGenerationOutcome(
            mode=AutonomousResearchMode.FULL_AGENT,
            proposals=tuple(proposals[:MAX_SEMANTIC_PROPOSALS]),
        )
    except Exception:
        try:
            proposals = certified_generator_call()
            return ResearchGenerationOutcome(
                mode=AutonomousResearchMode.DEGRADED_AGENT,
                proposals=tuple(proposals[:MAX_SEMANTIC_PROPOSALS]),
                reason_codes=("llm_gateway_unavailable",),
            )
        except Exception:
            return ResearchGenerationOutcome(
                mode=AutonomousResearchMode.SAFE_MODE,
                reason_codes=(
                    "llm_gateway_unavailable",
                    "certified_generator_unavailable",
                    "hold_without_existing_valid_certificate",
                ),
            )


class FastGateStage(StrEnum):
    COMPILE = "compile"
    INTEGRITY = "integrity"
    CHEAP_EVIDENCE = "cheap_evidence"
    PURGED_OOF = "purged_oof"
    EXACT = "exact"
    STATISTICAL = "statistical"
    PORTFOLIO = "portfolio"
    RISK_COMMIT = "risk_commit"


STAGE_DEADLINES = {
    FastGateStage.COMPILE: 15,
    FastGateStage.INTEGRITY: 35,
    FastGateStage.CHEAP_EVIDENCE: 60,
    FastGateStage.PURGED_OOF: 140,
    FastGateStage.EXACT: 210,
    FastGateStage.STATISTICAL: 260,
    FastGateStage.PORTFOLIO: 285,
    FastGateStage.RISK_COMMIT: 300,
}


class DeadlineExceeded(TimeoutError):
    pass


class DeadlineContext:
    def __init__(
        self,
        *,
        total_seconds: float = FAST_GATE_SECONDS,
        already_elapsed_seconds: float = 0.0,
    ) -> None:
        self.total_seconds = total_seconds
        self.started_monotonic = time.monotonic() - max(0.0, already_elapsed_seconds)
        self.cancelled = False

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed_seconds)

    def checkpoint(self, stage: FastGateStage, *, reserve_commit: bool = False) -> None:
        if self.cancelled:
            raise DeadlineExceeded(f"{stage}: validation cancelled")
        stage_deadline = min(float(STAGE_DEADLINES[stage]), self.total_seconds)
        effective_deadline = (
            self.total_seconds - COMMIT_RESERVE_SECONDS if reserve_commit else stage_deadline
        )
        if self.elapsed_seconds >= effective_deadline:
            raise DeadlineExceeded(f"{stage}: deadline exceeded")

    def cancel(self) -> None:
        self.cancelled = True


class MarketSnapshotV2(BaseModel):
    id: str
    dataset_fingerprint: str
    dataset_schema_version: int = Field(ge=1)
    availability_policy: str
    market_scope: tuple[str, ...]
    timeframe_seconds: int = Field(gt=0)
    features: dict[str, list[float] | list[list[float]]]
    event_time_ns: list[int]
    available_time_ns: list[int]
    decision_time_ns: list[int]
    ready_at: datetime
    market_data_freshness_seconds: Decimal = Field(default=Decimal("0"), ge=0)
    market_data_freshness_sla_seconds: Decimal = Field(default=Decimal("60"), gt=0)


class AutonomousResearchRunRequest(BaseModel):
    idempotency_key: str = Field(min_length=16, max_length=128)
    research_event_id: str = Field(min_length=1, max_length=128)
    mode: AutonomousResearchMode = AutonomousResearchMode.FULL_AGENT
    hypothesis: ResearchHypothesisV2
    dsl: StrategyDSLV2
    origin: CandidateOrigin
    snapshot: MarketSnapshotV2
    validation_config_fingerprint: str
    generator_provenance: GeneratorProvenance | None = None
    generator_envelope_id: str | None = None
    requested_risk_budget: Decimal = Field(default=Decimal("0.001"), gt=0, le=Decimal("0.10"))
    mature_reference_risk_budget: Decimal = Field(default=Decimal("0.02"), gt=0, le=Decimal("1"))
    transaction_cost_bps: Decimal = Field(default=Decimal("8"), ge=0, le=Decimal("500"))
    market_state_valid: bool | None = None
    current_spread_bps: Decimal | None = Field(default=None, ge=0)
    maximum_spread_bps: Decimal = Field(default=Decimal("20"), ge=0)
    liquidity_passed: bool | None = None
    portfolio_evidence: dict[str, str] | None = None
    research_profile: Literal["crypto_5m_v1"] | None = None
    horizons: tuple[Literal[3], Literal[6], Literal[12]] = (3, 6, 12)
    model_artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enforce_crypto_5m_contract(self):
        if self.research_profile != "crypto_5m_v1":
            return self
        required = {
            "trend",
            "volatility",
            "liquidity",
            "funding",
            "open_interest",
            "basis",
            "order_flow",
        }
        if self.snapshot.timeframe_seconds != 300:
            raise ValueError("crypto_5m_v1 requires a 5m feature snapshot")
        if set(self.dsl.regime_gates) != required:
            raise ValueError("crypto_5m_v1 requires all seven explicit regime gates")
        if self.dsl.risk.maximum_holding_bars not in self.horizons:
            raise ValueError("holding period must be one of the frozen 3/6/12 bar horizons")
        if len(self.model_artifact_ids) < 3:
            raise ValueError("linear, LightGBM, and ensemble artifact references are required")
        return self


class ResearchRunResult(BaseModel, frozen=True):
    run_id: str
    disposition: ResearchDisposition
    reason_codes: tuple[str, ...]
    evidence_references: tuple[str, ...]
    elapsed_seconds: Decimal
    deadline_exceeded: bool
    artifact_fingerprint: str | None = None
    trial_id: str | None = None
    eligibility_grant_id: str | None = None
    shadow_artifact_id: str | None = None
    committed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def behavior_fingerprint(intent) -> str:
    payload = {
        "entry": intent.entry_intent.tolist(),
        "position": intent.position_intent.tolist(),
        "exit": intent.exit_intent.tolist(),
        "holding_turnover": int(np.abs(np.diff(intent.position_intent)).sum()),
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _hypothesis_hash(hypothesis: ResearchHypothesisV2) -> str:
    return sha256(
        json.dumps(
            hypothesis.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _artifact(
    request: AutonomousResearchRunRequest,
    ir: CanonicalStrategyIR,
    build_identity: RuntimeBuildIdentity,
) -> ArtifactFingerprintV2:
    feature_ast_hash = sha256(
        json.dumps(
            [node.model_dump(mode="json") for node in ir.nodes],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    cost_parameters_fingerprint = sha256(
        f"transaction_cost_bps:{request.transaction_cost_bps}".encode()
    ).hexdigest()
    return ArtifactFingerprintV2(
        dataset_fingerprint=request.snapshot.dataset_fingerprint,
        dataset_schema_version=request.snapshot.dataset_schema_version,
        dataset_availability_policy=request.snapshot.availability_policy,
        feature_ast_hash=feature_ast_hash,
        ir_hash=ir.fingerprint,
        operator_versions=tuple(f"{node.operator}:{node.operator_version}" for node in ir.nodes),
        operator_policy_version=ir.operator_policy_version,
        market_scope=request.snapshot.market_scope,
        point_in_time_policy="available_time<=decision_time",
        fast_engine_version="numpy-fast-v1",
        exact_engine_version="event-intent-adapter-v1",
        cost_model_version="linear-bps-v1",
        cost_parameters_fingerprint=cost_parameters_fingerprint,
        slippage_model_version="existing-exact-engine-v1",
        slippage_parameters_fingerprint=cost_parameters_fingerprint,
        compiler_version="strategy-dsl-v2",
        purge_bars=max(
            12 if request.research_profile == "crypto_5m_v1" else 1,
            *(node.warmup for node in ir.nodes),
        ),
        embargo_bars=(
            12
            if request.research_profile == "crypto_5m_v1"
            else max(1, ir.maximum_holding_bars)
        ),
        validation_config_fingerprint=request.validation_config_fingerprint,
        git_commit_sha=build_identity.git_commit_sha,
        source_tree_hash=build_identity.source_tree_hash,
        working_tree_dirty=build_identity.working_tree_dirty,
    )


def _snapshot_candles(snapshot: MarketSnapshotV2) -> list[Candle]:
    close = snapshot.features["close"]
    opens = snapshot.features.get("open", close)
    highs = snapshot.features.get("high", close)
    lows = snapshot.features.get("low", close)
    volumes = snapshot.features.get("volume", [1_000_000.0] * len(close))

    def decimal_value(value: float) -> Decimal:
        return Decimal(str(value)).quantize(Decimal("0.00000001"))

    return [
        Candle(
            timestamp=datetime.fromtimestamp(timestamp / 1_000_000_000, tz=UTC),
            open=decimal_value(open_value),
            high=decimal_value(high_value),
            low=decimal_value(low_value),
            close=decimal_value(close_value),
            volume=decimal_value(volume),
        )
        for timestamp, open_value, high_value, low_value, close_value, volume in zip(
            snapshot.event_time_ns,
            opens,
            highs,
            lows,
            close,
            volumes,
            strict=True,
        )
    ]


def _purged_oof_evidence(
    net_returns: np.ndarray,
    *,
    purge_bars: int,
    embargo_bars: int,
) -> tuple[bool, Decimal]:
    indices = np.array_split(np.arange(len(net_returns)), 4)
    fold_means: list[float] = []
    for fold in indices:
        if fold.size <= purge_bars + embargo_bars:
            continue
        usable = fold[purge_bars : fold.size - embargo_bars]
        values = net_returns[usable]
        values = values[np.isfinite(values)]
        if values.size:
            fold_means.append(float(np.mean(values)))
    if len(fold_means) < 3:
        return False, Decimal("1")
    negative_ratio = Decimal(sum(value <= 0 for value in fold_means)) / Decimal(len(fold_means))
    return sum(value > 0 for value in fold_means) >= 3, negative_ratio


def _crypto_pre_return_cost_turnover_gate(
    intent: StrategyIntentSeries,
    features: dict[str, np.ndarray],
    *,
    timeframe_seconds: int,
    market_count: int,
    transaction_cost_bps: Decimal,
) -> tuple[list[str], dict[str, str]]:
    """Reject structurally expensive 5m candidates before any return is read."""

    positions = np.asarray(intent.position_intent, dtype=np.int8)
    previous = np.roll(positions, 1)
    if previous.size:
        previous[0] = 0
    entries = (positions != 0) & ((previous == 0) | (np.sign(previous) != np.sign(positions)))
    observation_days = max(
        Decimal(str(len(positions) * timeframe_seconds)) / Decimal("86400"),
        Decimal("0.000001"),
    )
    entries_per_symbol_day = Decimal(int(np.count_nonzero(entries))) / observation_days
    portfolio_entries_day = entries_per_symbol_day * Decimal(max(1, market_count))

    close = np.asarray(features.get("close", []), dtype=np.float64)
    stops = np.asarray(intent.stop_distance, dtype=np.float64)
    usable = (
        np.isfinite(close)
        & np.isfinite(stops)
        & (close > 0)
        & (np.abs(stops) > 0)
    )
    if np.any(usable):
        median_stop_fraction = Decimal(str(float(np.median(np.abs(stops[usable]) / close[usable]))))
    else:
        median_stop_fraction = Decimal("0")
    round_trip_fraction = transaction_cost_bps * Decimal("2") / Decimal("10000")
    round_trip_cost_r = (
        round_trip_fraction / median_stop_fraction
        if median_stop_fraction > 0
        else Decimal("Infinity")
    )

    reasons: list[str] = []
    if round_trip_cost_r > Decimal("0.25"):
        reasons.append("round_trip_cost_above_0_25r")
    if entries_per_symbol_day > Decimal("2"):
        reasons.append("symbol_turnover_above_two_per_day")
    if portfolio_entries_day > Decimal("6"):
        reasons.append("portfolio_turnover_above_six_per_day")
    evidence = {
        "estimated_round_trip_cost_r": str(round_trip_cost_r),
        "estimated_entries_per_symbol_day": str(entries_per_symbol_day),
        "estimated_portfolio_entries_day": str(portfolio_entries_day),
        "return_data_accessed": "false",
    }
    return reasons, evidence


class FastGateEngine:
    def __init__(
        self,
        store: Any,
        build_identity: RuntimeBuildIdentity,
        *,
        envelope: GeneratorEnvelopeCertificate | None = None,
    ) -> None:
        self.store = store
        self.build_identity = build_identity
        self.envelope = envelope

    def _commit(
        self,
        *,
        run_id: str,
        disposition: ResearchDisposition,
        reasons: list[str],
        evidence: list[str],
        deadline: DeadlineContext,
        artifact: ArtifactFingerprintV2 | None = None,
        trial_id: str | None = None,
        grant: ExecutionEligibilityGrant | None = None,
        shadow: ShadowCandidateArtifact | None = None,
        deadline_exceeded: bool = False,
        trial_result: dict | None = None,
    ) -> ResearchRunResult:
        result = ResearchRunResult(
            run_id=run_id,
            disposition=disposition,
            reason_codes=tuple(sorted(set(reasons))),
            evidence_references=tuple(evidence),
            elapsed_seconds=Decimal(str(round(deadline.elapsed_seconds, 6))),
            deadline_exceeded=deadline_exceeded,
            artifact_fingerprint=artifact.fingerprint if artifact else None,
            trial_id=trial_id,
            eligibility_grant_id=grant.id if grant else None,
            shadow_artifact_id=shadow.id if shadow else None,
        )
        self.store.commit_autonomous_research_decision(
            result,
            trial_result=trial_result,
        )
        return result

    def run(
        self,
        request: AutonomousResearchRunRequest,
        *,
        deadline: DeadlineContext | None = None,
    ) -> ResearchRunResult:
        snapshot_age = max(
            0.0,
            (datetime.now(UTC) - request.snapshot.ready_at).total_seconds(),
        )
        budget = deadline or DeadlineContext(already_elapsed_seconds=snapshot_age)
        run_id = f"research_run_{uuid4().hex}"
        existing = self.store.create_autonomous_research_event(
            run_id=run_id,
            idempotency_key=request.idempotency_key,
            snapshot_ready_at=request.snapshot.ready_at,
            payload={
                "run_id": run_id,
                "status": "RUNNING",
                "research_event_id": request.research_event_id,
                "hypothesis_id": request.hypothesis.id,
            },
        )
        if existing.get("disposition"):
            return ResearchRunResult.model_validate(existing)
        if existing.get("run_id") != run_id:
            persisted = self.store.load_autonomous_research_event(existing["run_id"])
            if persisted and persisted.get("disposition"):
                return ResearchRunResult.model_validate(persisted)
            run_id = existing["run_id"]
        evidence: list[str] = []
        trial: PermanentTrialRecord | None = None
        artifact: ArtifactFingerprintV2 | None = None
        try:
            if request.mode == AutonomousResearchMode.SAFE_MODE:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=["safe_mode_new_research_disabled"],
                    evidence=evidence,
                    deadline=budget,
                )
            if (
                request.snapshot.market_data_freshness_seconds
                > request.snapshot.market_data_freshness_sla_seconds
            ):
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=["market_data_freshness_sla_exceeded"],
                    evidence=["market-data-freshness:external-to-fast-gate-deadline"],
                    deadline=budget,
                )
            semantic_budget = self.store.consume_autonomous_research_budget(
                research_event_id=request.research_event_id,
                family_id=request.hypothesis.family_id,
                budget_kind="semantic_proposals",
                amount=1,
                limit=MAX_SEMANTIC_PROPOSALS,
                idempotency_key=f"{request.idempotency_key}:semantic",
            )
            if not semantic_budget:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=["semantic_proposal_budget_exhausted"],
                    evidence=evidence,
                    deadline=budget,
                )
            if (
                request.mode == AutonomousResearchMode.DEGRADED_AGENT
                and request.origin == CandidateOrigin.FREE_DSL
            ):
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=["degraded_agent_certified_generator_only"],
                    evidence=evidence,
                    deadline=budget,
                )
            # Economic rationale exists durably before candidate compilation.
            self.store.save_research_hypothesis_v2(
                request.hypothesis,
                parent_id=request.hypothesis.parent_hypothesis_id,
            )
            budget.checkpoint(FastGateStage.COMPILE)
            ir = compile_strategy_dsl(request.dsl)
            if request.dsl.hypothesis_id != request.hypothesis.id:
                raise StrategyDSLValidationError("DSL is not bound to the persisted hypothesis")
            if not set(request.hypothesis.required_features).issubset(ir.required_features):
                raise StrategyDSLValidationError(
                    "DSL does not declare all hypothesis required features"
                )
            artifact = _artifact(request, ir, self.build_identity)
            evidence.append(f"artifact:{artifact.fingerprint}")
            feature_graph_hash = sha256(
                json.dumps(
                    [node.model_dump(mode="json", exclude={"parameters"}) for node in ir.nodes],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            ast_is_new = self.store.register_research_novelty(
                novelty_kind="ast",
                novelty_fingerprint=ir.fingerprint,
                family_id=request.hypothesis.family_id,
                payload={"research_event_id": request.research_event_id},
            )
            feature_graph_is_new = self.store.register_research_novelty(
                novelty_kind="feature_graph",
                novelty_fingerprint=feature_graph_hash,
                family_id=request.hypothesis.family_id,
                payload={"ir_hash": ir.fingerprint},
            )
            if ast_is_new and not feature_graph_is_new:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.REJECTED,
                    reasons=["duplicate_feature_graph"],
                    evidence=evidence,
                    deadline=budget,
                    artifact=artifact,
                )
            lineage_id = sha256(
                f"{request.research_event_id}:{ir.fingerprint}".encode()
            ).hexdigest()
            features = {
                name: np.asarray(values, dtype=np.float64)
                for name, values in request.snapshot.features.items()
            }
            missing_features = sorted(set(ir.required_features) - features.keys())
            unequal_feature_lengths = (
                len({len(features[name]) for name in ir.required_features if name in features}) > 1
            )
            if missing_features or unequal_feature_lengths:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=(
                        ["required_features_missing"]
                        if missing_features
                        else ["feature_array_length_mismatch"]
                    ),
                    evidence=[*evidence, "data-infrastructure:available"],
                    deadline=budget,
                    artifact=artifact,
                )
            evidence.extend(["data-infrastructure:healthy", "campaign-feature-coverage:complete"])
            budget.checkpoint(FastGateStage.INTEGRITY)
            integrity = run_integrity_gates(
                ir,
                features,
                event_time_ns=np.asarray(request.snapshot.event_time_ns, dtype=np.int64),
                available_time_ns=np.asarray(request.snapshot.available_time_ns, dtype=np.int64),
                decision_time_ns=np.asarray(request.snapshot.decision_time_ns, dtype=np.int64),
            )
            evidence.append(f"integrity:{artifact.fingerprint}")
            if not integrity.passed:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.REJECTED,
                    reasons=integrity.reason_codes,
                    evidence=evidence,
                    deadline=budget,
                    artifact=artifact,
                )
            intent = NumpyFastVectorBackend().evaluate(ir, features)
            RollingFeatureCube(self.store).put(
                artifact.fingerprint,
                {
                    "feature_values": {
                        name: np.asarray(values).tolist()
                        for name, values in intent.feature_values.items()
                    },
                    "warmup": intent.warmup,
                },
            )
            behavior = behavior_fingerprint(intent)
            evidence.append(f"behavior:{behavior}")
            self.store.save_candidate_lineage_v2(
                lineage_id=lineage_id,
                research_event_id=request.research_event_id,
                family_id=request.hypothesis.family_id,
                candidate_id=ir.fingerprint,
                parent_ids=(
                    [request.hypothesis.parent_hypothesis_id]
                    if request.hypothesis.parent_hypothesis_id
                    else []
                ),
                operation="initial_proposal",
                payload={
                    "hypothesis_id": request.hypothesis.id,
                    "ast_hash": artifact.feature_ast_hash,
                    "feature_graph_hash": feature_graph_hash,
                    "behavior_hash": behavior,
                    "dataset_fingerprint": request.snapshot.dataset_fingerprint,
                    "validation_fingerprint": request.validation_config_fingerprint,
                },
            )
            is_novel_behavior = BehaviorFingerprintIndex(self.store).register(
                behavior_fingerprint=behavior,
                ir_hash=ir.fingerprint,
                family_id=request.hypothesis.family_id,
                payload={
                    "research_event_id": run_id,
                    "artifact_fingerprint": artifact.fingerprint,
                },
            )
            if not is_novel_behavior:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.REJECTED,
                    reasons=["duplicate_pre_return_behavior"],
                    evidence=evidence,
                    deadline=budget,
                    artifact=artifact,
                )
            SignalCache(self.store).put(
                artifact.fingerprint,
                {
                    "behavior_fingerprint": behavior,
                    "entry_intent": intent.entry_intent.tolist(),
                    "position_intent": intent.position_intent.tolist(),
                    "invalidation": intent.invalidation.tolist(),
                },
            )

            if request.research_profile == "crypto_5m_v1":
                structural_reasons, structural_evidence = _crypto_pre_return_cost_turnover_gate(
                    intent,
                    features,
                    timeframe_seconds=request.snapshot.timeframe_seconds,
                    market_count=len(set(request.snapshot.market_scope)),
                    transaction_cost_bps=request.transaction_cost_bps,
                )
                CostScenarioCache(self.store).put(
                    artifact.fingerprint,
                    {"pre_return_gate": structural_evidence},
                )
                evidence.append(
                    "pre-return-cost-turnover:"
                    + json.dumps(structural_evidence, sort_keys=True, separators=(",", ":"))
                )
                if structural_reasons:
                    return self._commit(
                        run_id=run_id,
                        disposition=ResearchDisposition.REJECTED,
                        reasons=structural_reasons,
                        evidence=evidence,
                        deadline=budget,
                        artifact=artifact,
                    )

            # This atomic reservation is the last operation before reading close
            # returns and strategy PnL below.
            empirical_budget = self.store.consume_autonomous_research_budget(
                research_event_id=request.research_event_id,
                family_id=request.hypothesis.family_id,
                budget_kind="empirical_trials",
                amount=1,
                limit=MAX_EMPIRICAL_TRIALS,
                idempotency_key=f"{request.idempotency_key}:empirical",
            )
            if not empirical_budget:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=["empirical_trial_budget_exhausted"],
                    evidence=evidence,
                    deadline=budget,
                    artifact=artifact,
                )
            trial_candidate = PermanentTrialRecord.empirical(
                research_event_id=request.research_event_id,
                campaign_id=None,
                hypothesis_id=request.hypothesis.id,
                family_id=request.hypothesis.family_id,
                candidate_id=ir.fingerprint,
                dataset_fingerprint=request.snapshot.dataset_fingerprint,
                validation_fingerprint=request.validation_config_fingerprint,
                artifact_fingerprint=artifact.fingerprint,
            )
            trial = PermanentTrialRecord.model_validate(
                self.store.reserve_research_trial(trial_candidate)
            )
            fdr = OnlineFDRController(self.store)
            global_allocation = fdr.allocate_alpha(
                "autonomous:global",
                scope="global",
                idempotency_key=f"{trial.idempotency_key}:global",
            )
            family_allocation = fdr.allocate_alpha(
                f"autonomous:family:{request.hypothesis.family_id}",
                scope=request.hypothesis.family_id,
                idempotency_key=f"{trial.idempotency_key}:family",
            )

            budget.checkpoint(FastGateStage.CHEAP_EVIDENCE)
            close = features["close"]
            returns = np.zeros_like(close)
            returns[1:] = close[1:] / close[:-1] - 1.0
            turnover = np.abs(np.diff(intent.position_intent, prepend=0))
            cost = float(request.transaction_cost_bps) / 10_000.0
            net = np.roll(intent.position_intent, 1) * returns - turnover * cost
            CostScenarioCache(self.store).put(
                artifact.fingerprint,
                {
                    "transaction_cost_bps": str(request.transaction_cost_bps),
                    "net_observations": net.tolist(),
                },
            )
            budget.checkpoint(FastGateStage.PURGED_OOF)
            oof_passed, pbo_upper_bound = _purged_oof_evidence(
                net,
                purge_bars=max(
                    12 if request.research_profile == "crypto_5m_v1" else 1,
                    *(node.warmup for node in ir.nodes),
                ),
                embargo_bars=(
                    12
                    if request.research_profile == "crypto_5m_v1"
                    else ir.maximum_holding_bars
                ),
            )
            exact_budget = self.store.consume_autonomous_research_budget(
                research_event_id=request.research_event_id,
                family_id=request.hypothesis.family_id,
                budget_kind="exact_finalists",
                amount=1,
                limit=MAX_EXACT_FINALISTS,
                idempotency_key=f"{request.idempotency_key}:exact",
            )
            if not exact_budget:
                return self._commit(
                    run_id=run_id,
                    disposition=ResearchDisposition.UNTESTABLE,
                    reasons=["exact_finalist_budget_exhausted"],
                    evidence=evidence,
                    deadline=budget,
                    artifact=artifact,
                    trial_id=trial.id,
                    trial_result={
                        "result": str(TrialResult.UNTESTABLE),
                        "reason_codes": ["exact_finalist_budget_exhausted"],
                    },
                )
            budget.checkpoint(FastGateStage.EXACT)
            assert_backend_intent_parity(ir, features)
            exact = run_canonical_ir_exact_replay(
                ir,
                _snapshot_candles(request.snapshot),
                feature_overrides=features,
                fee_bps=request.transaction_cost_bps / Decimal("2"),
                spread_bps=request.transaction_cost_bps / Decimal("4"),
                slippage_bps=request.transaction_cost_bps / Decimal("4"),
            )
            evidence.append(f"exact:{artifact.fingerprint}:{exact.engine_version}")
            bootstrap_cache = BootstrapReferenceCache(self.store)
            cached_bootstrap = bootstrap_cache.get(artifact.fingerprint)
            if cached_bootstrap is None:
                expected_r = moving_block_expected_r(
                    exact.net_pnls,
                    exact.initial_risks,
                    samples=500,
                    seed_material=artifact.fingerprint,
                )
                bootstrap_cache.put(
                    artifact.fingerprint,
                    {"expected_r": expected_r.model_dump(mode="json")},
                )
            else:
                from .research_integrity import ExpectedREvidence

                expected_r = ExpectedREvidence.model_validate(cached_bootstrap["expected_r"])
            budget.checkpoint(FastGateStage.STATISTICAL)
            p_value = expected_r.p_value
            fdr.record_result(global_allocation, p_value=p_value)
            fdr.record_result(family_allocation, p_value=p_value)
            online_alpha = min(global_allocation.alpha, family_allocation.alpha)
            deflated_sharpe_probability = Decimal("1") - p_value
            pbo_threshold = (
                Decimal("0.20")
                if request.research_profile == "crypto_5m_v1"
                else Decimal("0.25")
            )
            stressed_net = np.roll(intent.position_intent, 1) * returns - turnover * cost * 2
            cost_stress_passed = bool(np.nanmean(stressed_net) > 0)
            statistical_passed = bool(
                expected_r.passed
                and p_value <= online_alpha
                and deflated_sharpe_probability >= Decimal("0.90")
                and pbo_upper_bound <= pbo_threshold
                and oof_passed
                and cost_stress_passed
            )
            risk_passed = bool(
                exact.trades and all(trade.initial_risk_amount > 0 for trade in exact.trades)
            )
            budget.checkpoint(FastGateStage.PORTFOLIO, reserve_commit=True)
            reasons: list[str] = []
            if not expected_r.testable:
                disposition = ResearchDisposition.UNTESTABLE
                reasons.extend(expected_r.reasons)
            elif not expected_r.passed:
                disposition = ResearchDisposition.REJECTED
                reasons.append("non_positive_expected_r_confidence_bound")
            elif not oof_passed:
                disposition = ResearchDisposition.REJECTED
                reasons.append("purged_oof_stability_failed")
            elif pbo_upper_bound > pbo_threshold:
                disposition = ResearchDisposition.REJECTED
                reasons.append("pbo_upper_bound_failed")
            elif not cost_stress_passed:
                disposition = ResearchDisposition.REJECTED
                reasons.append("cost_stress_failed")
            elif not statistical_passed:
                disposition = ResearchDisposition.SHADOW_ONLY
                reasons.append("online_fdr_budget_not_met")
            elif not risk_passed:
                disposition = ResearchDisposition.REJECTED
                reasons.append("risk_gate_failed")
            else:
                disposition = ResearchDisposition.SHADOW_ONLY

            portfolio_passed = False
            if request.portfolio_evidence is None:
                reasons.append("portfolio_evidence_missing")
            else:
                portfolio_passed, portfolio_reasons = portfolio_gate_lite(
                    PortfolioGateLiteEvidence.model_validate(request.portfolio_evidence)
                )
                reasons.extend(portfolio_reasons)
            market_risk_passed = bool(
                request.market_state_valid is True
                and request.liquidity_passed is True
                and request.current_spread_bps is not None
                and request.current_spread_bps <= request.maximum_spread_bps
            )
            if not market_risk_passed:
                reasons.append("latest_market_state_risk_gate_failed")

            provenance_reasons: list[str] = []
            if request.origin == CandidateOrigin.CERTIFIED_GENERATOR:
                if request.generator_provenance is None or self.envelope is None:
                    provenance_reasons.append("certified_generator_provenance_missing")
                else:
                    provenance_reasons.extend(
                        validate_generator_provenance(
                            request.generator_provenance,
                            self.envelope,
                            hypothesis_hash=_hypothesis_hash(request.hypothesis),
                            output_ir_hash=ir.fingerprint,
                        )
                    )
                    if ir.family not in self.envelope.allowed_families:
                        provenance_reasons.append("family_outside_generator_envelope")
                    if self.envelope.allowed_features and not set(ir.required_features).issubset(
                        self.envelope.allowed_features
                    ):
                        provenance_reasons.append("feature_outside_generator_envelope")
                    if any(
                        node.operator not in self.envelope.allowed_operators for node in ir.nodes
                    ):
                        provenance_reasons.append("operator_outside_generator_envelope")
                    if len(ir.nodes) > self.envelope.maximum_nodes:
                        provenance_reasons.append("complexity_outside_generator_envelope")
                    if max(node.lookback for node in ir.nodes) > self.envelope.maximum_lookback:
                        provenance_reasons.append("lookback_outside_generator_envelope")
                    lower_holding, upper_holding = self.envelope.holding_horizon_bounds
                    if not lower_holding <= ir.maximum_holding_bars <= upper_holding:
                        provenance_reasons.append("holding_outside_generator_envelope")
                    if request.requested_risk_budget > self.envelope.risk_budget_ceiling:
                        provenance_reasons.append("risk_outside_generator_envelope")
            else:
                provenance_reasons.append("free_dsl_requires_shadow")
            reasons.extend(provenance_reasons)

            grant: ExecutionEligibilityGrant | None = None
            shadow: ShadowCandidateArtifact | None = None
            if (
                statistical_passed
                and risk_passed
                and portfolio_passed
                and market_risk_passed
                and not provenance_reasons
                and self.build_identity.formal_certificate_eligible
            ):
                disposition = ResearchDisposition.EXECUTION_ELIGIBLE
                grant = ExecutionEligibilityGrant.issue(
                    strategy_ir_hash=ir.fingerprint,
                    artifact_fingerprint=artifact.fingerprint,
                    generator_envelope_fingerprint=self.envelope.fingerprint,
                    target_timeframe_seconds=request.snapshot.timeframe_seconds,
                    requested_risk_budget=request.requested_risk_budget,
                    mature_reference_risk_budget=request.mature_reference_risk_budget,
                    build_identity=self.build_identity,
                )
                self.store.save_research_certificate(grant, "execution_eligibility_grant")
                evidence.append(f"eligibility:{grant.id}")
            elif disposition == ResearchDisposition.SHADOW_ONLY:
                shadow = ShadowCandidateArtifact(
                    research_run_id=run_id,
                    hypothesis_id=request.hypothesis.id,
                    ir_hash=ir.fingerprint,
                    artifact_fingerprint=artifact.fingerprint,
                    reason_codes=tuple(sorted(set(reasons))),
                )
                self.store.save_research_certificate(shadow, "shadow_candidate")
                evidence.append(f"shadow:{shadow.id}")
            trial_result = {
                ResearchDisposition.EXECUTION_ELIGIBLE: TrialResult.PASS,
                ResearchDisposition.SHADOW_ONLY: TrialResult.SHADOW_REQUIRED,
                ResearchDisposition.REJECTED: TrialResult.FAIL,
                ResearchDisposition.UNTESTABLE: TrialResult.UNTESTABLE,
            }[disposition]
            budget.checkpoint(FastGateStage.RISK_COMMIT)
            return self._commit(
                run_id=run_id,
                disposition=disposition,
                reasons=reasons,
                evidence=evidence,
                deadline=budget,
                artifact=artifact,
                trial_id=trial.id,
                grant=grant,
                shadow=shadow,
                trial_result={
                    "result": str(trial_result),
                    "p_value": str(p_value),
                    "global_alpha": str(global_allocation.alpha),
                    "family_alpha": str(family_allocation.alpha),
                    "reason_codes": reasons,
                },
            )
        except DeadlineExceeded:
            return self._commit(
                run_id=run_id,
                disposition=ResearchDisposition.UNTESTABLE,
                reasons=["deadline_exceeded"],
                evidence=evidence,
                deadline=budget,
                artifact=artifact,
                trial_id=trial.id if trial else None,
                deadline_exceeded=True,
                trial_result=(
                    {
                        "result": str(TrialResult.UNTESTABLE),
                        "reason_codes": ["deadline_exceeded"],
                    }
                    if trial is not None
                    else None
                ),
            )
        except StrategyDSLValidationError:
            return self._commit(
                run_id=run_id,
                disposition=ResearchDisposition.REJECTED,
                reasons=["semantic_validation_failed"],
                evidence=evidence,
                deadline=budget,
                artifact=artifact,
                trial_id=trial.id if trial else None,
                trial_result=(
                    {
                        "result": str(TrialResult.FAIL),
                        "reason_codes": ["semantic_validation_failed"],
                    }
                    if trial is not None
                    else None
                ),
            )
        except Exception:
            return self._commit(
                run_id=run_id,
                disposition=ResearchDisposition.UNTESTABLE,
                reasons=["research_infrastructure_failure"],
                evidence=evidence,
                deadline=budget,
                artifact=artifact,
                trial_id=trial.id if trial else None,
                trial_result=(
                    {
                        "result": str(TrialResult.UNTESTABLE),
                        "reason_codes": ["research_infrastructure_failure"],
                    }
                    if trial is not None
                    else None
                ),
            )


class ShadowEvidence(BaseModel, frozen=True):
    effective_sample_size: Decimal = Field(ge=0)
    independent_signal_episodes: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    regime_coverage: tuple[str, ...]
    stress_coverage: tuple[str, ...]
    cost_error_bps: Decimal = Field(ge=0)
    slippage_error_bps: Decimal = Field(ge=0)
    signal_decay_ratio: Decimal = Field(ge=0)
    calibrated: bool
    cross_market_passed: bool = False
    purged_walk_forward_passed: bool = False
    embargo_passed: bool = False
    out_of_fold_passed: bool = False
    block_bootstrap_passed: bool = False
    deflated_sharpe_probability: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    pbo_upper_bound: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    online_fdr_passed: bool = False
    cost_stress_passed: bool = False
    parameter_stability_passed: bool = False
    independent_blind_reference: str | None = None
    forward_evidence_reference: str | None = None


class SlowCertificationDecision(BaseModel, frozen=True):
    passed: bool
    reason_codes: tuple[str, ...]


class SlowCertificationInput(BaseModel, frozen=True):
    market_pnls: dict[str, list[Decimal]]
    market_initial_risks: dict[str, list[Decimal]]
    purged_oof_expected_rs: tuple[Decimal, ...]
    stressed_expected_rs: tuple[Decimal, ...]
    parameter_variant_expected_rs: tuple[Decimal, ...]
    embargo_bars: int = Field(ge=1)
    independent_signal_episodes: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    regime_coverage: tuple[str, ...]
    stress_coverage: tuple[str, ...]
    cost_error_bps: Decimal = Field(ge=0)
    slippage_error_bps: Decimal = Field(ge=0)
    signal_decay_ratio: Decimal = Field(ge=0)
    calibrated: bool
    online_fdr_passed: bool
    independent_blind_reference: str | None = None
    forward_evidence_reference: str | None = None


class SlowCertificationEngine:
    def evaluate_empirical(
        self,
        inputs: SlowCertificationInput,
        *,
        artifact_fingerprint: str,
    ) -> tuple[ShadowEvidence, SlowCertificationDecision]:
        markets = sorted(inputs.market_pnls)
        risk_markets = sorted(inputs.market_initial_risks)
        if markets != risk_markets:
            raise ValueError("slow certification market PnL/risk scopes differ")
        combined_pnls: list[Decimal] = []
        combined_risks: list[Decimal] = []
        market_means: list[Decimal] = []
        for market in markets:
            pnls = inputs.market_pnls[market]
            risks = inputs.market_initial_risks[market]
            if len(pnls) != len(risks):
                raise ValueError("slow certification PnL/risk observations differ")
            values = [pnl / risk for pnl, risk in zip(pnls, risks, strict=True) if risk > 0]
            market_means.append(
                sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")
            )
            combined_pnls.extend(pnls)
            combined_risks.extend(risks)
        bootstrap = self.expected_r_evidence(
            combined_pnls,
            combined_risks,
            artifact_fingerprint=artifact_fingerprint,
        )
        fold_count = len(inputs.purged_oof_expected_rs)
        positive_folds = sum(value > 0 for value in inputs.purged_oof_expected_rs)
        pbo = (
            Decimal(fold_count - positive_folds) / Decimal(fold_count)
            if fold_count
            else Decimal("1")
        )
        evidence = ShadowEvidence(
            effective_sample_size=Decimal(
                min(inputs.independent_signal_episodes, inputs.trade_count)
            ),
            independent_signal_episodes=inputs.independent_signal_episodes,
            trade_count=inputs.trade_count,
            regime_coverage=inputs.regime_coverage,
            stress_coverage=inputs.stress_coverage,
            cost_error_bps=inputs.cost_error_bps,
            slippage_error_bps=inputs.slippage_error_bps,
            signal_decay_ratio=inputs.signal_decay_ratio,
            calibrated=inputs.calibrated,
            cross_market_passed=len(market_means) >= 2 and all(value > 0 for value in market_means),
            purged_walk_forward_passed=fold_count >= 4
            and positive_folds >= (fold_count * 3 + 3) // 4,
            embargo_passed=inputs.embargo_bars >= 1,
            out_of_fold_passed=fold_count >= 4 and positive_folds == fold_count,
            block_bootstrap_passed=bootstrap.passed,
            deflated_sharpe_probability=Decimal("1") - bootstrap.p_value,
            pbo_upper_bound=pbo,
            online_fdr_passed=inputs.online_fdr_passed,
            cost_stress_passed=bool(inputs.stressed_expected_rs)
            and all(value > 0 for value in inputs.stressed_expected_rs),
            parameter_stability_passed=bool(inputs.parameter_variant_expected_rs)
            and all(value > 0 for value in inputs.parameter_variant_expected_rs),
            independent_blind_reference=inputs.independent_blind_reference,
            forward_evidence_reference=inputs.forward_evidence_reference,
        )
        return evidence, self.evaluate(evidence)

    def evaluate(self, evidence: ShadowEvidence) -> SlowCertificationDecision:
        reasons: list[str] = []
        if evidence.effective_sample_size < 30:
            reasons.append("ess_below_30")
        if evidence.trade_count < 30 or evidence.independent_signal_episodes < 30:
            reasons.append("insufficient_independent_episodes")
        if not evidence.regime_coverage or not evidence.stress_coverage:
            reasons.append("regime_or_stress_coverage_missing")
        if not evidence.calibrated:
            reasons.append("forward_calibration_failed")
        if evidence.cost_error_bps > Decimal("10"):
            reasons.append("realized_cost_model_error_too_large")
        if evidence.slippage_error_bps > Decimal("10"):
            reasons.append("realized_slippage_model_error_too_large")
        if evidence.signal_decay_ratio < Decimal("0.50"):
            reasons.append("forward_signal_decay_too_large")
        if not evidence.purged_walk_forward_passed:
            reasons.append("purged_walk_forward_failed")
        if not evidence.cross_market_passed:
            reasons.append("cross_market_validation_failed")
        if not evidence.embargo_passed:
            reasons.append("embargo_validation_failed")
        if not evidence.out_of_fold_passed:
            reasons.append("out_of_fold_validation_failed")
        if not evidence.block_bootstrap_passed:
            reasons.append("slow_5000_block_bootstrap_failed")
        if evidence.deflated_sharpe_probability < Decimal("0.90"):
            reasons.append("deflated_sharpe_probability_below_threshold")
        if evidence.pbo_upper_bound > Decimal("0.25"):
            reasons.append("pbo_upper_bound_above_threshold")
        if not evidence.online_fdr_passed:
            reasons.append("online_fdr_not_passed")
        if not evidence.cost_stress_passed:
            reasons.append("cost_stress_failed")
        if not evidence.parameter_stability_passed:
            reasons.append("parameter_stability_failed")
        if evidence.independent_blind_reference is None:
            reasons.append("independent_blind_evidence_missing")
        if evidence.forward_evidence_reference is None:
            reasons.append("forward_shadow_evidence_missing")
        return SlowCertificationDecision(passed=not reasons, reason_codes=tuple(reasons))

    def expected_r_evidence(
        self,
        pnls: list[Decimal],
        initial_risks: list[Decimal],
        *,
        artifact_fingerprint: str,
    ):
        return moving_block_expected_r(
            pnls,
            initial_risks,
            samples=5_000,
            minimum_observations=30,
            seed_material=artifact_fingerprint,
        )

    def certify(
        self,
        *,
        strategy_ir_hash: str,
        artifact_fingerprint: str,
        slow_validation_fingerprint: str,
        evidence: ShadowEvidence,
        generator_provenance: GeneratorProvenance | None,
        build_identity: RuntimeBuildIdentity,
        store: Any | None = None,
    ) -> FullStrategyCertificate:
        decision = self.evaluate(evidence)
        if not decision.passed:
            raise ValueError(
                "slow certification evidence is incomplete: " + ", ".join(decision.reason_codes)
            )
        if not build_identity.formal_certificate_eligible:
            raise ValueError("dirty build identity cannot issue a full strategy certificate")
        certificate = FullStrategyCertificate(
            strategy_ir_hash=strategy_ir_hash,
            artifact_fingerprint=artifact_fingerprint,
            generator_provenance=generator_provenance,
            slow_validation_fingerprint=slow_validation_fingerprint,
            independent_blind_evidence_reference=evidence.independent_blind_reference,
            forward_shadow_evidence_reference=evidence.forward_evidence_reference,
            build_identity=build_identity,
        )
        if store is not None:
            store.save_research_certificate(certificate, "full_strategy_certificate")
        return certificate


class PortfolioGateLiteEvidence(BaseModel):
    component_expected_return: Decimal
    marginal_sharpe: Decimal
    cvar_after: Decimal
    cvar_limit: Decimal
    maximum_drawdown_after: Decimal
    maximum_drawdown_limit: Decimal
    signal_overlap: Decimal = Field(ge=0, le=1)
    pnl_overlap: Decimal = Field(ge=-1, le=1)
    trade_overlap: Decimal = Field(ge=0, le=1)
    exposure_overlap: Decimal = Field(ge=0, le=1)


def portfolio_gate_lite(evidence: PortfolioGateLiteEvidence) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if evidence.component_expected_return < 0:
        reasons.append("component_has_negative_expectation")
    if evidence.marginal_sharpe <= 0:
        reasons.append("no_positive_portfolio_marginal_value")
    if evidence.cvar_after > evidence.cvar_limit:
        reasons.append("portfolio_cvar_limit_exceeded")
    if evidence.maximum_drawdown_after > evidence.maximum_drawdown_limit:
        reasons.append("portfolio_drawdown_limit_exceeded")
    return not reasons, reasons
