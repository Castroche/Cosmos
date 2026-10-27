from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .domain import AuditEvent


def stable_fingerprint(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:20]


class ReviewJobView(BaseModel):
    id: str
    candidate_id: str | None = None
    candidate_name: str | None = None
    candidate_fingerprint: str | None = None
    review_contract_version: int = 1
    policy_fingerprint: str = ""
    origin_scope: dict[str, str] = Field(default_factory=dict)
    scope_results: list[dict] = Field(default_factory=list)
    status: str
    stage: str
    evidence_tier: str
    progress_percent: int = 0
    report_id: str | None = None
    candidate_count: int = 0
    selected_strategy_id: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    dataset_id: str | None = None
    attempt_count: int = 0
    error: str | None = None
    updated_at: datetime


class ReviewOverview(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    queued: int = 0
    unreviewed: int = 0
    running: int = 0
    blocked: int = 0
    paper_eligible: int = 0
    sandbox_canary: int = 0
    sandbox_mature: int = 0
    live_capped: int = 0
    rejected: int = 0
    legacy: int = 0
    data_blocked: int = 0
    jobs: list[ReviewJobView] = Field(default_factory=list)
    pipeline: dict[str, str | int | bool] = Field(default_factory=dict)


class ReviewEvidenceItem(BaseModel):
    id: str
    kind: Literal["strategy", "campaign"] = "strategy"
    name: str
    status: str
    evidence_tier: str = "unreviewed"
    source: str = "unknown"
    family: str = "unknown"
    summary: str = ""
    reasons: list[str] = Field(default_factory=list)
    metrics: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    technical_details: str = ""
    review_contract_version: int = 1
    origin_scope: dict[str, str] = Field(default_factory=dict)
    scope_results: list[dict] = Field(default_factory=list)
    updated_at: datetime


class PersonalStrategyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=1000)
    strategy_spec: dict
    origin_ref: str | None = None


class PersonalStrategyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    strategy_spec: dict | None = None


class PersonalStrategyRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"personal_{uuid4().hex[:16]}")
    name: str
    description: str = ""
    family: str
    status: Literal["draft", "active", "archived"] = "draft"
    version: int = 1
    strategy_spec: dict
    spec_fingerprint: str
    origin_ref: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StrategyLibraryItem(BaseModel):
    id: str
    name: str
    description: str = ""
    origin: Literal["builtin", "personal", "agent"]
    family: str
    status: str
    version: int | str
    strategy_spec: dict
    spec_fingerprint: str
    executable: bool
    evidence_tier: str = "unreviewed"
    executable_environments: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    invalidation_reason: str = ""
    hidden: bool = False
    can_edit: bool = False
    can_archive: bool = False
    can_restore: bool = False
    can_delete: bool = False
    can_clone: bool = True
    evidence_summary: str = ""
    updated_at: datetime | None = None


class StrategyLibraryPage(BaseModel):
    items: list[StrategyLibraryItem]
    total: int


class StrategyLibraryPreference(BaseModel):
    hidden: bool


class AuditEventPage(BaseModel):
    items: list[AuditEvent]
    next_cursor: int | None = None
