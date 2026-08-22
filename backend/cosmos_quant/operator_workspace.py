from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .agent.schemas import AgentAutonomyMode
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


class AgentLaunchRequest(BaseModel):
    profile_id: str = Field(default="primary-auto-trader", min_length=3, max_length=80)
    symbols: list[str] = Field(min_length=1, max_length=30)
    timeframes: list[str] = Field(min_length=1, max_length=12)
    autonomy_mode: AgentAutonomyMode
    risk_template: str = Field(default="platform-default", min_length=3, max_length=80)
    objective: str = Field(
        default="持续感知授权市场，仅在策略验证与硬风控通过后自动交易",
        min_length=8,
        max_length=1000,
    )
    poll_seconds: int = Field(default=60, ge=15, le=3600)
    authorize: bool = False
    preview_fingerprint: str | None = None

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper().replace("-", "/") for value in values]
        if any("/" not in value for value in normalized):
            raise ValueError("symbols must use BASE/QUOTE notation")
        return list(dict.fromkeys(normalized))

    @field_validator("timeframes")
    @classmethod
    def normalize_timeframes(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().lower() for value in values))


class AgentLaunchProfile(BaseModel):
    id: str
    goal_id: str | None = None
    policy_id: str
    symbols: list[str]
    timeframes: list[str]
    autonomy_mode: AgentAutonomyMode
    risk_template: str = "platform-default"
    objective: str
    poll_seconds: int
    grant_fingerprint: str
    authorized_fingerprint: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentLaunchPreview(BaseModel):
    profile_id: str
    grant_fingerprint: str
    confirmation_required: bool
    reuses_goal_id: str | None = None
    mode: AgentAutonomyMode
    symbols: list[str]
    timeframes: list[str]
    risk_summary: dict[str, str | int | bool]
    blocking_reasons: list[str] = Field(default_factory=list)


class AgentLaunchResult(BaseModel):
    profile: AgentLaunchProfile
    goal_id: str
    reused: bool
    status: str


class AgentMarketSense(BaseModel):
    symbol: str
    timeframe: str
    status: str
    last_event_at: datetime | None = None
    next_scan_at: datetime | None = None
    last_run_status: str | None = None


class AgentStrategySummary(BaseModel):
    plan_id: str
    name: str
    symbol: str
    timeframe: str
    family: str
    status: str
    source: str
    fingerprint: str
    updated_at: datetime


class AgentGoalOverview(BaseModel):
    goal_id: str
    objective: str
    status: str
    autonomy_mode: str
    symbols: list[str]
    timeframes: list[str]
    runtime_status: Literal["running", "waiting", "paused", "blocked", "offline"]
    last_heartbeat_at: datetime | None = None
    next_scan_at: datetime | None = None
    last_action: str | None = None
    last_action_summary: str | None = None
    current_strategy: AgentStrategySummary | None = None
    recently_generated_strategies: list[AgentStrategySummary] = Field(default_factory=list)
    market_sense: list[AgentMarketSense] = Field(default_factory=list)
    active_episode_count: int = 0
    uncertain_episode_count: int = 0
    blocking_reasons: list[str] = Field(default_factory=list)


class AgentRuntimeOverview(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    active_goal_count: int
    latest_heartbeat_at: datetime | None = None
    risk_halted: bool
    risk_reason: str = ""
    goals: list[AgentGoalOverview]


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
