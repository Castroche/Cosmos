from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from .trading_intelligence import DecisionEpisode, PositionRuntimeV2, RiskAllocation


class TradeHistoryItemV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_id: str
    episode_id: str
    plan_id: str
    plan_ids: list[str] = Field(default_factory=list)
    episode_ids: list[str] = Field(default_factory=list)
    symbol: str
    side: str
    opened_at: datetime
    closed_at: datetime
    duration_minutes: int
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal | None = None
    stop_price: Decimal
    realized_pnl: Decimal
    gross_pnl: Decimal
    fees: Decimal
    return_after_cost: Decimal
    return_on_risk: Decimal
    mfe: Decimal
    mae: Decimal
    targets_reached: int
    exit_reason: str | None = None
    entry_order_ids: list[str]
    exit_order_ids: list[str]


class TradeEquityPointV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    closed_at: datetime
    cumulative_pnl: Decimal
    position_id: str


class TradeHistorySummaryV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trade_count: int
    winning_trades: int
    losing_trades: int
    win_rate: Decimal | None
    net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_pnl: Decimal
    total_fees: Decimal
    maximum_drawdown: Decimal
    average_return_on_risk: Decimal


class TradeHistoryResponseV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    symbol: str | None
    days: int
    summary: TradeHistorySummaryV2
    equity_curve: list[TradeEquityPointV2]
    items: list[TradeHistoryItemV2]


def build_trade_history_v2(
    positions: list[PositionRuntimeV2],
    episodes: dict[str, DecisionEpisode],
    allocations: dict[str, RiskAllocation],
    *,
    symbol: str | None = None,
    days: int = 30,
    limit: int = 500,
    now: datetime | None = None,
) -> TradeHistoryResponseV2:
    observed_at = now or datetime.now(UTC)
    cutoff = observed_at - timedelta(days=days) if days > 0 else None
    items: list[TradeHistoryItemV2] = []
    seen_positions: set[str] = set()
    for position in positions:
        if position.position_id in seen_positions:
            continue
        if str(position.state) != "CLOSED" or position.closed_at is None:
            continue
        if symbol and position.symbol != symbol:
            continue
        if cutoff and position.closed_at < cutoff:
            continue
        episode_ids = list(
            dict.fromkeys(
                [*position.episode_ids, *([position.episode_id] if position.episode_id else [])]
            )
        )
        plan_ids = list(dict.fromkeys([*position.plan_ids, position.plan_id]))
        episode = episodes.get(position.episode_id or "")
        if episode is None or not episode.outcome:
            episode = next(
                (
                    episodes[item]
                    for item in episode_ids
                    if item in episodes and episodes[item].outcome
                ),
                episode,
            )
        if episode is None:
            continue
        seen_positions.add(position.position_id)
        outcome = episode.outcome or {}
        allocation = allocations.get(episode.risk_allocation_id or "")
        notional = Decimal(str(outcome.get("notional") or "0"))
        if notional <= 0 and allocation is not None:
            notional = allocation.notional
        return_after_cost = Decimal(str(outcome.get("return_after_cost") or "0"))
        fees = Decimal(str(outcome.get("fees") or position.accumulated_fees or "0"))
        realized_value = outcome.get("realized_pnl")
        realized_pnl = (
            Decimal(str(realized_value))
            if realized_value is not None
            else return_after_cost * notional
        )
        gross_value = outcome.get("gross_pnl")
        gross_pnl = Decimal(str(gross_value)) if gross_value is not None else realized_pnl + fees
        exit_value = outcome.get("exit_price")
        exit_price = Decimal(str(exit_value)) if exit_value is not None else None
        duration = max(
            int((position.closed_at - position.opened_at).total_seconds() // 60),
            0,
        )
        items.append(
            TradeHistoryItemV2(
                position_id=position.position_id,
                episode_id=episode.episode_id,
                plan_id=position.plan_id,
                plan_ids=plan_ids,
                episode_ids=episode_ids,
                symbol=position.symbol,
                side=position.side,
                opened_at=position.opened_at,
                closed_at=position.closed_at,
                duration_minutes=duration,
                quantity=position.original_quantity,
                entry_price=position.average_entry,
                exit_price=exit_price,
                stop_price=position.initial_stop,
                realized_pnl=realized_pnl,
                gross_pnl=gross_pnl,
                fees=fees,
                return_after_cost=return_after_cost,
                return_on_risk=Decimal(str(outcome.get("return_on_risk") or "0")),
                mfe=Decimal(str(outcome.get("mfe") or "0")),
                mae=Decimal(str(outcome.get("mae") or "0")),
                targets_reached=position.targets_reached,
                exit_reason=position.exit_reason,
                entry_order_ids=position.entry_order_ids,
                exit_order_ids=position.exit_order_ids,
            )
        )

    items.sort(key=lambda item: item.closed_at, reverse=True)
    items = items[:limit]
    chronological = list(reversed(items))
    cumulative = Decimal("0")
    peak = Decimal("0")
    maximum_drawdown = Decimal("0")
    curve: list[TradeEquityPointV2] = []
    for item in chronological:
        cumulative += item.realized_pnl
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
        curve.append(
            TradeEquityPointV2(
                closed_at=item.closed_at,
                cumulative_pnl=cumulative,
                position_id=item.position_id,
            )
        )

    wins = [item.realized_pnl for item in items if item.realized_pnl > 0]
    losses = [item.realized_pnl for item in items if item.realized_pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))
    count = len(items)
    return TradeHistoryResponseV2(
        generated_at=observed_at,
        symbol=symbol,
        days=days,
        summary=TradeHistorySummaryV2(
            trade_count=count,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=(Decimal(len(wins)) / Decimal(count) if count else None),
            net_pnl=sum((item.realized_pnl for item in items), Decimal("0")),
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=(gross_profit / gross_loss if gross_loss > 0 else None),
            average_pnl=(
                sum((item.realized_pnl for item in items), Decimal("0")) / Decimal(count)
                if count
                else Decimal("0")
            ),
            total_fees=sum((item.fees for item in items), Decimal("0")),
            maximum_drawdown=maximum_drawdown,
            average_return_on_risk=(
                sum((item.return_on_risk for item in items), Decimal("0")) / Decimal(count)
                if count
                else Decimal("0")
            ),
        ),
        equity_curve=curve,
        items=items,
    )
