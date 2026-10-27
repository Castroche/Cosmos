from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentAutonomyMode(StrEnum):
    """Historical execution scope retained for read-only evidence parsing."""

    OBSERVE = "observe"
    RESEARCH = "research"
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE_CAPPED = "live_capped"


class TradeEpisodeStatus(StrEnum):
    RESERVED = "reserved"
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    AWAITING_REVIEW = "awaiting_review"
    CLOSED = "closed"
    CANCELED = "canceled"
    UNCERTAIN = "uncertain"


class TradeEpisode(BaseModel):
    """Read-only schema for pre-v2 trade evidence."""

    id: str = Field(default_factory=lambda: f"episode_{uuid4().hex[:16]}")
    goal_id: str
    policy_id: str | None = None
    run_id: str
    plan_id: str
    strategy_certificate_id: str | None = None
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
