from __future__ import annotations

import json
import math
import random
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from statistics import NormalDist
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResearchDisposition(StrEnum):
    EXECUTION_ELIGIBLE = "execution_eligible"
    SHADOW_ONLY = "shadow_only"
    REJECTED = "rejected"
    UNTESTABLE = "untestable"


class TrialType(StrEnum):
    SEMANTIC = "semantic"
    EMPIRICAL = "empirical"
    SHADOW = "shadow"
    BLIND = "blind"


class TrialResult(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    FAIL = "fail"
    UNTESTABLE = "untestable"
    SHADOW_REQUIRED = "shadow_required"


class CertificationOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SHADOW_REQUIRED = "shadow_required"


class RedactedCertificationResult(BaseModel):
    """The only blind result shape available to a Research Agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: CertificationOutcome
    reason_codes: list[str] = Field(default_factory=list)
    evidence_reference: str


class ExpectedREvidence(BaseModel):
    observation_count: int = Field(ge=0)
    expected_r: Decimal
    cost_adjusted_expectancy: Decimal
    confidence_lower_bound: Decimal
    p_value: Decimal = Field(ge=0, le=1)
    bootstrap_samples: int = Field(ge=0)
    block_length: int = Field(ge=0)
    testable: bool
    passed: bool
    reasons: list[str] = Field(default_factory=list)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def moving_block_expected_r(
    pnls: list[Decimal],
    initial_risks: list[Decimal],
    *,
    samples: int = 500,
    confidence: Decimal = Decimal("0.95"),
    minimum_observations: int = 20,
    seed_material: str = "cosmos-expected-r-v1",
) -> ExpectedREvidence:
    """Deterministic circular moving-block inference for net trade R.

    PnL is already net of fee, slippage and funding in the exact engine. A
    non-positive or missing initial risk cannot be silently replaced with a
    notional proxy and therefore makes the evidence untestable.
    """

    if len(pnls) != len(initial_risks):
        raise ValueError("PnL and initial-risk observations must have the same length")
    if samples < 100:
        raise ValueError("expected-R bootstrap requires at least 100 samples")
    if not Decimal("0.5") < confidence < Decimal("1"):
        raise ValueError("bootstrap confidence must be between 0.5 and 1")
    if not pnls:
        return ExpectedREvidence(
            observation_count=0,
            expected_r=Decimal("0"),
            cost_adjusted_expectancy=Decimal("0"),
            confidence_lower_bound=Decimal("0"),
            p_value=Decimal("1"),
            bootstrap_samples=0,
            block_length=0,
            testable=False,
            passed=False,
            reasons=["no resolved trade episodes"],
        )
    if any(risk <= 0 for risk in initial_risks):
        return ExpectedREvidence(
            observation_count=len(pnls),
            expected_r=Decimal("0"),
            cost_adjusted_expectancy=Decimal("0"),
            confidence_lower_bound=Decimal("0"),
            p_value=Decimal("1"),
            bootstrap_samples=0,
            block_length=0,
            testable=False,
            passed=False,
            reasons=["one or more trades have no positive pre-committed initial risk"],
        )
    values = [pnl / risk for pnl, risk in zip(pnls, initial_risks, strict=True)]
    count = len(values)
    expected = sum(values, Decimal("0")) / Decimal(count)
    expectancy = sum(pnls, Decimal("0")) / Decimal(count)
    if count < minimum_observations:
        return ExpectedREvidence(
            observation_count=count,
            expected_r=_quantize(expected),
            cost_adjusted_expectancy=expectancy.quantize(Decimal("0.01")),
            confidence_lower_bound=Decimal("0"),
            p_value=Decimal("1"),
            bootstrap_samples=0,
            block_length=0,
            testable=False,
            passed=False,
            reasons=[f"effective trade sample {count} is below {minimum_observations}"],
        )
    block_length = max(2, math.ceil(count ** (1 / 3)))
    block_length = min(block_length, max(2, count // 4))
    seed = int.from_bytes(sha256(seed_material.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)

    def sample_mean(population: list[Decimal]) -> Decimal:
        selected: list[Decimal] = []
        while len(selected) < count:
            start = rng.randrange(count)
            selected.extend(
                population[(start + offset) % count] for offset in range(block_length)
            )
        return sum(selected[:count], Decimal("0")) / Decimal(count)

    bootstrap_means = sorted(sample_mean(values) for _ in range(samples))
    lower_index = max(0, math.floor(samples * float(Decimal("1") - confidence)))
    lower_bound = bootstrap_means[min(lower_index, samples - 1)]
    centered = [value - expected for value in values]
    null_exceedances = sum(sample_mean(centered) >= expected for _ in range(samples))
    p_value = Decimal(null_exceedances + 1) / Decimal(samples + 1)
    passed = lower_bound > 0
    return ExpectedREvidence(
        observation_count=count,
        expected_r=_quantize(expected),
        cost_adjusted_expectancy=expectancy.quantize(Decimal("0.01")),
        confidence_lower_bound=_quantize(lower_bound),
        p_value=_quantize(p_value),
        bootstrap_samples=samples,
        block_length=block_length,
        testable=True,
        passed=passed,
        reasons=(
            ["net Expected-R moving-block confidence bound is positive"]
            if passed
            else ["net Expected-R moving-block confidence bound is not positive"]
        ),
    )


class PermanentTrialRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"trial_{uuid4().hex[:20]}")
    idempotency_key: str = Field(min_length=16, max_length=128)
    research_event_id: str
    campaign_id: str | None = None
    hypothesis_id: str
    family_id: str
    candidate_id: str
    parent_trial_id: str | None = None
    trial_type: TrialType
    touched_returns: bool = False
    touched_blind: bool = False
    touched_shadow: bool = False
    dataset_fingerprint: str
    validation_fingerprint: str
    artifact_fingerprint: str
    global_alpha: Decimal | None = Field(default=None, ge=0, le=1)
    family_alpha: Decimal | None = Field(default=None, ge=0, le=1)
    p_value: Decimal | None = Field(default=None, ge=0, le=1)
    adjusted_p_value: Decimal | None = Field(default=None, ge=0, le=1)
    dsr: Decimal | None = Field(default=None, ge=0, le=1)
    pbo: Decimal | None = Field(default=None, ge=0, le=1)
    result: TrialResult = TrialResult.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def empirical(
        cls,
        *,
        research_event_id: str,
        campaign_id: str | None,
        hypothesis_id: str,
        family_id: str,
        candidate_id: str,
        dataset_fingerprint: str,
        validation_fingerprint: str,
        artifact_fingerprint: str,
        parent_trial_id: str | None = None,
    ) -> PermanentTrialRecord:
        identity = {
            "research_event_id": research_event_id,
            "hypothesis_id": hypothesis_id,
            "family_id": family_id,
            "candidate_id": candidate_id,
            "dataset_fingerprint": dataset_fingerprint,
            "validation_fingerprint": validation_fingerprint,
            "artifact_fingerprint": artifact_fingerprint,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        return cls(
            idempotency_key=sha256(encoded).hexdigest(),
            research_event_id=research_event_id,
            campaign_id=campaign_id,
            hypothesis_id=hypothesis_id,
            family_id=family_id,
            candidate_id=candidate_id,
            parent_trial_id=parent_trial_id,
            trial_type=TrialType.EMPIRICAL,
            touched_returns=True,
            dataset_fingerprint=dataset_fingerprint,
            validation_fingerprint=validation_fingerprint,
            artifact_fingerprint=artifact_fingerprint,
        )


class OnlineFDRAllocation(BaseModel):
    stream_id: str
    sequence: int = Field(ge=1)
    alpha: Decimal = Field(ge=0, le=1)
    method: str
    idempotency_key: str


class OnlineFDRSnapshot(BaseModel):
    stream_id: str
    scope: str
    target_fdr: Decimal = Field(gt=0, lt=1)
    initial_wealth: Decimal = Field(gt=0, lt=1)
    next_sequence: int = Field(ge=1)
    rejection_sequences: list[int] = Field(default_factory=list)
    pending_sequences: list[int] = Field(default_factory=list)
    method: str = "lord_plus_plus_power_law_v1"
    degraded: bool = False

    @model_validator(mode="after")
    def validate_ordered_stream_state(self):
        pending = self.pending_sequences
        rejected = self.rejection_sequences
        if len(pending) != len(set(pending)) or len(rejected) != len(set(rejected)):
            raise ValueError("online FDR sequences must be unique")
        if any(item <= 0 or item >= self.next_sequence for item in [*pending, *rejected]):
            raise ValueError("online FDR sequence is outside allocated history")
        if set(pending).intersection(rejected):
            raise ValueError("online FDR sequence cannot be both pending and rejected")
        if self.initial_wealth > self.target_fdr:
            raise ValueError("online FDR initial wealth exceeds target")
        return self

    @property
    def current_wealth(self) -> Decimal:
        spent_floor = sum(
            (lord_plus_plus_alpha(self, sequence) for sequence in range(1, self.next_sequence)),
            Decimal("0"),
        )
        replenished = self.target_fdr * Decimal(len(self.rejection_sequences))
        return max(self.initial_wealth + replenished - spent_floor, Decimal("0"))


def _gamma(sequence: int) -> Decimal:
    if sequence <= 0:
        return Decimal("0")
    # A conservative power-law spending sequence retains useful late-stream
    # resolution. 0.4 * sum(k^-1.6) is below one, leaving unspent safety margin.
    return Decimal(str(0.4 / (sequence**1.6)))


def lord_plus_plus_alpha(snapshot: OnlineFDRSnapshot, sequence: int) -> Decimal:
    if snapshot.degraded:
        return (snapshot.target_fdr * _gamma(sequence)).quantize(Decimal("0.00000001"))
    alpha = snapshot.initial_wealth * _gamma(sequence)
    for discovery_index, discovery_sequence in enumerate(snapshot.rejection_sequences):
        if discovery_sequence >= sequence:
            continue
        reward = (
            snapshot.target_fdr - snapshot.initial_wealth
            if discovery_index == 0
            else snapshot.target_fdr
        )
        alpha += reward * _gamma(sequence - discovery_sequence)
    return min(max(alpha, Decimal("0")), snapshot.target_fdr).quantize(
        Decimal("0.00000001")
    )


class OnlineFDRController:
    """Persistent LORD++ controller backed by SqliteStore atomic methods."""

    def __init__(self, store: Any, *, target_fdr: Decimal = Decimal("0.05")):
        if not Decimal("0") < target_fdr < Decimal("1"):
            raise ValueError("target FDR must be between zero and one")
        self.store = store
        self.target_fdr = target_fdr

    def allocate_alpha(
        self,
        stream_id: str,
        *,
        scope: str,
        idempotency_key: str,
    ) -> OnlineFDRAllocation:
        payload = self.store.allocate_online_fdr(
            stream_id=stream_id,
            scope=scope,
            target_fdr=self.target_fdr,
            idempotency_key=idempotency_key,
        )
        return OnlineFDRAllocation.model_validate(payload)

    def record_result(
        self,
        allocation: OnlineFDRAllocation,
        *,
        p_value: Decimal,
    ) -> OnlineFDRSnapshot:
        payload = self.store.record_online_fdr_result(
            stream_id=allocation.stream_id,
            sequence=allocation.sequence,
            idempotency_key=allocation.idempotency_key,
            p_value=p_value,
        )
        return OnlineFDRSnapshot.model_validate(payload)

    def snapshot(self, stream_id: str) -> OnlineFDRSnapshot | None:
        payload = self.store.load_online_fdr_snapshot(stream_id)
        return OnlineFDRSnapshot.model_validate(payload) if payload else None

    def restore(self, snapshot: OnlineFDRSnapshot) -> None:
        self.store.restore_online_fdr_snapshot(snapshot.model_dump(mode="json"))


def combine_global_family_alpha(
    global_allocation: OnlineFDRAllocation,
    family_allocation: OnlineFDRAllocation,
) -> Decimal:
    return min(global_allocation.alpha, family_allocation.alpha)


def normal_one_sided_positive_mean_p_value(values: list[Decimal]) -> Decimal:
    """Fallback descriptive p-value used only when bootstrap evidence is available."""

    if len(values) < 2:
        return Decimal("1")
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    if variance <= 0:
        return Decimal("0") if mean > 0 else Decimal("1")
    z = float(mean) / math.sqrt(float(variance) / len(values))
    return Decimal(str(1 - NormalDist().cdf(z))).quantize(Decimal("0.0001"))
