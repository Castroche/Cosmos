from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

from .domain import StrategyPlan, StrategySignal


class EntrySignalState(StrEnum):
    ARMED = "armed"
    DEGRADED = "degraded"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"


class EntrySignalAssessment(BaseModel):
    state: EntrySignalState
    execution_allowed: bool
    blocking: bool
    reason: str


def assess_entry_signal(
    plan: StrategyPlan,
    signal: StrategySignal,
    *,
    now: datetime | None = None,
) -> EntrySignalAssessment:
    """Classify latest evidence without treating every flat bar as a failure."""

    now = now or datetime.now(UTC)
    expires_at = plan.entry.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            return EntrySignalAssessment(
                state=EntrySignalState.EXPIRED,
                execution_allowed=False,
                blocking=True,
                reason="计划入场窗口已过期，必须重新生成并验证。",
            )

    if signal.actionable:
        if str(signal.direction) == str(plan.side):
            return EntrySignalAssessment(
                state=EntrySignalState.ARMED,
                execution_allowed=True,
                blocking=False,
                reason=f"最新已收盘信号继续支持计划方向：{plan.side}。",
            )
        return EntrySignalAssessment(
            state=EntrySignalState.INVALIDATED,
            execution_allowed=False,
            blocking=True,
            reason=(f"最新已收盘信号为 {signal.direction}，与计划 {plan.side} 形成明确反向证据。"),
        )

    return EntrySignalAssessment(
        state=EntrySignalState.DEGRADED,
        execution_allowed=True,
        blocking=False,
        reason=(
            "最新信号暂时转为 flat，但没有形成反向证据；"
            "原计划在入场有效期、价格区间与全部硬风险门禁内仍可执行。"
        ),
    )
