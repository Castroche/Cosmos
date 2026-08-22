from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from ..domain import Candle, FundingRatePoint, Side, StrategyPlan
from ..indicators import atr
from ..timeframes import candle_close_time, timeframe_seconds


class ShadowOutcomeStatus(StrEnum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    NO_ENTRY = "no_entry"
    CLOSED = "closed"
    HORIZON_MARK = "horizon_mark"


class ShadowPlanOutcome(BaseModel):
    """Causal, non-executable forward outcome for one frozen trade plan."""

    status: ShadowOutcomeStatus
    plan_id: str
    symbol: str
    timeframe: str
    side: Side
    decision_at: datetime
    evaluated_through: datetime | None = None
    entered_at: datetime | None = None
    exited_at: datetime | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    original_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    current_stop: Decimal | None = None
    net_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    funding_cost: Decimal = Decimal("0")
    return_on_risk: Decimal | None = None
    exit_reasons: list[str] = Field(default_factory=list)
    bars_evaluated: int = 0
    causal: bool = True
    executable: bool = False


class ShadowValidationTrial(BaseModel):
    """Immutable candidate decision plus its evolving forward-only outcome."""

    id: str = Field(default_factory=lambda: f"shadow_{uuid4().hex[:16]}")
    goal_id: str
    run_id: str
    decision_batch_id: str
    plan: StrategyPlan
    decision_at: datetime
    horizon_at: datetime
    selected_for_execution: bool = False
    execution_eligible: bool = False
    screening_rank: int
    screening_summary: str
    selection_score: dict[str, Any] = Field(default_factory=dict)
    adaptation_evidence: dict[str, Any] = Field(default_factory=dict)
    signal_evidence: dict[str, Any] = Field(default_factory=dict)
    backtest_evidence: dict[str, Any] = Field(default_factory=dict)
    outcome: ShadowPlanOutcome
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ShadowCalibrationSummary(BaseModel):
    trial_count: int
    decision_batches: int
    settled_outcomes: int
    settled_batches: int
    pending_entry: int
    open_positions: int
    no_entry: int
    wins: int
    losses_or_flat: int
    total_r: Decimal
    expectancy_r: Decimal
    rejected_settled: int
    rejected_positive: int
    rejected_nonpositive: int
    selected_settled: int
    selected_positive: int
    selected_nonpositive: int
    calibration_ready: bool
    minimum_settled_batches: int = 30
    causal: bool = True
    executable: bool = False
    independent_performance_evidence: bool = False


def summarize_shadow_trials(
    trials: list[ShadowValidationTrial],
    *,
    minimum_settled_batches: int = 30,
) -> ShadowCalibrationSummary:
    settled = [
        trial
        for trial in trials
        if trial.outcome.return_on_risk is not None
        and trial.outcome.status
        in {
            ShadowOutcomeStatus.CLOSED,
            ShadowOutcomeStatus.HORIZON_MARK,
        }
    ]
    returns = [
        trial.outcome.return_on_risk
        for trial in settled
        if trial.outcome.return_on_risk is not None
    ]
    rejected = [trial for trial in settled if not trial.selected_for_execution]
    selected = [trial for trial in settled if trial.selected_for_execution]
    settled_batches = {trial.decision_batch_id for trial in settled}
    total_r = sum(returns, Decimal("0"))
    expectancy = total_r / Decimal(len(returns)) if returns else Decimal("0")
    return ShadowCalibrationSummary(
        trial_count=len(trials),
        decision_batches=len({trial.decision_batch_id for trial in trials}),
        settled_outcomes=len(settled),
        settled_batches=len(settled_batches),
        pending_entry=sum(
            trial.outcome.status == ShadowOutcomeStatus.PENDING_ENTRY for trial in trials
        ),
        open_positions=sum(trial.outcome.status == ShadowOutcomeStatus.OPEN for trial in trials),
        no_entry=sum(trial.outcome.status == ShadowOutcomeStatus.NO_ENTRY for trial in trials),
        wins=sum(value > 0 for value in returns),
        losses_or_flat=sum(value <= 0 for value in returns),
        total_r=total_r.quantize(Decimal("0.001")),
        expectancy_r=expectancy.quantize(Decimal("0.001")),
        rejected_settled=len(rejected),
        rejected_positive=sum(trial.outcome.return_on_risk > 0 for trial in rejected),
        rejected_nonpositive=sum(trial.outcome.return_on_risk <= 0 for trial in rejected),
        selected_settled=len(selected),
        selected_positive=sum(trial.outcome.return_on_risk > 0 for trial in selected),
        selected_nonpositive=sum(trial.outcome.return_on_risk <= 0 for trial in selected),
        calibration_ready=len(settled_batches) >= minimum_settled_batches,
        minimum_settled_batches=minimum_settled_batches,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _next_full_bar_start(decision_at: datetime, timeframe: str) -> datetime:
    """Exclude the bar already forming when the decision was made."""

    aware = _aware(decision_at)
    seconds = timeframe_seconds(timeframe)
    next_epoch = (int(aware.timestamp()) // seconds + 1) * seconds
    return datetime.fromtimestamp(next_epoch, tz=UTC)


def _funding_cost(
    funding_rates: list[FundingRatePoint],
    *,
    entry_time: datetime,
    exit_time: datetime,
    side: Side,
    quantity: Decimal,
    fallback_price: Decimal,
) -> Decimal:
    direction = Decimal("1") if side == Side.LONG else Decimal("-1")
    return sum(
        (
            quantity * (point.mark_price or fallback_price) * point.rate * direction
            for point in funding_rates
            if entry_time < _aware(point.timestamp) <= exit_time
        ),
        Decimal("0"),
    )


def evaluate_shadow_plan(
    plan: StrategyPlan,
    candles: list[Candle],
    *,
    decision_at: datetime,
    as_of: datetime,
    fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
    funding_rates: list[FundingRatePoint] | None = None,
    force_close: bool = False,
) -> ShadowPlanOutcome:
    """Evaluate one frozen plan on future, fully closed bars only.

    The candle already forming at ``decision_at`` is excluded because its OHLC
    includes trades that happened before the decision. Entry is allowed only
    on complete later candles that finish before the plan's expiry. Stops win
    same-candle ambiguity, matching the production backtest convention.
    """

    decision_at = _aware(decision_at)
    as_of = _aware(as_of)
    entry_expires_at = _aware(plan.entry.expires_at) if plan.entry.expires_at is not None else None
    first_bar_start = _next_full_bar_start(decision_at, plan.timeframe)
    eligible = sorted(
        (
            candle
            for candle in candles
            if _aware(candle.timestamp) >= first_bar_start
            and candle_close_time(candle, plan.timeframe) <= as_of
        ),
        key=lambda candle: candle.timestamp,
    )
    funding_rates = funding_rates or []
    slip = slippage_bps / Decimal("10000")
    fee_rate = fee_bps / Decimal("10000")
    entry_mid = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
    original_quantity = plan.sizing.suggested_quantity

    position: dict | None = None
    net_pnl = Decimal("0")
    fees = Decimal("0")
    slippage_cost = Decimal("0")
    funding_cost = Decimal("0")
    exit_reasons: list[str] = []
    last_exit_price: Decimal | None = None
    exited_at: datetime | None = None

    def close_quantity(
        trigger_price: Decimal,
        quantity: Decimal,
        reason: str,
        timestamp: datetime,
    ) -> None:
        nonlocal net_pnl, fees, slippage_cost, funding_cost
        nonlocal last_exit_price, exited_at
        assert position is not None
        exit_price = trigger_price * (
            Decimal("1") - slip if plan.side == Side.LONG else Decimal("1") + slip
        )
        gross = (exit_price - position["entry"]) * quantity * direction
        allocated_fees = (position["entry"] + exit_price) * quantity * fee_rate
        allocated_slippage = (
            abs(position["entry"] - entry_mid) + abs(exit_price - trigger_price)
        ) * quantity
        allocated_funding = _funding_cost(
            funding_rates,
            entry_time=position["time"],
            exit_time=timestamp,
            side=plan.side,
            quantity=quantity,
            fallback_price=position["entry"],
        )
        net_pnl += gross - allocated_fees - allocated_funding
        fees += allocated_fees
        slippage_cost += allocated_slippage
        funding_cost += allocated_funding
        position["remaining"] -= quantity
        last_exit_price = exit_price
        exited_at = timestamp
        exit_reasons.append(reason)

    for index, candle in enumerate(eligible):
        candle_time = candle_close_time(candle, plan.timeframe)
        if position is None:
            if (
                entry_expires_at is not None
                and candle_close_time(candle, plan.timeframe) > entry_expires_at
            ):
                break
            if candle.low <= entry_mid <= candle.high:
                fill = entry_mid * (
                    Decimal("1") + slip if plan.side == Side.LONG else Decimal("1") - slip
                )
                position = {
                    "entry": fill,
                    "time": candle_time,
                    "remaining": original_quantity,
                    "target_index": 0,
                    "current_stop": plan.exit.stop_loss,
                    "favorable_price": fill,
                    "bars_held": 0,
                }
            # Never enter and exit from the same OHLC bar: without ticks its
            # intrabar sequence relative to the entry is unknowable.
            continue

        position["bars_held"] += 1
        stop_hit = (
            candle.low <= position["current_stop"]
            if plan.side == Side.LONG
            else candle.high >= position["current_stop"]
        )
        if stop_hit:
            stop_reason = (
                "trailing_stop" if position["current_stop"] != plan.exit.stop_loss else "stop_loss"
            )
            close_quantity(
                position["current_stop"],
                position["remaining"],
                stop_reason,
                candle_time,
            )
        else:
            while position["target_index"] < len(plan.exit.take_profits):
                target = plan.exit.take_profits[position["target_index"]]
                target_hit = (
                    candle.high >= target.price
                    if plan.side == Side.LONG
                    else candle.low <= target.price
                )
                if not target_hit:
                    break
                final_target = position["target_index"] == len(plan.exit.take_profits) - 1
                quantity = (
                    position["remaining"]
                    if final_target
                    else min(
                        original_quantity * target.close_fraction,
                        position["remaining"],
                    )
                )
                close_quantity(
                    target.price,
                    quantity,
                    "take_profit",
                    candle_time,
                )
                position["target_index"] += 1

        if (
            position["remaining"] > 0
            and plan.exit.time_exit_is_due(held_bars=position["bars_held"])
        ):
            close_quantity(
                candle.close,
                position["remaining"],
                "time_stop",
                candle_time,
            )

        if position["remaining"] <= 0:
            break
        if plan.exit.trailing_stop_atr is not None and index >= 14:
            if plan.side == Side.LONG:
                position["favorable_price"] = max(
                    position["favorable_price"],
                    candle.high,
                )
                favorable_move = position["favorable_price"] - position["entry"]
            else:
                position["favorable_price"] = min(
                    position["favorable_price"],
                    candle.low,
                )
                favorable_move = position["entry"] - position["favorable_price"]
            initial_risk = abs(position["entry"] - plan.exit.stop_loss)
            if plan.exit.trailing_is_active(
                targets_reached=position["target_index"],
                favorable_move=max(favorable_move, Decimal("0")),
                initial_risk=initial_risk,
            ):
                trail_distance = (
                    atr(eligible[max(0, index - 14) : index + 1], 14) * plan.exit.trailing_stop_atr
                )
                if plan.side == Side.LONG:
                    position["current_stop"] = max(
                        position["current_stop"],
                        position["favorable_price"] - trail_distance,
                    )
                else:
                    position["current_stop"] = min(
                        position["current_stop"],
                        position["favorable_price"] + trail_distance,
                    )

    evaluated_through = candle_close_time(eligible[-1], plan.timeframe) if eligible else None
    if position is None and not exit_reasons:
        expired = bool(force_close or (entry_expires_at and as_of >= entry_expires_at))
        return ShadowPlanOutcome(
            status=(ShadowOutcomeStatus.NO_ENTRY if expired else ShadowOutcomeStatus.PENDING_ENTRY),
            plan_id=plan.id,
            symbol=plan.symbol,
            timeframe=plan.timeframe,
            side=plan.side,
            decision_at=decision_at,
            evaluated_through=evaluated_through,
            original_quantity=original_quantity,
            bars_evaluated=len(eligible),
        )

    assert position is not None
    if position["remaining"] > 0 and force_close and eligible:
        final_candle = eligible[-1]
        close_quantity(
            final_candle.close,
            position["remaining"],
            "horizon_mark",
            _aware(final_candle.timestamp),
        )

    remaining = max(position["remaining"], Decimal("0"))
    closed = remaining <= 0
    unrealized = Decimal("0")
    if not closed and eligible:
        mark = eligible[-1].close
        unrealized = (mark - position["entry"]) * remaining * direction - (
            position["entry"] + mark
        ) * remaining * fee_rate
    status = (
        ShadowOutcomeStatus.HORIZON_MARK
        if closed and exit_reasons[-1:] == ["horizon_mark"]
        else ShadowOutcomeStatus.CLOSED
        if closed
        else ShadowOutcomeStatus.OPEN
    )
    final_pnl = net_pnl
    return_on_risk = (
        final_pnl / plan.sizing.risk_amount if closed and plan.sizing.risk_amount > 0 else None
    )
    return ShadowPlanOutcome(
        status=status,
        plan_id=plan.id,
        symbol=plan.symbol,
        timeframe=plan.timeframe,
        side=plan.side,
        decision_at=decision_at,
        evaluated_through=evaluated_through,
        entered_at=position["time"],
        exited_at=exited_at if closed else None,
        entry_price=position["entry"],
        exit_price=last_exit_price if closed else None,
        original_quantity=original_quantity,
        remaining_quantity=remaining,
        current_stop=position["current_stop"] if not closed else None,
        net_pnl=final_pnl.quantize(Decimal("0.01")),
        unrealized_pnl=unrealized.quantize(Decimal("0.01")),
        fees=fees.quantize(Decimal("0.01")),
        slippage_cost=slippage_cost.quantize(Decimal("0.01")),
        funding_cost=funding_cost.quantize(Decimal("0.01")),
        return_on_risk=(
            return_on_risk.quantize(Decimal("0.001")) if return_on_risk is not None else None
        ),
        exit_reasons=exit_reasons,
        bars_evaluated=len(eligible),
    )
