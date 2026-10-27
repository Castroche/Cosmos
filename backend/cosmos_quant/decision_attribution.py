from __future__ import annotations

from decimal import Decimal
from math import sqrt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DecisionOutcomeV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str
    campaign_id: str | None = None
    attribution_role: Literal["PRIMARY", "CONTRIBUTOR"] = "PRIMARY"
    plan_ids: list[str] = Field(default_factory=list)
    episode_ids: list[str] = Field(default_factory=list)
    evidence_complete: bool = False
    trading_mode: str = "UNKNOWN"
    action: Literal["LONG", "SHORT", "WAIT", "NO_TRADE", "MANAGE_EXISTING", "EXIT"]
    setup: str = "unknown"
    regime: str = "unknown"
    direction: Literal["LONG", "SHORT", "NONE"] = "NONE"
    notional: Decimal = Decimal("0")
    gross_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    exit_price: Decimal | None = None
    return_after_cost: Decimal = Decimal("0")
    return_on_risk: Decimal = Decimal("0")
    mfe: Decimal = Decimal("0")
    mae: Decimal = Decimal("0")
    fees: Decimal = Field(default=Decimal("0"), ge=0)
    slippage: Decimal = Field(default=Decimal("0"), ge=0)
    wake_fired: bool = False
    wake_actionable: bool = False
    entered_after_wait: bool = False
    plan_expired: bool = False
    no_trade_favorable_2atr_before_adverse_1atr: bool | None = None
    quant_probability: Decimal | None = Field(default=None, ge=0, le=1)
    llm_probability: Decimal | None = Field(default=None, ge=0, le=1)
    target_hit: bool = False
    stopped: bool = False
    thesis_remained_valid_after_stop: bool = False
    target_reached_after_stop: bool = False


class DecisionAttributionReport(BaseModel):
    sample_count: int
    trade_count: int
    expectancy_r: Decimal
    profit_factor: Decimal | None
    sharpe: Decimal | None
    sortino: Decimal | None
    maximum_drawdown_r: Decimal
    average_mfe: Decimal
    average_mae: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    wake_frequency: Decimal
    wake_precision: Decimal | None
    wait_entry_rate: Decimal | None
    plan_expiry_rate: Decimal | None
    no_trade_missed_opportunity_rate: Decimal | None
    post_stop_thesis_target_rate: Decimal | None
    by_setup_regime_direction: dict[str, dict[str, Decimal | int]]
    quant_brier: Decimal | None
    llm_brier: Decimal | None


def _average(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")


def _sample_std(values: list[Decimal], *, downside_only: bool = False) -> Decimal | None:
    selected = [item for item in values if item < 0] if downside_only else values
    if len(selected) < 2:
        return None
    mean = _average(selected)
    variance = sum(((item - mean) ** 2 for item in selected), Decimal("0")) / Decimal(
        len(selected) - 1
    )
    return Decimal(str(sqrt(float(variance))))


def _brier(records: list[DecisionOutcomeV2], field: str) -> Decimal | None:
    pairs = [
        (getattr(item, field), Decimal("1") if item.target_hit else Decimal("0"))
        for item in records
        if getattr(item, field) is not None
    ]
    return (
        _average([(probability - outcome) ** 2 for probability, outcome in pairs])
        if pairs
        else None
    )


def build_decision_attribution(
    records: list[DecisionOutcomeV2],
) -> DecisionAttributionReport:
    # An add is a decision episode, but its pooled campaign is only one trade.
    campaigns: dict[str, DecisionOutcomeV2] = {}
    for item in records:
        if item.action not in {"LONG", "SHORT"} or item.attribution_role == "CONTRIBUTOR":
            continue
        campaigns.setdefault(item.campaign_id or item.episode_id, item)
    trades = list(campaigns.values())
    returns = [item.return_on_risk for item in trades]
    gains = sum((item for item in returns if item > 0), Decimal("0"))
    losses = abs(sum((item for item in returns if item < 0), Decimal("0")))
    profit_factor = gains / losses if losses > 0 else None
    mean = _average(returns)
    standard = _sample_std(returns)
    downside = _sample_std(returns, downside_only=True)
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for value in returns:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    wakes = [item for item in records if item.wake_fired]
    waits = [item for item in records if item.action == "WAIT"]
    no_trades = [
        item
        for item in records
        if item.action == "NO_TRADE"
        and item.no_trade_favorable_2atr_before_adverse_1atr is not None
    ]
    stopped_valid = [
        item for item in trades if item.stopped and item.thesis_remained_valid_after_stop
    ]
    grouped: dict[str, list[DecisionOutcomeV2]] = {}
    for item in trades:
        key = f"{item.setup}|{item.regime}|{item.direction}"
        grouped.setdefault(key, []).append(item)
    return DecisionAttributionReport(
        sample_count=len(records),
        trade_count=len(trades),
        expectancy_r=mean,
        profit_factor=profit_factor,
        sharpe=mean / standard if standard and standard > 0 else None,
        sortino=mean / downside if downside and downside > 0 else None,
        maximum_drawdown_r=max_drawdown,
        average_mfe=_average([item.mfe for item in trades]),
        average_mae=_average([item.mae for item in trades]),
        total_fees=sum((item.fees for item in trades), Decimal("0")),
        total_slippage=sum((item.slippage for item in trades), Decimal("0")),
        wake_frequency=Decimal(len(wakes)) / Decimal(len(records)) if records else Decimal("0"),
        wake_precision=(
            Decimal(sum(item.wake_actionable for item in wakes)) / Decimal(len(wakes))
            if wakes
            else None
        ),
        wait_entry_rate=(
            Decimal(sum(item.entered_after_wait for item in waits)) / Decimal(len(waits))
            if waits
            else None
        ),
        plan_expiry_rate=(
            Decimal(sum(item.plan_expired for item in waits)) / Decimal(len(waits))
            if waits
            else None
        ),
        no_trade_missed_opportunity_rate=(
            Decimal(
                sum(bool(item.no_trade_favorable_2atr_before_adverse_1atr) for item in no_trades)
            )
            / Decimal(len(no_trades))
            if no_trades
            else None
        ),
        post_stop_thesis_target_rate=(
            Decimal(sum(item.target_reached_after_stop for item in stopped_valid))
            / Decimal(len(stopped_valid))
            if stopped_valid
            else None
        ),
        by_setup_regime_direction={
            key: {
                "count": len(items),
                "expectancy_r": _average([item.return_on_risk for item in items]),
                "win_rate": Decimal(sum(item.return_on_risk > 0 for item in items))
                / Decimal(len(items)),
            }
            for key, items in grouped.items()
        },
        quant_brier=_brier(trades, "quant_probability"),
        llm_brier=_brier(trades, "llm_probability"),
    )
