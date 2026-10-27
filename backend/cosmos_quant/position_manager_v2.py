from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .trading_intelligence import PositionRuntimeV2, PositionStateV2, TradePlan


class PositionActionType(StrEnum):
    HOLD = "HOLD"
    MOVE_STOP = "MOVE_STOP"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


class PositionActionV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: PositionActionType
    reason: str
    quantity: Decimal | None = Field(default=None, gt=0)
    new_stop: Decimal | None = Field(default=None, gt=0)
    target_index: int | None = Field(default=None, ge=0)


class DeterministicPositionManager:
    """Pure position management. It never invokes an LLM."""

    def evaluate(
        self,
        *,
        plan: TradePlan,
        position: PositionRuntimeV2,
        mark_price: Decimal,
        atr_value: Decimal | None,
        now: datetime | None = None,
    ) -> PositionActionV2:
        current = now or datetime.now(UTC)
        if position.plan_id != plan.plan_id:
            raise ValueError("position and plan do not match")
        if position.state not in {
            PositionStateV2.PARTIALLY_FILLED,
            PositionStateV2.OPEN,
            PositionStateV2.PARTIAL_EXIT,
        }:
            return PositionActionV2(action=PositionActionType.HOLD, reason="position not open")
        stop_hit = (
            mark_price <= position.current_stop
            if position.side == "LONG"
            else mark_price >= position.current_stop
        )
        if stop_hit:
            return PositionActionV2(
                action=PositionActionType.CLOSE,
                quantity=position.remaining_quantity,
                reason="hard stop reached",
            )
        if plan.management.maximum_holding_minutes is not None and current >= (
            (position.management_started_at or position.opened_at)
            + timedelta(minutes=plan.management.maximum_holding_minutes)
        ):
            return PositionActionV2(
                action=PositionActionType.CLOSE,
                quantity=position.remaining_quantity,
                reason="maximum holding time reached",
            )
        if position.targets_reached < len(plan.management.take_profits):
            target = plan.management.take_profits[position.targets_reached]
            target_hit = (
                mark_price >= target.price
                if position.side == "LONG"
                else mark_price <= target.price
            )
            if target_hit:
                quantity = min(
                    (position.management_quantity or position.original_quantity)
                    * target.close_fraction,
                    position.remaining_quantity,
                )
                return PositionActionV2(
                    action=(
                        PositionActionType.CLOSE
                        if quantity >= position.remaining_quantity
                        else PositionActionType.REDUCE
                    ),
                    quantity=quantity,
                    target_index=position.targets_reached,
                    reason=f"take-profit target {position.targets_reached + 1} reached",
                )
        break_even_target = plan.management.break_even_after_target
        if (
            break_even_target is not None
            and position.targets_reached >= break_even_target
            and (
                (position.side == "LONG" and position.current_stop < position.average_entry)
                or (position.side == "SHORT" and position.current_stop > position.average_entry)
            )
        ):
            return PositionActionV2(
                action=PositionActionType.MOVE_STOP,
                new_stop=position.average_entry,
                reason="break-even rule activated",
            )
        trailing = plan.management.atr_trailing_multiple
        if trailing is not None and atr_value is not None and position.targets_reached >= 1:
            candidate = (
                mark_price - trailing * atr_value
                if position.side == "LONG"
                else mark_price + trailing * atr_value
            )
            improves = (
                candidate > position.current_stop
                if position.side == "LONG"
                else candidate < position.current_stop
            )
            if improves:
                return PositionActionV2(
                    action=PositionActionType.MOVE_STOP,
                    new_stop=candidate,
                    reason="ATR trailing stop tightened",
                )
        return PositionActionV2(action=PositionActionType.HOLD, reason="no rule triggered")

    def apply(
        self,
        position: PositionRuntimeV2,
        action: PositionActionV2,
        *,
        now: datetime | None = None,
    ) -> PositionRuntimeV2:
        current = now or datetime.now(UTC)
        updates: dict = {"version": position.version + 1, "updated_at": current}
        if action.action == PositionActionType.MOVE_STOP:
            updates["current_stop"] = action.new_stop
        elif action.action in {PositionActionType.REDUCE, PositionActionType.CLOSE}:
            quantity = action.quantity or position.remaining_quantity
            remaining = max(position.remaining_quantity - quantity, Decimal("0"))
            updates["remaining_quantity"] = remaining
            if action.target_index is not None:
                updates["targets_reached"] = max(
                    position.targets_reached,
                    action.target_index + 1,
                )
            updates["state"] = (
                PositionStateV2.CLOSED if remaining == 0 else PositionStateV2.PARTIAL_EXIT
            )
        return position.model_copy(update=updates)
