from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from uuid import uuid4

from pydantic import BaseModel, Field


class StrategyReviewJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    PAUSED = "paused"


class ReviewScopeResult(BaseModel):
    symbol: str
    timeframe: str
    role: str = "origin"
    status: str = "queued"
    stage: str = "queued"
    dataset_id: str | None = None
    dataset_fingerprint: str | None = None
    metrics: dict = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class StrategyReviewJob(BaseModel):
    """Durable review of one immutable candidate version.

    Candidate identity and evidence identity are deliberately separate.  A retry
    may replace infrastructure evidence, but it may never silently reinterpret a
    different StrategySpec as the candidate that was originally queued.
    """

    id: str = Field(default_factory=lambda: f"review_{uuid4().hex[:16]}")
    candidate_id: str
    candidate_fingerprint: str
    review_fingerprint: str
    review_contract_version: int = Field(default=1, ge=1)
    policy_fingerprint: str = ""
    origin_symbol: str = ""
    origin_timeframe: str = ""
    trial_count: int = Field(default=1, ge=1)
    scope_results: list[ReviewScopeResult] = Field(default_factory=list)
    entry_profile: str = "balanced_5m_v1"
    status: StrategyReviewJobStatus = StrategyReviewJobStatus.QUEUED
    stage: str = "queued"
    evidence_tier: str = "unreviewed"
    progress_percent: int = Field(default=0, ge=0, le=100)
    dataset_id: str | None = None
    dataset_fingerprint: str | None = None
    parent_candidate_id: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    error: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def review_failure_detail(exc: Exception) -> str:
    """Never persist an empty infrastructure failure explanation."""

    message = str(exc).strip() or "exception did not provide a message"
    return f"{type(exc).__name__}: {message}"[:1000]


def should_retry_review_after_restart(
    job: StrategyReviewJob,
    *,
    max_attempts: int = 3,
) -> bool:
    """Retry interrupted and legacy opaque infrastructure failures once code restarts."""

    if job.attempt_count >= max_attempts:
        return False
    if job.status == StrategyReviewJobStatus.RUNNING:
        return True
    if job.status != StrategyReviewJobStatus.BLOCKED:
        return False
    normalized = (job.error or "").strip().lower()
    return not normalized or normalized.startswith(
        ("timeouterror:", "connectionerror:", "httpx.", "oserror:")
    )


def strategy_review_fingerprint(
    candidate_fingerprint: str,
    *,
    entry_profile: str,
    candidate_id: str = "",
    symbol: str = "",
    timeframe: str = "",
    policy_fingerprint: str = "",
    review_contract_version: int = 1,
) -> str:
    if review_contract_version <= 1:
        payload = {
            "candidate_fingerprint": candidate_fingerprint,
            "entry_profile": entry_profile,
            "review_contract": "candidate_review_v1",
        }
    else:
        if not all((candidate_id, symbol, timeframe, policy_fingerprint)):
            raise ValueError("v2 review fingerprint requires candidate, scope and policy")
        payload = {
            "candidate_id": candidate_id,
            "candidate_fingerprint": candidate_fingerprint,
            "symbol": symbol,
            "timeframe": timeframe,
            "entry_profile": entry_profile,
            "policy_fingerprint": policy_fingerprint,
            "review_contract": f"candidate_review_v{review_contract_version}",
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]
