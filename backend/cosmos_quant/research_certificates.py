from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .build_identity import RuntimeBuildIdentity


class ArtifactFingerprintV2(BaseModel, frozen=True):
    version: Literal[2] = 2
    dataset_fingerprint: str
    dataset_schema_version: int
    dataset_availability_policy: str
    feature_ast_hash: str
    ir_hash: str
    operator_versions: tuple[str, ...]
    operator_policy_version: str
    market_scope: tuple[str, ...]
    point_in_time_policy: str
    fast_engine_version: str
    exact_engine_version: str
    cost_model_version: str
    cost_parameters_fingerprint: str
    slippage_model_version: str
    slippage_parameters_fingerprint: str
    compiler_version: str
    purge_bars: int
    embargo_bars: int
    validation_config_fingerprint: str
    git_commit_sha: str
    source_tree_hash: str
    working_tree_dirty: bool

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return sha256(payload).hexdigest()


class GeneratorProvenance(BaseModel, frozen=True):
    generator_id: str
    generator_version: str
    policy_version: str
    hypothesis_hash: str
    random_seed: int
    generation_fingerprint: str
    envelope_fingerprint: str
    output_ir_hash: str


class GeneratorEnvelopeCertificate(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"generator_envelope_{uuid4().hex}")
    generator_id: str
    generator_version: str
    policy_version: str
    allowed_families: tuple[str, ...]
    allowed_features: tuple[str, ...] = ()
    allowed_operators: tuple[str, ...]
    maximum_nodes: int
    maximum_ast_depth: int = Field(default=16, ge=1, le=64)
    maximum_lookback: int
    parameter_bounds: tuple[tuple[str, str, str], ...] = ()
    markets: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    regimes: tuple[str, ...] = ()
    holding_horizon_bounds: tuple[int, int] = (1, 2_000)
    risk_budget_ceiling: Decimal = Field(default=Decimal("0.10"), gt=0, le=Decimal("0.10"))
    maximum_turnover: Decimal = Field(default=Decimal("10"), gt=0)
    maximum_leverage: Decimal = Field(default=Decimal("1"), ge=1)
    execution_models: tuple[str, ...] = ("existing_exact_event_backend",)
    maximum_empirical_trials_per_event: int = Field(default=3, ge=1, le=3)
    compiler_version: str = "dsl-v2"
    data_schema_version: int = Field(default=9, ge=1)
    cost_model_version: str = "linear-bps-v1"
    revocation_conditions: tuple[str, ...] = (
        "compiler_change",
        "feature_semantic_change",
        "data_source_change",
        "cost_or_risk_model_change",
        "regime_or_execution_deterioration",
    )
    build_identity: RuntimeBuildIdentity
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_formal_build_identity(self):
        if not self.build_identity.formal_certificate_eligible:
            raise ValueError("dirty build identity cannot issue a generator envelope")
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("generator envelope expiry must be after issue time")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"id"})
        return sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class ShadowCandidateArtifact(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"shadow_{uuid4().hex}")
    research_run_id: str
    hypothesis_id: str
    ir_hash: str
    artifact_fingerprint: str
    reason_codes: tuple[str, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FullStrategyCertificate(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"full_certificate_{uuid4().hex}")
    strategy_ir_hash: str
    artifact_fingerprint: str
    generator_provenance: GeneratorProvenance | None
    slow_validation_fingerprint: str
    independent_blind_evidence_reference: str
    forward_shadow_evidence_reference: str
    build_identity: RuntimeBuildIdentity
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionEligibilityGrant(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"eligibility_{uuid4().hex}")
    strategy_ir_hash: str
    artifact_fingerprint: str
    generator_envelope_fingerprint: str
    build_identity: RuntimeBuildIdentity
    evidence_tier: Literal[
        "paper_eligible", "sandbox_canary", "sandbox_mature", "live_capped"
    ] = "sandbox_canary"
    market_scope: tuple[str, ...] = ()
    revocation_conditions: tuple[str, ...] = (
        "certificate_expired",
        "strategy_or_data_fingerprint_changed",
        "market_data_stale",
        "execution_integrity_failure",
        "forward_performance_deteriorated",
    )
    target_timeframe_seconds: int = Field(gt=0)
    risk_budget_fraction: Decimal = Field(gt=0, le=Decimal("0.10"))
    maximum_active_instances: Literal[1] = 1
    allow_scale_in: Literal[False] = False
    allow_pyramiding: Literal[False] = False
    allow_automatic_leverage_increase: Literal[False] = False
    issued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime

    @classmethod
    def issue(
        cls,
        *,
        strategy_ir_hash: str,
        artifact_fingerprint: str,
        generator_envelope_fingerprint: str,
        target_timeframe_seconds: int,
        requested_risk_budget: Decimal,
        mature_reference_risk_budget: Decimal,
        build_identity: RuntimeBuildIdentity,
        evidence_tier: Literal[
            "paper_eligible", "sandbox_canary", "sandbox_mature", "live_capped"
        ] = "sandbox_canary",
        market_scope: tuple[str, ...] = (),
        issued_at: datetime | None = None,
    ) -> ExecutionEligibilityGrant:
        if not build_identity.formal_certificate_eligible:
            raise ValueError("dirty build identity cannot issue execution eligibility")
        issued = issued_at or datetime.now(UTC)
        # Five percent of mature risk is the default; ten percent is an absolute
        # ceiling. This is strategy risk / stop-loss budget, not notional size.
        permitted = min(
            requested_risk_budget,
            mature_reference_risk_budget * Decimal("0.05"),
            Decimal("0.10"),
        )
        if permitted <= 0:
            raise ValueError("eligibility risk budget must be positive")
        return cls(
            strategy_ir_hash=strategy_ir_hash,
            artifact_fingerprint=artifact_fingerprint,
            generator_envelope_fingerprint=generator_envelope_fingerprint,
            build_identity=build_identity,
            evidence_tier=evidence_tier,
            market_scope=market_scope,
            target_timeframe_seconds=target_timeframe_seconds,
            risk_budget_fraction=permitted,
            issued_at=issued,
            expires_at=issued + timedelta(seconds=target_timeframe_seconds),
        )

    def allows_new_risk(self, *, now: datetime | None = None, active_instances: int = 0) -> bool:
        current = now or datetime.now(UTC)
        return current < self.expires_at and active_instances < self.maximum_active_instances


class CertificateRevocationEvent(BaseModel, frozen=True):
    id: str = Field(default_factory=lambda: f"revocation_{uuid4().hex}")
    certificate_id: str
    reason_code: str
    evidence_reference: str
    revoked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CertificateRevalidationContext(BaseModel, frozen=True):
    compiler_unchanged: bool
    feature_semantics_unchanged: bool
    data_source_unchanged: bool
    cost_model_unchanged: bool
    risk_model_unchanged: bool
    regime_valid: bool
    spread_within_limit: bool
    slippage_within_limit: bool
    forward_evidence_valid: bool


def certificate_revocation_reasons(
    context: CertificateRevalidationContext,
) -> list[str]:
    checks = {
        "compiler_changed": context.compiler_unchanged,
        "feature_semantics_changed": context.feature_semantics_unchanged,
        "data_source_changed": context.data_source_unchanged,
        "cost_model_changed": context.cost_model_unchanged,
        "risk_model_changed": context.risk_model_unchanged,
        "regime_invalid": context.regime_valid,
        "spread_deteriorated": context.spread_within_limit,
        "slippage_deteriorated": context.slippage_within_limit,
        "forward_evidence_degraded": context.forward_evidence_valid,
    }
    return [reason for reason, passed in checks.items() if not passed]


def auto_revoke_certificate(
    store,
    *,
    certificate_id: str,
    context: CertificateRevalidationContext,
    evidence_reference: str,
) -> CertificateRevocationEvent | None:
    reasons = certificate_revocation_reasons(context)
    if not reasons:
        return None
    event = CertificateRevocationEvent(
        certificate_id=certificate_id,
        reason_code="|".join(reasons),
        evidence_reference=evidence_reference,
    )
    store.revoke_research_certificate(event)
    return event


def validate_generator_provenance(
    provenance: GeneratorProvenance,
    envelope: GeneratorEnvelopeCertificate,
    *,
    hypothesis_hash: str,
    output_ir_hash: str,
) -> list[str]:
    reasons: list[str] = []
    if not envelope.build_identity.formal_certificate_eligible:
        reasons.append("dirty_build_identity")
    if envelope.expires_at is not None and envelope.expires_at <= datetime.now(UTC):
        reasons.append("generator_envelope_expired")
    if provenance.generator_id != envelope.generator_id:
        reasons.append("generator_id_mismatch")
    if provenance.generator_version != envelope.generator_version:
        reasons.append("generator_version_mismatch")
    if provenance.policy_version != envelope.policy_version:
        reasons.append("generator_policy_mismatch")
    if provenance.envelope_fingerprint != envelope.fingerprint:
        reasons.append("generator_envelope_mismatch")
    if provenance.hypothesis_hash != hypothesis_hash:
        reasons.append("generator_hypothesis_mismatch")
    if provenance.output_ir_hash != output_ir_hash:
        reasons.append("generator_output_mismatch")
    expected_generation = sha256(
        json.dumps(
            {
                "generator_id": provenance.generator_id,
                "generator_version": provenance.generator_version,
                "policy_version": provenance.policy_version,
                "hypothesis_hash": provenance.hypothesis_hash,
                "random_seed": provenance.random_seed,
                "output_ir_hash": provenance.output_ir_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if provenance.generation_fingerprint != expected_generation:
        reasons.append("generation_fingerprint_invalid")
    return reasons
