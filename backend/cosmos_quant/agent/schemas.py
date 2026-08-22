from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class AgentAutonomyMode(StrEnum):
    OBSERVE = "observe"
    RESEARCH = "research"
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE_CAPPED = "live_capped"


class AgentGoalStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class AgentEventType(StrEnum):
    CANDLE_CLOSED = "candle_closed"
    MARKET_REGIME_CHANGED = "market_regime_changed"
    MARKET_DATA_STALE = "market_data_stale"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELED = "order_canceled"
    ORDER_UNCERTAIN = "order_uncertain"
    POSITION_OPENED = "position_opened"
    POSITION_UPDATED = "position_updated"
    POSITION_CLOSED = "position_closed"
    RISK_WARNING = "risk_warning"
    RISK_HALTED = "risk_halted"
    SCHEDULE_TRIGGERED = "schedule_triggered"
    TOOL_COMPLETED = "tool_completed"
    MANUAL_TRIGGERED = "manual_triggered"


class AgentEventStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PROCESSED = "processed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class AgentAction(StrEnum):
    OBSERVE = "observe"
    ANALYZE = "analyze"
    RESEARCH = "research"
    BACKTEST = "backtest"
    PROPOSE_ENTRY = "propose_entry"
    EXECUTE_ENTRY = "execute_entry"
    WAIT = "wait"
    CANCEL_ENTRY = "cancel_entry"
    REDUCE_POSITION = "reduce_position"
    EXIT_POSITION = "exit_position"
    HALT = "halt"
    REVIEW = "review"


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    HALTED = "halted"


class ToolRiskClass(StrEnum):
    READ_ONLY = "read_only"
    RESEARCH = "research"
    INCREASE_RISK = "increase_risk"
    DECREASE_RISK = "decrease_risk"
    CONTROL = "control"


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    STRATEGY = "strategy"
    OPERATIONAL = "operational"


class AgentGoal(BaseModel):
    id: str = Field(default_factory=lambda: _id("goal"))
    objective: str = Field(min_length=8, max_length=1000)
    symbols: list[str] = Field(min_length=1)
    timeframes: list[str] = Field(min_length=1)
    autonomy_mode: AgentAutonomyMode = AgentAutonomyMode.OBSERVE
    risk_policy_id: str
    poll_seconds: int = Field(default=60, ge=15, le=3600)
    status: AgentGoalStatus = AgentGoalStatus.ACTIVE
    valid_until: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper().replace("-", "/") for value in values]
        if any("/" not in value for value in normalized):
            raise ValueError("agent symbols must use canonical BASE/QUOTE notation")
        return list(dict.fromkeys(normalized))

    @field_validator("timeframes")
    @classmethod
    def normalize_timeframes(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().lower() for value in values))


class AgentEvent(BaseModel):
    id: str = Field(default_factory=lambda: _id("aevt"))
    event_type: AgentEventType
    source: str = Field(min_length=1, max_length=100)
    goal_id: str | None = None
    dedupe_key: str = Field(min_length=4, max_length=300)
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentToolCall(BaseModel):
    id: str = Field(default_factory=lambda: _id("tool"))
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentDecision(BaseModel):
    id: str = Field(default_factory=lambda: _id("decision"))
    run_id: str
    action: AgentAction
    reason: str = Field(min_length=8, max_length=3000)
    evidence: list[str] = Field(default_factory=list)
    uncertainty: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    valid_until: datetime
    tool_call: AgentToolCall | None = None
    expected_outcome: str = Field(default="", max_length=1000)
    abort_conditions: list[str] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def action_requires_tool(self):
        terminal_without_tool = {AgentAction.WAIT}
        if self.action not in terminal_without_tool and self.tool_call is None:
            raise ValueError(f"action {self.action} requires a structured tool call")
        if self.valid_until <= self.created_at:
            raise ValueError("agent decision must expire after it is created")
        return self


class AgentObservation(BaseModel):
    id: str = Field(default_factory=lambda: _id("obs"))
    run_id: str
    kind: str = Field(min_length=2, max_length=100)
    ok: bool
    summary: str = Field(min_length=1, max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str | None = None
    event_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentState(BaseModel):
    goal_id: str
    run_id: str
    phase: str = "observing"
    market: dict[str, Any] = Field(default_factory=dict)
    portfolio: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    grant: dict[str, Any] = Field(default_factory=dict)
    open_order_ids: list[str] = Field(default_factory=list)
    position_symbols: list[str] = Field(default_factory=list)
    active_episodes: list[dict[str, Any]] = Field(default_factory=list)
    memories: list[dict[str, Any]] = Field(default_factory=list)
    recent_observation_ids: list[str] = Field(default_factory=list)
    memory_refs: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentReadinessCheck(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    passed: bool
    message: str = Field(min_length=1, max_length=500)


class AgentReadiness(BaseModel):
    ready_for_autonomous_trading: bool
    mode: AgentAutonomyMode
    checks: list[AgentReadinessCheck]
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentRun(BaseModel):
    id: str = Field(default_factory=lambda: _id("run"))
    goal_id: str
    trigger_event_id: str
    status: AgentRunStatus = AgentRunStatus.RUNNING
    step_count: int = Field(default=0, ge=0)
    max_steps: int = Field(default=8, ge=1, le=20)
    state: AgentState | None = None
    last_error: str = Field(default="", max_length=2000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


class AgentMemory(BaseModel):
    id: str = Field(default_factory=lambda: _id("mem"))
    memory_type: MemoryType
    content: str = Field(min_length=8, max_length=5000)
    tags: list[str] = Field(default_factory=list)
    source_episode_id: str | None = None
    confidence: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TradeEpisodeStatus(StrEnum):
    RESERVED = "reserved"
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    AWAITING_REVIEW = "awaiting_review"
    CLOSED = "closed"
    CANCELED = "canceled"
    UNCERTAIN = "uncertain"


class TradeEpisode(BaseModel):
    id: str = Field(default_factory=lambda: _id("episode"))
    goal_id: str
    policy_id: str | None = None
    run_id: str
    plan_id: str
    symbol: str
    side: str
    status: TradeEpisodeStatus
    entry_order_ids: list[str] = Field(default_factory=list)
    exit_order_ids: list[str] = Field(default_factory=list)
    exit_client_order_ids: list[str] = Field(default_factory=list)
    planned_risk: Decimal = Decimal("0")
    realized_pnl: Decimal | None = None
    fees: Decimal = Decimal("0")
    return_on_risk: Decimal | None = None
    summary: str = ""
    opened_at: datetime
    closed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentEpisodeRuntime(BaseModel):
    """Read-only execution truth shown beside a persisted trade episode."""

    episode_id: str
    plan_id: str
    symbol: str
    plan_status: str | None = None
    leverage: Decimal | None = None
    entry_quantity: Decimal = Decimal("0")
    average_entry: Decimal | None = None
    entry_fees: Decimal = Decimal("0")
    stop_loss: Decimal | None = None
    take_profits: list[dict[str, Any]] = Field(default_factory=list)
    native_protection_status: str
    protected_order_ids: list[str] = Field(default_factory=list)
    last_reconciled_at: datetime | None = None
