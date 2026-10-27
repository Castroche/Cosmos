from __future__ import annotations

import json
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from decimal import ROUND_UP, Decimal
from hashlib import sha256
from math import sqrt

from pydantic import BaseModel, Field, model_validator

from .configuration import ResearchValidationPreferences, StrategyKind
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    MarketSnapshot,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
    Side,
    StrategyPlan,
)
from .indicators import atr
from .signals import NoTradeSignal, score_market_state, strategy_family_fit
from .statistical_validation import (
    StatisticalValidationReport,
    validate_statistical_evidence,
)
from .strategy_factory import StrategyFactory
from .strategy_spec import (
    StrategySpec,
    minimum_signal_bars,
    validate_strategy_spec,
)
from .timeframes import (
    candle_close_time,
    confirmation_timeframe,
    periods_per_year,
    resample_closed_candles,
    timeframe_seconds,
)

CAUSAL_LIBRARY_POLICY_VERSION = 2


class CausalStrategyPrior(BaseModel):
    """One pre-registered strategy available to a causal Agent policy.

    The metadata is intentionally bound to the executable StrategySpec.  A
    backtest may rank these priors using only the current closed-bar regime and
    currently available causal features; realised or future returns are never a
    selector input.
    """

    id: str = Field(min_length=3, max_length=160)
    family: StrategyKind
    archetype: str = Field(min_length=1, max_length=160)
    preferred_regimes: list[str] = Field(default_factory=list, max_length=24)
    avoid_regimes: list[str] = Field(default_factory=list, max_length=24)
    required_data: list[str] = Field(default_factory=lambda: ["ohlcv"], min_length=1)
    spec: dict
    spec_fingerprint: str = Field(min_length=16, max_length=64)

    @model_validator(mode="after")
    def validate_executable_binding(self):
        validated = validate_strategy_spec(self.spec, self.family)
        if validated.fingerprint != self.spec_fingerprint:
            raise ValueError("causal strategy prior fingerprint does not match its StrategySpec")
        if "ohlcv" not in self.required_data:
            raise ValueError("every causal strategy prior must require OHLCV")
        if len(self.required_data) != len(set(self.required_data)):
            raise ValueError("causal strategy prior required_data values must be unique")
        return self

    @property
    def validated_spec(self) -> StrategySpec:
        return validate_strategy_spec(self.spec, self.family)


def causal_library_policy_fingerprint(
    priors: list[CausalStrategyPrior],
    *,
    maximum_candidates_per_bar: int,
    abstain_on_ambiguous_regime: bool,
    minimum_regime_confidence: Decimal = Decimal("0"),
    minimum_signal_confidence: Decimal = Decimal("0"),
    minimum_directional_consensus: int = 1,
) -> str:
    payload = {
        "version": CAUSAL_LIBRARY_POLICY_VERSION,
        "selection_inputs": "closed_bar_regime_and_latest_known_causal_features_only",
        "maximum_candidates_per_bar": maximum_candidates_per_bar,
        "abstain_on_ambiguous_regime": abstain_on_ambiguous_regime,
        "minimum_regime_confidence": str(minimum_regime_confidence),
        "minimum_signal_confidence": str(minimum_signal_confidence),
        "minimum_directional_consensus": minimum_directional_consensus,
        "priors": [
            prior.model_dump(mode="json") for prior in sorted(priors, key=lambda item: item.id)
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:24]


def rank_causal_strategy_priors(
    priors: list[CausalStrategyPrior],
    assessment,
    available_data: set[str],
) -> list[tuple[CausalStrategyPrior, Decimal, list[str]]]:
    """Rank frozen priors without using any return or trade outcome."""

    ranked: list[tuple[CausalStrategyPrior, Decimal, list[str]]] = []
    preferred_archetypes = set(assessment.preferred_archetypes)
    avoided_archetypes = set(assessment.avoided_archetypes)
    primary_tokens = {token for token in assessment.primary_regime.split("_") if token}
    for prior in priors:
        missing = sorted(set(prior.required_data) - available_data)
        if missing:
            continue
        supported, family_score, relative_score, family_reason = strategy_family_fit(
            assessment,
            prior.family,
        )
        if not supported:
            continue
        score = family_score * Decimal("100")
        reasons = [family_reason]
        if prior.family == assessment.selected_kind:
            score += Decimal("20")
        if prior.archetype in preferred_archetypes:
            score += Decimal("20")
            reasons.append("strategy archetype is preferred by the current closed-bar regime")
        if prior.archetype in avoided_archetypes:
            score -= Decimal("40")
            reasons.append("strategy archetype is avoided by the current closed-bar regime")
        preferred_text = " ".join(prior.preferred_regimes)
        avoided_text = " ".join(prior.avoid_regimes)
        for token in primary_tokens:
            if token in preferred_text:
                score += Decimal("3")
            if token in avoided_text:
                score -= Decimal("6")
        if assessment.volatility_regime in preferred_text:
            score += Decimal("3")
        if assessment.volatility_regime in avoided_text:
            score -= Decimal("6")
        reasons.append(f"causal family score={family_score:f}, relative score={relative_score:.3f}")
        ranked.append((prior, score, reasons))
    return sorted(ranked, key=lambda item: (-item[1], item[0].id))


def latest_causal_funding_point(
    funding_rates: list[FundingRatePoint],
    funding_timestamps: list[datetime],
    timestamp: datetime,
    *,
    maximum_age: timedelta = timedelta(hours=24),
) -> FundingRatePoint | None:
    index = bisect_right(funding_timestamps, timestamp) - 1
    if index < 0:
        return None
    point = funding_rates[index]
    if timestamp - point.timestamp > maximum_age:
        return None
    return point


def point_available_time(point, *, fallback: datetime) -> datetime:
    """Return the persisted point-in-time availability used by every causal join."""

    return getattr(point, "available_time", None) or fallback


def causal_open_interest_context(
    points: list[OpenInterestPoint],
    timestamps: list[datetime],
    timestamp: datetime,
    *,
    lookback: timedelta = timedelta(hours=24),
    maximum_age: timedelta = timedelta(hours=24),
) -> tuple[OpenInterestPoint | None, Decimal | None]:
    latest_index = bisect_right(timestamps, timestamp) - 1
    if latest_index < 0:
        return None, None
    latest = points[latest_index]
    if timestamp - latest.timestamp > maximum_age:
        return None, None
    baseline_cutoff = timestamp - lookback
    baseline_index = bisect_right(timestamps, baseline_cutoff) - 1
    if baseline_index < 0:
        return latest, None
    baseline = points[baseline_index]
    if baseline_cutoff - baseline.timestamp > maximum_age:
        return latest, None
    if latest.open_interest_amount is not None and baseline.open_interest_amount is not None:
        latest_value = latest.open_interest_amount
        baseline_value = baseline.open_interest_amount
    elif latest.open_interest_value is not None and baseline.open_interest_value is not None:
        latest_value = latest.open_interest_value
        baseline_value = baseline.open_interest_value
    else:
        return latest, None
    if baseline_value == 0:
        return latest, None
    return latest, (latest_value - baseline_value) / abs(baseline_value)


def latest_causal_point(
    points,
    timestamps: list[datetime],
    timestamp: datetime,
    *,
    maximum_age: timedelta = timedelta(hours=24),
):
    index = bisect_right(timestamps, timestamp) - 1
    if index < 0:
        return None
    point = points[index]
    if timestamp - timestamps[index] > maximum_age:
        return None
    return point


def causal_order_flow_context(
    points: list[OrderFlowBar],
    availability_timestamps: list[datetime],
    timestamp: datetime,
    *,
    maximum_age: timedelta = timedelta(hours=24),
) -> tuple[OrderFlowBar | None, Decimal | None]:
    point = latest_causal_point(
        points,
        availability_timestamps,
        timestamp,
        maximum_age=maximum_age,
    )
    if point is None:
        return None, None
    directional_volume = point.taker_buy_base_volume + point.taker_sell_base_volume
    imbalance = (
        point.delta_base_volume / directional_volume if directional_volume > 0 else Decimal("0")
    )
    return point, imbalance


def causal_liquidation_flow_context(
    points: list[LiquidationFlowPoint],
    timestamps: list[datetime],
    timestamp: datetime,
    *,
    lookback: timedelta = timedelta(hours=24),
) -> tuple[LiquidationFlowPoint | None, Decimal | None, Decimal | None, Decimal | None]:
    end_index = bisect_right(timestamps, timestamp)
    if end_index == 0:
        return None, None, None, None
    start_index = bisect_right(timestamps, timestamp - lookback)
    window = points[start_index:end_index]
    if not window:
        return None, None, None, None
    latest = window[-1]
    if latest.timestamp != timestamp and timestamp - latest.timestamp > timedelta(hours=1):
        return None, None, None, None
    if any(
        point.source_market_type != "coin_m_inverse_perpetual"
        or point.source_instrument_id != latest.source_instrument_id
        or point.evidence_role != "cross_contract_underlying_liquidation_context"
        for point in window
    ):
        return None, None, None, None
    long_contracts = sum(
        (point.long_liquidated_contracts for point in window),
        Decimal("0"),
    )
    short_contracts = sum(
        (point.short_liquidated_contracts for point in window),
        Decimal("0"),
    )
    total = long_contracts + short_contracts
    imbalance = (short_contracts - long_contracts) / total if total > 0 else Decimal("0")
    return latest, long_contracts, short_contracts, imbalance


class BacktestTrade(BaseModel):
    entry_time: str
    exit_time: str
    side: Side
    algorithm: str = "unknown"
    signal_regime: str = "uncertain"
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    intended_quantity: Decimal | None = None
    actual_entry_quantity: Decimal | None = None
    position_limit_reason: str = "stop_risk"
    pnl: Decimal
    exit_reason: str
    gross_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    funding_cost: Decimal = Decimal("0")
    initial_risk_amount: Decimal = Field(default=Decimal("0"), ge=0)


class BacktestTradeEpisode(BaseModel):
    entry_time: str
    exit_time: str
    side: Side
    algorithm: str
    signal_regime: str
    pnl: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cost: Decimal
    initial_risk_amount: Decimal = Field(default=Decimal("0"), ge=0)
    exit_reasons: list[str] = Field(default_factory=list)


class BacktestStrategyAttribution(BaseModel):
    algorithm: str
    total_trades: int
    long_trades: int
    short_trades: int
    wins: int
    losses: int
    win_rate_percent: Decimal
    net_profit: Decimal
    return_percent: Decimal
    profit_factor: Decimal
    average_trade: Decimal
    total_costs: Decimal
    trade_share_percent: Decimal


class BacktestWindowSummary(BaseModel):
    label: str
    sample_bars: int
    data_start: datetime
    data_end: datetime
    net_profit: Decimal
    return_percent: Decimal
    max_drawdown_percent: Decimal
    win_rate_percent: Decimal
    profit_factor: Decimal
    total_trades: int


class TemporalStabilityReport(BaseModel):
    """Chronological fixed-spec validation across non-overlapping time folds."""

    mode: str = "fixed_spec_chronological_folds"
    lookahead_safe: bool = True
    parameter_mode: str = "immutable_no_in_sample_tuning"
    fold_count: int
    minimum_trades_per_fold: int
    evaluable_folds: int
    positive_folds: int
    positive_fold_ratio: Decimal
    worst_fold_return_percent: Decimal | None = None
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    windows: list[BacktestWindowSummary] = Field(default_factory=list)


class BacktestResult(BaseModel):
    initial_equity: Decimal
    final_equity: Decimal
    net_profit: Decimal
    return_percent: Decimal
    max_drawdown_percent: Decimal
    win_rate_percent: Decimal
    profit_factor: Decimal
    sharpe: Decimal
    total_trades: int
    trades: list[BacktestTrade]
    trade_episodes: list[BacktestTradeEpisode] = Field(default_factory=list)
    strategy_attribution: list[BacktestStrategyAttribution] = Field(default_factory=list)
    equity_curve: list[Decimal]
    strategy_fingerprint: str | None = None
    data_fingerprint: str | None = None
    dataset_id: str | None = None
    data_start: datetime | None = None
    data_end: datetime | None = None
    annualized_return_percent: Decimal = Decimal("0")
    calmar: Decimal = Decimal("0")
    expectancy: Decimal = Decimal("0")
    average_trade: Decimal = Decimal("0")
    average_win: Decimal = Decimal("0")
    average_loss: Decimal = Decimal("0")
    payoff_ratio: Decimal = Decimal("0")
    win_loss_quality_passed: bool = False
    total_fees: Decimal = Decimal("0")
    total_slippage: Decimal = Decimal("0")
    total_funding: Decimal = Decimal("0")
    exposure_percent: Decimal = Decimal("0")
    annualization_periods: Decimal = Decimal("0")
    sample_bars: int = 0
    sample_years: Decimal = Decimal("0")
    stress_return_percent: Decimal | None = None
    robustness_passed: bool = False
    validation_reasons: list[str] = Field(default_factory=list)
    trade_sample_standard: int = 0
    trade_sample_floor: int = 0
    trade_sample_strong_floor: int = 0
    trade_sample_corroborated: bool = False
    trade_sample_strongly_corroborated: bool = False
    trade_sample_tier: str = "insufficient"
    cost_model: dict[str, str | bool] = Field(default_factory=dict)
    recent_window: BacktestWindowSummary | None = None
    temporal_stability: TemporalStabilityReport | None = None
    parameter_walk_forward: dict | None = None
    statistical_validation: StatisticalValidationReport | None = None
    integrity_checks: dict[str, bool | str | int] = Field(default_factory=dict)
    # True means an otherwise open position had to be force-closed at data end.
    terminal_position_closed: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _aggregate_trade_episodes(
    trades: list[BacktestTrade],
) -> list[BacktestTradeEpisode]:
    """Combine partial exits into independent entry-to-flat trade episodes."""

    grouped: dict[tuple[str, Side, str, str], dict] = {}
    order: list[tuple[str, Side, str, str]] = []
    for trade in trades:
        key = (
            trade.entry_time,
            trade.side,
            trade.algorithm,
            trade.signal_regime,
        )
        if key not in grouped:
            order.append(key)
            grouped[key] = {
                "entry_time": trade.entry_time,
                "exit_time": trade.exit_time,
                "side": trade.side,
                "algorithm": trade.algorithm,
                "signal_regime": trade.signal_regime,
                "pnl": Decimal("0"),
                "gross_pnl": Decimal("0"),
                "fees": Decimal("0"),
                "slippage_cost": Decimal("0"),
                "funding_cost": Decimal("0"),
                "initial_risk_amount": Decimal("0"),
                "exit_reasons": [],
            }
        episode = grouped[key]
        episode["exit_time"] = max(episode["exit_time"], trade.exit_time)
        for field in (
            "pnl",
            "gross_pnl",
            "fees",
            "slippage_cost",
            "funding_cost",
            "initial_risk_amount",
        ):
            episode[field] += getattr(trade, field)
        if trade.exit_reason not in episode["exit_reasons"]:
            episode["exit_reasons"].append(trade.exit_reason)
    return [BacktestTradeEpisode.model_validate(grouped[key]) for key in order]


def _strategy_attribution(
    episodes: list[BacktestTradeEpisode],
    *,
    initial_equity: Decimal,
) -> list[BacktestStrategyAttribution]:
    grouped: dict[str, list[BacktestTradeEpisode]] = {}
    for episode in episodes:
        grouped.setdefault(episode.algorithm, []).append(episode)
    total = len(episodes)
    result: list[BacktestStrategyAttribution] = []
    for algorithm, items in grouped.items():
        wins = [item.pnl for item in items if item.pnl > 0]
        losses = [item.pnl for item in items if item.pnl < 0]
        net = sum((item.pnl for item in items), Decimal("0"))
        profit_factor = (
            sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
            if losses
            else (Decimal("999.99") if wins else Decimal("0"))
        )
        result.append(
            BacktestStrategyAttribution(
                algorithm=algorithm,
                total_trades=len(items),
                long_trades=sum(item.side == Side.LONG for item in items),
                short_trades=sum(item.side == Side.SHORT for item in items),
                wins=len(wins),
                losses=len(losses),
                win_rate_percent=(
                    Decimal(len(wins)) / Decimal(len(items)) * Decimal("100")
                    if items
                    else Decimal("0")
                ).quantize(Decimal("0.01")),
                net_profit=net.quantize(Decimal("0.01")),
                return_percent=(
                    net / initial_equity * Decimal("100") if initial_equity else Decimal("0")
                ).quantize(Decimal("0.01")),
                profit_factor=profit_factor.quantize(Decimal("0.01")),
                average_trade=(net / Decimal(len(items)) if items else Decimal("0")).quantize(
                    Decimal("0.01")
                ),
                total_costs=sum(
                    (item.fees + item.slippage_cost + item.funding_cost for item in items),
                    Decimal("0"),
                ).quantize(Decimal("0.01")),
                trade_share_percent=(
                    Decimal(len(items)) / Decimal(total) * Decimal("100") if total else Decimal("0")
                ).quantize(Decimal("0.01")),
            )
        )
    return sorted(result, key=lambda item: item.algorithm)


def _summarize(
    initial_equity: Decimal,
    equity: Decimal,
    max_drawdown: Decimal,
    trades: list[BacktestTrade],
    curve: list[Decimal],
    annual_periods: float,
    sample_bars: int,
    exposure_bars: int,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    funding_points: int = 0,
    terminal_position_closed: bool = False,
) -> BacktestResult:
    episodes = _aggregate_trade_episodes(trades)
    wins = [episode.pnl for episode in episodes if episode.pnl > 0]
    losses = [episode.pnl for episode in episodes if episode.pnl < 0]
    profit_factor = (
        sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
        if losses
        else (Decimal("999.99") if wins else Decimal("0"))
    )
    returns = []
    for previous, current in zip(curve, curve[1:], strict=False):
        returns.append((current - previous) / previous if previous else Decimal("0"))
    if len(returns) > 1:
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum((item - mean) ** 2 for item in returns) / Decimal(len(returns) - 1)
        sharpe = float(mean) / sqrt(float(variance)) * sqrt(annual_periods) if variance > 0 else 0
    else:
        sharpe = 0
    net = equity - initial_equity
    sample_years = Decimal(str(sample_bars / annual_periods)) if sample_bars else Decimal("0")
    if sample_bars and equity > 0 and initial_equity > 0:
        annualized = (
            Decimal(str((float(equity / initial_equity) ** (annual_periods / sample_bars)) - 1))
            * 100
        )
    else:
        annualized = Decimal("0")
    total_fees = sum((trade.fees for trade in trades), Decimal("0"))
    total_slippage = sum((trade.slippage_cost for trade in trades), Decimal("0"))
    total_funding = sum((trade.funding_cost for trade in trades), Decimal("0"))
    average_trade = net / Decimal(len(episodes)) if episodes else Decimal("0")
    win_rate = Decimal(len(wins)) / Decimal(len(episodes)) if episodes else Decimal("0")
    average_win = sum(wins, Decimal("0")) / Decimal(len(wins)) if wins else Decimal("0")
    average_loss = abs(sum(losses, Decimal("0")) / Decimal(len(losses))) if losses else Decimal("0")
    expectancy = win_rate * average_win - (Decimal("1") - win_rate) * average_loss
    payoff_ratio = (
        average_win / average_loss
        if average_loss > 0
        else (Decimal("999.99") if average_win > 0 else Decimal("0"))
    )
    return BacktestResult(
        initial_equity=initial_equity,
        final_equity=equity.quantize(Decimal("0.01")),
        net_profit=net.quantize(Decimal("0.01")),
        return_percent=(net / initial_equity * 100).quantize(Decimal("0.01")),
        max_drawdown_percent=(max_drawdown * 100).quantize(Decimal("0.01")),
        win_rate_percent=(
            Decimal(len(wins)) / Decimal(len(episodes)) * 100 if episodes else Decimal("0")
        ).quantize(Decimal("0.01")),
        profit_factor=profit_factor.quantize(Decimal("0.01")),
        sharpe=Decimal(str(sharpe)).quantize(Decimal("0.01")),
        total_trades=len(episodes),
        trades=trades,
        trade_episodes=episodes,
        strategy_attribution=_strategy_attribution(
            episodes,
            initial_equity=initial_equity,
        ),
        equity_curve=curve,
        annualized_return_percent=annualized.quantize(Decimal("0.01")),
        calmar=(annualized / (max_drawdown * 100) if max_drawdown > 0 else Decimal("0")).quantize(
            Decimal("0.01")
        ),
        expectancy=expectancy.quantize(Decimal("0.01")),
        average_trade=average_trade.quantize(Decimal("0.01")),
        average_win=average_win.quantize(Decimal("0.01")),
        average_loss=average_loss.quantize(Decimal("0.01")),
        payoff_ratio=payoff_ratio.quantize(Decimal("0.01")),
        total_fees=total_fees.quantize(Decimal("0.01")),
        total_slippage=total_slippage.quantize(Decimal("0.01")),
        total_funding=total_funding.quantize(Decimal("0.01")),
        exposure_percent=(
            Decimal(exposure_bars) / Decimal(sample_bars) * 100 if sample_bars else Decimal("0")
        ).quantize(Decimal("0.01")),
        annualization_periods=Decimal(str(annual_periods)).quantize(Decimal("0.01")),
        sample_bars=sample_bars,
        sample_years=sample_years.quantize(Decimal("0.001")),
        cost_model={
            "fee_bps": str(fee_bps),
            "slippage_bps": str(slippage_bps),
            "funding_included": funding_points > 0,
            "funding_points": str(funding_points),
            "funding_note": (
                "historical funding payments included"
                if funding_points
                else "historical funding unavailable; current rate was not backfilled"
            ),
        },
        terminal_position_closed=terminal_position_closed,
        integrity_checks={
            "closed_candle_signals_only": True,
            "next_bar_execution": True,
            "stop_first_on_ambiguous_bar": True,
            "terminal_position_liquidated": True,
            "historical_funding_not_backfilled_from_current": True,
        },
    )


def _marked_equity(
    realized_equity: Decimal,
    position: dict | None,
    mark_price: Decimal,
    fee_rate: Decimal,
) -> Decimal:
    """Return liquidation-free mark-to-market equity for an open backtest position.

    Entry fees for the remaining quantity are deducted immediately because they have
    already been incurred even though trade-level accounting records them at exit.
    """

    if position is None:
        return realized_equity
    plan: StrategyPlan = position["plan"]
    remaining = position["remaining"]
    direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
    unrealized = (mark_price - position["entry"]) * remaining * direction
    accrued_entry_fee = position["entry"] * remaining * fee_rate
    return realized_equity + unrealized - accrued_entry_fee


def _simulated_slippage_fraction(
    *,
    base_slippage_bps: Decimal,
    market_impact_bps: Decimal,
    quantity: Decimal,
    candle_volume: Decimal,
) -> Decimal:
    participation = quantity / candle_volume if candle_volume > 0 and quantity > 0 else Decimal("1")
    impact = Decimal(str(sqrt(float(max(participation, Decimal("0")))))) * market_impact_bps
    total_bps = min(
        base_slippage_bps + impact,
        Decimal("500"),
    )
    return total_bps / Decimal("10000")


def validate_research_result(
    result: BacktestResult,
    stressed: BacktestResult,
    policy: ResearchValidationPreferences,
    *,
    research_trial_count: int = 1,
) -> BacktestResult:
    """Apply evidence-aware research gates to an already completed walk-forward.

    The normal trade-count standard remains authoritative. A near-standard
    sample is accepted only when full-sample PF, stressed costs, and a
    sufficiently populated recent window independently agree on positive edge.
    """

    trade_floor = int(
        (Decimal(policy.minimum_trades) * policy.minimum_trade_evidence_ratio).to_integral_value(
            rounding=ROUND_UP
        )
    )
    win_fraction = result.win_rate_percent / Decimal("100")
    implied_payoff_ratio = (
        result.profit_factor * (Decimal("1") - win_fraction) / win_fraction
        if result.payoff_ratio <= 0 and Decimal("0") < win_fraction < Decimal("1")
        else result.payoff_ratio
    )
    win_loss_quality_passed = bool(
        result.win_rate_percent >= policy.minimum_win_rate_percent
        and implied_payoff_ratio >= policy.minimum_payoff_ratio
        and result.profit_factor > Decimal("1")
    )
    strong_trade_floor = max(
        30,
        int(
            (Decimal(policy.minimum_trades) * Decimal("0.30")).to_integral_value(rounding=ROUND_UP)
        ),
    )
    recent = result.recent_window
    temporal = result.temporal_stability
    near_standard_corroborated = bool(
        result.total_trades >= trade_floor
        and result.profit_factor >= policy.minimum_profit_factor
        and win_loss_quality_passed
        and stressed.return_percent > 0
        and recent
        and recent.total_trades >= policy.minimum_recent_trades
        and recent.return_percent > 0
        and recent.profit_factor >= policy.minimum_profit_factor
        and temporal
        and temporal.passed
    )
    strong_profit_factor = max(
        policy.minimum_profit_factor + Decimal("0.15"),
        Decimal("1.30"),
    )
    strongly_corroborated = bool(
        result.total_trades >= strong_trade_floor
        and result.profit_factor >= strong_profit_factor
        and win_loss_quality_passed
        and stressed.return_percent > 0
        and recent
        and recent.total_trades >= policy.minimum_recent_trades
        and recent.return_percent > 0
        and recent.profit_factor >= policy.minimum_profit_factor
        and temporal
        and temporal.passed
        and temporal.evaluable_folds >= 3
        and temporal.positive_fold_ratio >= Decimal("0.75")
        and temporal.worst_fold_return_percent is not None
        and temporal.worst_fold_return_percent > 0
    )
    trade_sample_corroborated = bool(near_standard_corroborated or strongly_corroborated)
    trade_sample_passed = bool(
        result.total_trades >= policy.minimum_trades or trade_sample_corroborated
    )
    trade_sample_tier = (
        "standard"
        if result.total_trades >= policy.minimum_trades
        else "near_standard_corroborated"
        if near_standard_corroborated
        else "strong_multi_evidence"
        if strongly_corroborated
        else "insufficient"
    )
    reasons: list[str] = []
    if result.sample_bars < policy.minimum_sample_bars:
        reasons.append(f"研究样本少于 {policy.minimum_sample_bars} 根 K 线")
    if not trade_sample_passed:
        reasons.append(
            f"有效交易少于 {policy.minimum_trades} 笔，且未达到"
            f"{trade_floor} 笔以上的一般佐证，或 {strong_trade_floor} 笔以上的"
            "强总体/压力/近期/时间分段一致证据"
        )
    if result.profit_factor < policy.minimum_profit_factor:
        reasons.append(f"Profit Factor 低于 {policy.minimum_profit_factor:f}")
    if result.win_rate_percent < policy.minimum_win_rate_percent:
        reasons.append(
            f"胜率 {result.win_rate_percent:f}% 低于执行级门槛 {policy.minimum_win_rate_percent:f}%"
        )
    if implied_payoff_ratio < policy.minimum_payoff_ratio:
        reasons.append(
            f"平均盈利/平均亏损 {implied_payoff_ratio:f} 低于执行级门槛 "
            f"{policy.minimum_payoff_ratio:f}"
        )
    if result.max_drawdown_percent > policy.maximum_drawdown_percent:
        reasons.append(f"最大回撤超过 {policy.maximum_drawdown_percent:f}%")
    if stressed.return_percent <= 0:
        reasons.append(
            f"{policy.stress_fee_multiplier:f}x 手续费、"
            f"{policy.stress_slippage_multiplier:f}x 滑点压力场景收益不为正"
        )
    if result.total_trades >= trade_floor and (temporal is None or not temporal.passed):
        detail = "；".join(temporal.reasons) if temporal is not None else "缺少时间分段验证"
        reasons.append(f"时间稳定性未通过：{detail}")
    statistical_validation = validate_statistical_evidence(
        annualized_sharpe=result.sharpe,
        annualization_periods=result.annualization_periods,
        observation_count=max(result.sample_bars - 1, 0),
        pnls=[episode.pnl for episode in result.trade_episodes],
        initial_equity=result.initial_equity,
        trial_count=research_trial_count,
        minimum_win_rate_percent=policy.minimum_win_rate_percent,
        minimum_payoff_ratio=policy.minimum_payoff_ratio,
        maximum_drawdown_percent=policy.maximum_drawdown_percent,
    )
    if result.trade_episodes and not statistical_validation.uncertainty_passed:
        reasons.extend(statistical_validation.reasons)
    passed_evidence = ["基础稳健性门槛全部通过"]
    if near_standard_corroborated and result.total_trades < policy.minimum_trades:
        passed_evidence.append(
            f"交易样本 {result.total_trades}/{policy.minimum_trades}；"
            f"已由正压力收益与近期 {recent.total_trades} 笔、"
            f"PF {recent.profit_factor:f} 的一致证据补偿"
        )
    if strongly_corroborated and result.total_trades < trade_floor:
        passed_evidence.append(
            f"交易样本 {result.total_trades}/{policy.minimum_trades}，达到强证据最低 "
            f"{strong_trade_floor} 笔；全样本 PF {result.profit_factor:f}、"
            f"压力收益 {stressed.return_percent:f}% 与 "
            f"{temporal.evaluable_folds} 个可评估时间分段共同佐证；"
            "自动执行前仍须通过参数前推验证"
        )
    return result.model_copy(
        update={
            "stress_return_percent": stressed.return_percent,
            "robustness_passed": not reasons,
            "validation_reasons": reasons or passed_evidence,
            "trade_sample_standard": policy.minimum_trades,
            "trade_sample_floor": trade_floor,
            "trade_sample_strong_floor": strong_trade_floor,
            "trade_sample_corroborated": trade_sample_corroborated,
            "trade_sample_strongly_corroborated": strongly_corroborated,
            "trade_sample_tier": trade_sample_tier,
            "cost_model": {
                **result.cost_model,
                "stress_fee_multiplier": str(policy.stress_fee_multiplier),
                "stress_slippage_multiplier": str(policy.stress_slippage_multiplier),
            },
            "payoff_ratio": implied_payoff_ratio.quantize(Decimal("0.01")),
            "win_loss_quality_passed": win_loss_quality_passed,
            "statistical_validation": statistical_validation,
        }
    )


def _drawdown_state(
    marked_equity: Decimal,
    peak: Decimal,
    max_drawdown: Decimal,
) -> tuple[Decimal, Decimal]:
    peak = max(peak, marked_equity)
    drawdown = (peak - marked_equity) / peak if peak > 0 else Decimal("0")
    return peak, max(max_drawdown, drawdown)


def _recent_window_summary(
    result: BacktestResult,
    candles: list[Candle],
) -> BacktestWindowSummary:
    sample_bars = min(
        len(candles),
        max(200, int(len(candles) * 0.25)),
    )
    recent_candles = candles[-sample_bars:]
    start_at = recent_candles[0].timestamp
    comparison_start = start_at if start_at.tzinfo else start_at.replace(tzinfo=UTC)

    def exit_at(trade: BacktestTrade) -> datetime:
        parsed = datetime.fromisoformat(trade.exit_time)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    recent_trades = [trade for trade in result.trade_episodes if exit_at(trade) >= comparison_start]
    net_profit = sum((trade.pnl for trade in recent_trades), Decimal("0"))
    recent_initial = result.final_equity - net_profit
    return_percent = (
        net_profit / recent_initial * Decimal("100") if recent_initial else Decimal("0")
    )
    wins = [trade.pnl for trade in recent_trades if trade.pnl > 0]
    losses = [trade.pnl for trade in recent_trades if trade.pnl < 0]
    profit_factor = (
        sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
        if losses
        else (Decimal("999.99") if wins else Decimal("0"))
    )
    curve = result.equity_curve[-min(sample_bars, len(result.equity_curve)) :]
    peak = curve[0] if curve else result.final_equity
    max_drawdown = Decimal("0")
    for equity in curve:
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return BacktestWindowSummary(
        label="latest_25_percent",
        sample_bars=sample_bars,
        data_start=start_at,
        data_end=recent_candles[-1].timestamp,
        net_profit=net_profit.quantize(Decimal("0.01")),
        return_percent=return_percent.quantize(Decimal("0.01")),
        max_drawdown_percent=(max_drawdown * Decimal("100")).quantize(Decimal("0.01")),
        win_rate_percent=(
            Decimal(len(wins)) / Decimal(len(recent_trades)) * Decimal("100")
            if recent_trades
            else Decimal("0")
        ).quantize(Decimal("0.01")),
        profit_factor=profit_factor.quantize(Decimal("0.01")),
        total_trades=len(recent_trades),
    )


def _temporal_stability_report(
    result: BacktestResult,
    candles: list[Candle],
    *,
    fold_count: int = 4,
) -> TemporalStabilityReport:
    """Measure whether a fixed StrategySpec's edge persists across time."""

    start_index = min(55, max(len(candles) - 1, 0))
    usable = candles[start_index:]
    minimum_trades = max(3, min(10, result.total_trades // 8 or 3))

    def aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    def exit_at(trade: BacktestTrade) -> datetime:
        return aware(datetime.fromisoformat(trade.exit_time))

    windows: list[BacktestWindowSummary] = []
    if not usable:
        return TemporalStabilityReport(
            fold_count=fold_count,
            minimum_trades_per_fold=minimum_trades,
            evaluable_folds=0,
            positive_folds=0,
            positive_fold_ratio=Decimal("0"),
            passed=False,
            reasons=["没有足够的时间序列样本建立分段验证。"],
        )
    for fold in range(fold_count):
        lower = fold * len(usable) // fold_count
        upper = (fold + 1) * len(usable) // fold_count
        segment = usable[lower:upper]
        if not segment:
            continue
        start_at = aware(segment[0].timestamp)
        end_at = aware(segment[-1].timestamp)
        previous_pnl = sum(
            (trade.pnl for trade in result.trade_episodes if exit_at(trade) < start_at),
            Decimal("0"),
        )
        segment_trades = [
            trade for trade in result.trade_episodes if start_at <= exit_at(trade) <= end_at
        ]
        starting_equity = result.initial_equity + previous_pnl
        equity = starting_equity
        peak = equity
        max_drawdown = Decimal("0")
        for trade in segment_trades:
            equity += trade.pnl
            peak, max_drawdown = _drawdown_state(equity, peak, max_drawdown)
        net_profit = equity - starting_equity
        wins = [trade.pnl for trade in segment_trades if trade.pnl > 0]
        losses = [trade.pnl for trade in segment_trades if trade.pnl < 0]
        profit_factor = (
            sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
            if losses
            else (Decimal("999.99") if wins else Decimal("0"))
        )
        windows.append(
            BacktestWindowSummary(
                label=f"chronological_{fold + 1}",
                sample_bars=len(segment),
                data_start=segment[0].timestamp,
                data_end=segment[-1].timestamp,
                net_profit=net_profit.quantize(Decimal("0.01")),
                return_percent=(
                    net_profit / starting_equity * Decimal("100")
                    if starting_equity > 0
                    else Decimal("0")
                ).quantize(Decimal("0.01")),
                max_drawdown_percent=(max_drawdown * Decimal("100")).quantize(Decimal("0.01")),
                win_rate_percent=(
                    Decimal(len(wins)) / Decimal(len(segment_trades)) * Decimal("100")
                    if segment_trades
                    else Decimal("0")
                ).quantize(Decimal("0.01")),
                profit_factor=profit_factor.quantize(Decimal("0.01")),
                total_trades=len(segment_trades),
            )
        )
    evaluable = [window for window in windows if window.total_trades >= minimum_trades]
    positive = [
        window
        for window in evaluable
        if window.return_percent > 0 and window.profit_factor >= Decimal("1")
    ]
    positive_ratio = Decimal(len(positive)) / Decimal(len(evaluable)) if evaluable else Decimal("0")
    positive_profit = sum(
        (max(window.net_profit, Decimal("0")) for window in evaluable),
        Decimal("0"),
    )
    concentration = (
        max(
            (max(window.net_profit, Decimal("0")) for window in evaluable),
            default=Decimal("0"),
        )
        / positive_profit
        if positive_profit > 0
        else Decimal("1")
    )
    reasons: list[str] = []
    if len(evaluable) < 3:
        reasons.append(
            f"仅 {len(evaluable)}/{fold_count} 个时间分段达到 {minimum_trades} 笔最低可评估样本。"
        )
    if evaluable and positive_ratio < Decimal("0.5"):
        reasons.append(f"正收益时间分段占比 {positive_ratio * 100:.1f}% 低于 50%。")
    if len(evaluable) >= 3 and concentration > Decimal("0.70"):
        reasons.append(f"{concentration * 100:.1f}% 的分段正收益集中在单一时期。")
    passed = not reasons
    return TemporalStabilityReport(
        fold_count=fold_count,
        minimum_trades_per_fold=minimum_trades,
        evaluable_folds=len(evaluable),
        positive_folds=len(positive),
        positive_fold_ratio=positive_ratio,
        worst_fold_return_percent=(
            min(window.return_percent for window in evaluable) if evaluable else None
        ),
        passed=passed,
        reasons=reasons or ["固定参数策略在多个非重叠时间分段保持正向证据。"],
        windows=windows,
    )


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
            (quantity * (point.mark_price or fallback_price) * point.rate * direction)
            for point in funding_rates
            if entry_time < point.timestamp <= exit_time
        ),
        Decimal("0"),
    ).quantize(Decimal("0.01"))


def _update_atr_trailing_stop(
    position: dict,
    plan: StrategyPlan,
    candles: list[Candle],
    index: int,
) -> None:
    targets_reached = int(position.get("target_index", 0))
    if plan.exit.break_even_after_first_target and targets_reached >= 1:
        if plan.side == Side.LONG:
            position["current_stop"] = max(position["current_stop"], position["entry"])
        else:
            position["current_stop"] = min(position["current_stop"], position["entry"])
    if plan.exit.trailing_stop_atr is None or index < 14:
        return
    if plan.side == Side.LONG:
        position["favorable_price"] = max(
            position["favorable_price"],
            candles[index].high,
        )
        favorable_move = position["favorable_price"] - position["entry"]
    else:
        position["favorable_price"] = min(
            position["favorable_price"],
            candles[index].low,
        )
        favorable_move = position["entry"] - position["favorable_price"]
    initial_risk = abs(position["entry"] - plan.exit.stop_loss)
    if not plan.exit.trailing_is_active(
        targets_reached=targets_reached,
        favorable_move=max(favorable_move, Decimal("0")),
        initial_risk=initial_risk,
    ):
        return
    trail_distance = atr(candles[max(0, index - 14) : index + 1], 14) * plan.exit.trailing_stop_atr
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


def run_bracket_backtest(
    plan: StrategyPlan,
    candles: list[Candle],
    initial_equity: Decimal = Decimal("100000"),
    fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
) -> BacktestResult:
    """Conservative single-position bracket simulation.

    If stop and target are touched in the same candle, the stop is assumed first.
    This avoids optimistic intrabar ordering without tick data.
    """
    equity = initial_equity
    peak = equity
    max_drawdown = Decimal("0")
    curve = [equity]
    trades: list[BacktestTrade] = []
    exposure_bars = 0
    position: dict | None = None
    entry_mid = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    target = plan.exit.take_profits[0].price
    slip = slippage_bps / Decimal("10000")
    fee_rate = fee_bps / Decimal("10000")

    terminal_position_closed = False
    for index, candle in enumerate(candles):
        if position is not None:
            exposure_bars += 1
        if position is None:
            touched = candle.low <= entry_mid <= candle.high
            if touched:
                fill = entry_mid * (
                    Decimal("1") + slip if plan.side == Side.LONG else Decimal("1") - slip
                )
                position = {
                    "plan": plan,
                    "entry": fill,
                    "time": candle.timestamp,
                    "quantity": plan.sizing.suggested_quantity,
                    "remaining": plan.sizing.suggested_quantity,
                    "current_stop": plan.exit.stop_loss,
                    "favorable_price": fill,
                    "target_index": 0,
                    "bars_held": 0,
                }
            marked = _marked_equity(equity, position, candle.close, fee_rate)
            peak, max_drawdown = _drawdown_state(marked, peak, max_drawdown)
            curve.append(marked)
            continue

        position["bars_held"] += 1
        stop_hit = (
            candle.low <= position["current_stop"]
            if plan.side == Side.LONG
            else candle.high >= position["current_stop"]
        )
        target_hit = candle.high >= target if plan.side == Side.LONG else candle.low <= target
        time_exit_due = plan.exit.time_exit_is_due(held_bars=position["bars_held"])
        if not stop_hit and not target_hit and not time_exit_due:
            _update_atr_trailing_stop(position, plan, candles, index)
            marked = _marked_equity(equity, position, candle.close, fee_rate)
            peak, max_drawdown = _drawdown_state(marked, peak, max_drawdown)
            curve.append(marked)
            continue

        exit_trigger = (
            position["current_stop"] if stop_hit else target if target_hit else candle.close
        )
        exit_price = exit_trigger
        exit_price *= Decimal("1") - slip if plan.side == Side.LONG else Decimal("1") + slip
        direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
        gross = (exit_price - position["entry"]) * position["quantity"] * direction
        fees = (position["entry"] + exit_price) * position["quantity"] * fee_rate
        raw_entry = entry_mid
        raw_exit = exit_trigger
        slippage_cost = (
            abs(position["entry"] - raw_entry) + abs(exit_price - raw_exit)
        ) * position["quantity"]
        pnl = (gross - fees).quantize(Decimal("0.01"))
        equity += pnl
        trades.append(
            BacktestTrade(
                entry_time=position["time"].isoformat(),
                exit_time=candle.timestamp.isoformat(),
                side=plan.side,
                algorithm=str(plan.algorithm or "unknown"),
                signal_regime=str(plan.regime),
                entry_price=position["entry"].quantize(Decimal("0.01")),
                exit_price=exit_price.quantize(Decimal("0.01")),
                quantity=position["quantity"],
                pnl=pnl,
                exit_reason=(
                    "trailing_stop"
                    if stop_hit and position["current_stop"] != plan.exit.stop_loss
                    else "stop_loss"
                    if stop_hit
                    else "take_profit"
                    if target_hit
                    else "time_stop"
                ),
                gross_pnl=gross.quantize(Decimal("0.01")),
                fees=fees.quantize(Decimal("0.01")),
                slippage_cost=slippage_cost.quantize(Decimal("0.01")),
                initial_risk_amount=(
                    abs(position["entry"] - plan.exit.stop_loss) * position["quantity"]
                ).quantize(Decimal("0.01")),
            )
        )
        position = None
        peak, max_drawdown = _drawdown_state(equity, peak, max_drawdown)
        curve.append(equity)

    if position is not None and candles:
        last = candles[-1]
        raw_exit = last.close
        exit_price = raw_exit * (
            Decimal("1") - slip if plan.side == Side.LONG else Decimal("1") + slip
        )
        direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
        gross = (exit_price - position["entry"]) * position["quantity"] * direction
        fees = (position["entry"] + exit_price) * position["quantity"] * fee_rate
        slippage_cost = abs(exit_price - raw_exit) * position["quantity"]
        pnl = (gross - fees).quantize(Decimal("0.01"))
        equity += pnl
        trades.append(
            BacktestTrade(
                entry_time=position["time"].isoformat(),
                exit_time=last.timestamp.isoformat(),
                side=plan.side,
                algorithm=str(plan.algorithm or "unknown"),
                signal_regime=str(plan.regime),
                entry_price=position["entry"].quantize(Decimal("0.01")),
                exit_price=exit_price.quantize(Decimal("0.01")),
                quantity=position["quantity"],
                pnl=pnl,
                exit_reason="end_of_data",
                gross_pnl=gross.quantize(Decimal("0.01")),
                fees=fees.quantize(Decimal("0.01")),
                slippage_cost=slippage_cost.quantize(Decimal("0.01")),
                initial_risk_amount=(
                    abs(position["entry"] - plan.exit.stop_loss) * position["quantity"]
                ).quantize(Decimal("0.01")),
            )
        )
        terminal_position_closed = True
        peak, max_drawdown = _drawdown_state(equity, peak, max_drawdown)
        curve[-1] = equity

    return _summarize(
        initial_equity,
        equity,
        max_drawdown,
        trades,
        curve,
        periods_per_year(plan.timeframe),
        len(candles),
        exposure_bars,
        fee_bps,
        slippage_bps,
        terminal_position_closed=terminal_position_closed,
    )


def run_walk_forward_backtest(
    candles: list[Candle],
    initial_equity: Decimal = Decimal("100000"),
    fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
    *,
    strategy_kind: StrategyKind = StrategyKind.TREND,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    exchange: str = "historical",
    risk_fraction: Decimal = Decimal("0.005"),
    minimum_reward_risk: Decimal = Decimal("1.8"),
    allow_short: bool = True,
    require_confirmation: bool = False,
    validation: ResearchValidationPreferences | None = None,
    strategy_spec: StrategySpec | dict | None = None,
    funding_rates: list[FundingRatePoint] | None = None,
    open_interest: list[OpenInterestPoint] | None = None,
    positioning: list[PositioningPoint] | None = None,
    basis: list[BasisPoint] | None = None,
    order_flow: list[OrderFlowBar] | None = None,
    liquidation_flow: list[LiquidationFlowPoint] | None = None,
    automatic_selection: bool = False,
    causal_strategy_priors: list[CausalStrategyPrior | dict] | None = None,
    causal_policy_max_candidates_per_bar: int = 3,
    causal_policy_abstain_on_ambiguous_regime: bool = True,
    causal_policy_minimum_regime_confidence: Decimal = Decimal("0"),
    causal_policy_minimum_signal_confidence: Decimal = Decimal("0"),
    causal_policy_minimum_directional_consensus: int = 1,
    side_filter: Side | None = None,
    require_regime_family_fit: bool = True,
    research_trial_count: int = 1,
    maximum_volume_participation: Decimal = Decimal("0.02"),
    market_impact_bps: Decimal = Decimal("5"),
    maximum_gross_exposure: Decimal = Decimal("2"),
    maximum_leverage: Decimal = Decimal("3"),
    compute_stress: bool = True,
) -> BacktestResult:
    """Walk-forward test with no same-bar signal execution.

    A plan is generated using data available at a candle close. Its entry can only
    fill on later candles. Pending plans expire after eight candles, and the same
    conservative stop-before-target rule is used for ambiguous candles.
    """
    if not 1 <= causal_policy_max_candidates_per_bar <= 20:
        raise ValueError("causal policy candidates per bar must be between 1 and 20")
    if not Decimal("0") <= causal_policy_minimum_regime_confidence <= Decimal("1"):
        raise ValueError("causal policy minimum regime confidence must be between 0 and 1")
    if not Decimal("0") <= causal_policy_minimum_signal_confidence <= Decimal("1"):
        raise ValueError("causal policy minimum signal confidence must be between 0 and 1")
    if not (
        1 <= causal_policy_minimum_directional_consensus <= causal_policy_max_candidates_per_bar
    ):
        raise ValueError("causal policy directional consensus must be between 1 and max candidates")
    causal_priors = [
        CausalStrategyPrior.model_validate(item) for item in (causal_strategy_priors or [])
    ]
    if causal_priors and automatic_selection:
        raise ValueError("default automatic selection and causal library policy are exclusive")
    if causal_priors and strategy_spec is not None:
        raise ValueError("causal library policy cannot also bind one static StrategySpec")
    prior_ids = [item.id for item in causal_priors]
    if len(prior_ids) != len(set(prior_ids)):
        raise ValueError("causal library policy prior identifiers must be unique")
    prior_fingerprints = [item.spec_fingerprint for item in causal_priors]
    if len(prior_fingerprints) != len(set(prior_fingerprints)):
        raise ValueError("causal library policy StrategySpec fingerprints must be unique")
    policy_fingerprint = (
        causal_library_policy_fingerprint(
            causal_priors,
            maximum_candidates_per_bar=causal_policy_max_candidates_per_bar,
            abstain_on_ambiguous_regime=(causal_policy_abstain_on_ambiguous_regime),
            minimum_regime_confidence=causal_policy_minimum_regime_confidence,
            minimum_signal_confidence=causal_policy_minimum_signal_confidence,
            minimum_directional_consensus=(causal_policy_minimum_directional_consensus),
        )
        if causal_priors
        else None
    )
    validated_spec = (
        None
        if automatic_selection or causal_priors
        else validate_strategy_spec(strategy_spec, strategy_kind)
    )
    warmup_bars = (
        max(55, *(minimum_signal_bars(item.validated_spec) for item in causal_priors))
        if causal_priors
        else 55
        if validated_spec is None
        else minimum_signal_bars(validated_spec)
    )
    higher_timeframe = confirmation_timeframe(timeframe)
    confirmation_source_ratio = max(
        1,
        timeframe_seconds(higher_timeframe) // timeframe_seconds(timeframe),
    )
    confirmation_slow = (
        max(item.validated_spec.parameters.confirmation_ema_slow for item in causal_priors)
        if causal_priors
        else 50
        if validated_spec is None
        else validated_spec.parameters.confirmation_ema_slow
    )
    history_limit = max(
        500,
        (confirmation_slow * confirmation_source_ratio + confirmation_source_ratio)
        if require_confirmation and higher_timeframe != timeframe
        else 0,
    )
    confirmation_history_limit = 300
    confirmation_series = (
        []
        if higher_timeframe == timeframe
        else resample_closed_candles(
            candles,
            timeframe,
            higher_timeframe,
        )
    )
    confirmation_close_times = [
        candle_close_time(item, higher_timeframe) for item in confirmation_series
    ]
    if len(candles) < warmup_bars + 2:
        raise ValueError(
            "walk-forward backtest requires at least "
            f"{warmup_bars + 2} candles for this strategy spec"
        )
    strategy_factory = StrategyFactory()
    equity = initial_equity
    peak = equity
    max_drawdown = Decimal("0")
    curve = [equity]
    trades: list[BacktestTrade] = []
    exposure_bars = 0
    pending: tuple[StrategyPlan, int] | None = None
    position: dict | None = None
    fee_rate = fee_bps / Decimal("10000")
    funding_rates = funding_rates or []
    funding_rates = sorted(
        funding_rates,
        key=lambda item: point_available_time(item, fallback=item.timestamp),
    )
    funding_timestamps = [
        point_available_time(item, fallback=item.timestamp) for item in funding_rates
    ]
    open_interest = sorted(
        open_interest or [],
        key=lambda item: point_available_time(item, fallback=item.timestamp),
    )
    open_interest_timestamps = [
        point_available_time(item, fallback=item.timestamp) for item in open_interest
    ]
    positioning = sorted(
        positioning or [],
        key=lambda item: point_available_time(item, fallback=item.timestamp),
    )
    positioning_timestamps = [
        point_available_time(item, fallback=item.timestamp) for item in positioning
    ]
    basis = sorted(
        basis or [],
        key=lambda item: point_available_time(item, fallback=item.timestamp),
    )
    basis_timestamps = [point_available_time(item, fallback=item.timestamp) for item in basis]
    order_flow = sorted(
        order_flow or [],
        key=lambda item: point_available_time(
            item,
            fallback=item.timestamp + timedelta(seconds=timeframe_seconds(timeframe)),
        ),
    )
    order_flow_availability_timestamps = [
        point_available_time(
            item,
            fallback=item.timestamp + timedelta(seconds=timeframe_seconds(timeframe)),
        )
        for item in order_flow
    ]
    liquidation_flow = sorted(
        liquidation_flow or [],
        key=lambda item: point_available_time(item, fallback=item.timestamp),
    )
    liquidation_flow_timestamps = [
        point_available_time(item, fallback=item.timestamp) for item in liquidation_flow
    ]
    terminal_position_closed = False
    causal_policy_selection_counts: dict[str, int] = {}
    causal_policy_abstentions = 0
    notional_cap_count = 0
    gross_exposure_cap_count = 0
    leverage_cap_count = 0
    volume_cap_count = 0

    for index in range(warmup_bars, len(candles)):
        candle = candles[index]
        if position is not None:
            exposure_bars += 1
        if position is not None:
            plan: StrategyPlan = position["plan"]
            position["bars_held"] += 1
            stop_hit = (
                candle.low <= position["current_stop"]
                if plan.side == Side.LONG
                else candle.high >= position["current_stop"]
            )
            exits: list[tuple[Decimal, Decimal, str]] = []
            if stop_hit:
                exit_reason = (
                    "trailing_stop"
                    if position["current_stop"] != plan.exit.stop_loss
                    else "stop_loss"
                )
                exits.append((position["current_stop"], position["remaining"], exit_reason))
            else:
                targets = plan.exit.take_profits
                while position["target_index"] < len(targets):
                    target = targets[position["target_index"]]
                    target_hit = (
                        candle.high >= target.price
                        if plan.side == Side.LONG
                        else candle.low <= target.price
                    )
                    if not target_hit:
                        break
                    is_final = position["target_index"] == len(targets) - 1
                    close_quantity = (
                        position["remaining"]
                        if is_final
                        else min(
                            position["original_quantity"] * target.close_fraction,
                            position["remaining"],
                        )
                    )
                    exits.append((target.price, close_quantity, "take_profit"))
                    position["target_index"] += 1

                projected_remaining = position["remaining"] - sum(
                    (quantity for _, quantity, _ in exits),
                    Decimal("0"),
                )
                if projected_remaining > 0 and plan.exit.time_exit_is_due(
                    held_bars=position["bars_held"],
                ):
                    exits.append((candle.close, projected_remaining, "time_stop"))

            for trigger_price, close_quantity, exit_reason in exits:
                exit_slip = _simulated_slippage_fraction(
                    base_slippage_bps=slippage_bps,
                    market_impact_bps=market_impact_bps,
                    quantity=close_quantity,
                    candle_volume=candle.volume,
                )
                exit_price = trigger_price
                exit_price *= (
                    Decimal("1") - exit_slip if plan.side == Side.LONG else Decimal("1") + exit_slip
                )
                direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
                gross = (exit_price - position["entry"]) * close_quantity * direction
                fees = (position["entry"] + exit_price) * close_quantity * fee_rate
                raw_entry = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
                slippage_cost = (
                    abs(position["entry"] - raw_entry) + abs(exit_price - trigger_price)
                ) * close_quantity
                funding_cost = _funding_cost(
                    funding_rates,
                    entry_time=position["time"],
                    exit_time=candle.timestamp,
                    side=plan.side,
                    quantity=close_quantity,
                    fallback_price=position["entry"],
                )
                pnl = (gross - fees - funding_cost).quantize(Decimal("0.01"))
                equity += pnl
                trades.append(
                    BacktestTrade(
                        entry_time=position["time"].isoformat(),
                        exit_time=candle.timestamp.isoformat(),
                        side=plan.side,
                        algorithm=str(plan.algorithm or "unknown"),
                        signal_regime=str(plan.regime),
                        entry_price=position["entry"].quantize(Decimal("0.01")),
                        exit_price=exit_price.quantize(Decimal("0.01")),
                        quantity=close_quantity,
                        intended_quantity=position["intended_quantity"],
                        actual_entry_quantity=position["original_quantity"],
                        position_limit_reason=position["position_limit_reason"],
                        pnl=pnl,
                        exit_reason=exit_reason,
                        gross_pnl=gross.quantize(Decimal("0.01")),
                        fees=fees.quantize(Decimal("0.01")),
                        slippage_cost=slippage_cost.quantize(Decimal("0.01")),
                        funding_cost=funding_cost,
                        initial_risk_amount=(
                            abs(position["entry"] - plan.exit.stop_loss) * close_quantity
                        ).quantize(Decimal("0.01")),
                    )
                )
                position["remaining"] -= close_quantity
            if position is not None and position["remaining"] <= 0:
                position = None
            elif position is not None:
                _update_atr_trailing_stop(position, plan, candles, index)

        if position is None and pending is not None:
            plan, expires_index = pending
            entry_mid = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
            if candle.low <= entry_mid <= candle.high:
                available_equity = max(equity, Decimal("0"))
                gross_exposure_quantity = (
                    available_equity * maximum_gross_exposure / entry_mid
                    if entry_mid > 0
                    else Decimal("0")
                )
                leverage_quantity = (
                    available_equity * maximum_leverage / entry_mid
                    if entry_mid > 0
                    else Decimal("0")
                )
                volume_quantity = max(
                    candle.volume * maximum_volume_participation,
                    Decimal("0"),
                )
                fill_quantity = min(
                    plan.sizing.suggested_quantity,
                    gross_exposure_quantity,
                    leverage_quantity,
                    volume_quantity,
                )
                position_limit_reasons: list[str] = []
                if fill_quantity < plan.sizing.suggested_quantity:
                    if gross_exposure_quantity == fill_quantity:
                        position_limit_reasons.append("gross_exposure")
                        gross_exposure_cap_count += 1
                    if leverage_quantity == fill_quantity:
                        position_limit_reasons.append("leverage")
                        leverage_cap_count += 1
                    if volume_quantity == fill_quantity:
                        position_limit_reasons.append("volume_participation")
                        volume_cap_count += 1
                    if {"gross_exposure", "leverage"}.intersection(
                        position_limit_reasons
                    ):
                        notional_cap_count += 1
                entry_slip = _simulated_slippage_fraction(
                    base_slippage_bps=slippage_bps,
                    market_impact_bps=market_impact_bps,
                    quantity=fill_quantity,
                    candle_volume=candle.volume,
                )
                fill = entry_mid * (
                    Decimal("1") + entry_slip
                    if plan.side == Side.LONG
                    else Decimal("1") - entry_slip
                )
                if fill_quantity > 0:
                    position = {
                        "plan": plan,
                        "entry": fill,
                        "time": candle.timestamp,
                        "original_quantity": fill_quantity,
                        "intended_quantity": plan.sizing.suggested_quantity,
                        "position_limit_reason": (
                            "+".join(position_limit_reasons)
                            if position_limit_reasons
                            else "stop_risk"
                        ),
                        "remaining": fill_quantity,
                        "target_index": 0,
                        "current_stop": plan.exit.stop_loss,
                        "favorable_price": fill,
                        "bars_held": 0,
                    }
                pending = None
            elif index >= expires_index:
                pending = None

        if position is None and pending is None:
            history = candles[max(0, index - history_limit + 1) : index + 1]
            signal = history[-1]
            signal_timestamp = candle_close_time(signal, timeframe)
            known_funding = latest_causal_funding_point(
                funding_rates,
                funding_timestamps,
                signal_timestamp,
            )
            known_open_interest, open_interest_change = causal_open_interest_context(
                open_interest,
                open_interest_timestamps,
                signal_timestamp,
            )
            known_positioning = latest_causal_point(
                positioning,
                positioning_timestamps,
                signal_timestamp,
            )
            known_basis = latest_causal_point(
                basis,
                basis_timestamps,
                signal_timestamp,
            )
            known_order_flow, order_flow_imbalance = causal_order_flow_context(
                order_flow,
                order_flow_availability_timestamps,
                signal_timestamp,
            )
            (
                known_liquidation_flow,
                liquidation_long_contracts,
                liquidation_short_contracts,
                liquidation_flow_imbalance,
            ) = causal_liquidation_flow_context(
                liquidation_flow,
                liquidation_flow_timestamps,
                signal_timestamp,
            )
            money_tick = Decimal("0.00000001")
            half_spread = max(signal.close * Decimal("0.0001"), money_tick)
            market = MarketSnapshot(
                symbol=symbol,
                timestamp=signal_timestamp,
                bid=(signal.close - half_spread).quantize(money_tick),
                ask=(signal.close + half_spread).quantize(money_tick),
                last=signal.close,
                source="historical",
                funding_rate=known_funding.rate if known_funding else None,
                funding_time=(known_funding.timestamp if known_funding else None),
                funding_interval="historical_causal",
                open_interest=(
                    known_open_interest.open_interest_amount if known_open_interest else None
                ),
                open_interest_value=(
                    known_open_interest.open_interest_value if known_open_interest else None
                ),
                open_interest_change_24h=open_interest_change,
                open_interest_time=(known_open_interest.timestamp if known_open_interest else None),
                long_short_ratio=(
                    known_positioning.long_short_ratio if known_positioning else None
                ),
                long_account_fraction=(
                    known_positioning.long_account_fraction if known_positioning else None
                ),
                short_account_fraction=(
                    known_positioning.short_account_fraction if known_positioning else None
                ),
                positioning_time=(known_positioning.timestamp if known_positioning else None),
                basis_rate=known_basis.basis_rate if known_basis else None,
                mark_price=known_basis.mark_price if known_basis else None,
                index_price=known_basis.index_price if known_basis else None,
                basis_time=known_basis.timestamp if known_basis else None,
                order_flow_delta=(known_order_flow.delta_base_volume if known_order_flow else None),
                order_flow_imbalance=order_flow_imbalance,
                order_flow_quote_volume=(
                    known_order_flow.quote_volume if known_order_flow else None
                ),
                order_flow_time=(
                    order_flow_availability_timestamps[
                        bisect_right(order_flow_availability_timestamps, signal_timestamp) - 1
                    ]
                    if known_order_flow
                    else None
                ),
                liquidation_flow_imbalance=liquidation_flow_imbalance,
                liquidation_long_contracts=liquidation_long_contracts,
                liquidation_short_contracts=liquidation_short_contracts,
                liquidation_flow_time=(
                    known_liquidation_flow.timestamp if known_liquidation_flow else None
                ),
                liquidation_source_market_type=(
                    known_liquidation_flow.source_market_type if known_liquidation_flow else None
                ),
                liquidation_source_instrument_id=(
                    known_liquidation_flow.source_instrument_id if known_liquidation_flow else None
                ),
                liquidation_evidence_role=(
                    known_liquidation_flow.evidence_role if known_liquidation_flow else None
                ),
            )
            try:
                if higher_timeframe == timeframe:
                    confirmation = None
                else:
                    confirmation_end = bisect_right(
                        confirmation_close_times,
                        market.timestamp,
                    )
                    confirmation = confirmation_series[
                        max(
                            0,
                            confirmation_end - confirmation_history_limit,
                        ) : confirmation_end
                    ]
                generation_kwargs = dict(
                    candles=history,
                    snapshot=market,
                    equity=equity,
                    risk_fraction=risk_fraction,
                    exchange=exchange,
                    timeframe=timeframe,
                    minimum_reward_risk=minimum_reward_risk,
                    allow_short=allow_short,
                    confirmation_candles=confirmation,
                    require_confirmation=require_confirmation,
                    strategy_spec=strategy_spec,
                )
                if causal_priors:
                    assessment = score_market_state(history)
                    if (
                        causal_policy_abstain_on_ambiguous_regime and assessment.ambiguous
                    ) or assessment.confidence < causal_policy_minimum_regime_confidence:
                        causal_policy_abstentions += 1
                        plan = None
                    else:
                        available_data = {"ohlcv"}
                        if known_funding is not None:
                            available_data.add("funding_rate")
                        if known_open_interest is not None and open_interest_change is not None:
                            available_data.add("open_interest")
                        if known_positioning is not None:
                            available_data.add("positioning")
                        if known_basis is not None:
                            available_data.add("basis")
                        if known_order_flow is not None and order_flow_imbalance is not None:
                            available_data.add("order_flow")
                        if (
                            known_liquidation_flow is not None
                            and liquidation_flow_imbalance is not None
                        ):
                            available_data.add("liquidation_flow")
                        ranked_priors = rank_causal_strategy_priors(
                            causal_priors,
                            assessment,
                            available_data,
                        )[:causal_policy_max_candidates_per_bar]
                        plan = None
                        actionable_policy_plans = []
                        for prior, policy_score, policy_reasons in ranked_priors:
                            prior_kwargs = dict(generation_kwargs)
                            prior_kwargs["strategy_spec"] = prior.spec
                            try:
                                selected_plan = strategy_factory.generate(
                                    prior.family,
                                    **prior_kwargs,
                                )
                            except NoTradeSignal:
                                continue
                            if selected_plan.confidence < causal_policy_minimum_signal_confidence:
                                continue
                            actionable_policy_plans.append(
                                (
                                    prior,
                                    policy_score,
                                    policy_reasons,
                                    selected_plan,
                                )
                            )
                        side_counts = {
                            side: sum(
                                selected_plan.side == side
                                for _, _, _, selected_plan in actionable_policy_plans
                            )
                            for side in {item[3].side for item in actionable_policy_plans}
                        }
                        admitted_plan = next(
                            (
                                item
                                for item in actionable_policy_plans
                                if side_counts[item[3].side]
                                >= causal_policy_minimum_directional_consensus
                            ),
                            None,
                        )
                        if admitted_plan is not None:
                            prior, policy_score, policy_reasons, selected_plan = admitted_plan
                            plan = selected_plan.model_copy(
                                update={
                                    "algorithm": f"library:{prior.id}",
                                    "reasons": [
                                        f"causal policy v{CAUSAL_LIBRARY_POLICY_VERSION} "
                                        f"selected {prior.id} at score {policy_score:f}",
                                        "directional consensus "
                                        f"{side_counts[selected_plan.side]}/"
                                        f"{len(actionable_policy_plans)}",
                                        *policy_reasons,
                                        *selected_plan.reasons,
                                    ],
                                }
                            )
                            causal_policy_selection_counts[prior.id] = (
                                causal_policy_selection_counts.get(prior.id, 0) + 1
                            )
                        if plan is None:
                            causal_policy_abstentions += 1
                elif automatic_selection:
                    generation_kwargs.pop("strategy_spec")
                    plan = strategy_factory.generate_automatic(**generation_kwargs)
                else:
                    if require_regime_family_fit:
                        current_fit = strategy_family_fit(
                            score_market_state(history),
                            strategy_kind,
                        )[0]
                        if not current_fit:
                            plan = None
                        else:
                            plan = strategy_factory.generate(
                                strategy_kind,
                                **generation_kwargs,
                            )
                    else:
                        plan = strategy_factory.generate(
                            strategy_kind,
                            **generation_kwargs,
                        )
            except NoTradeSignal:
                plan = None
            if plan is not None and side_filter is not None and plan.side != side_filter:
                plan = None
            if plan is not None:
                pending = (plan, index + 8)
        marked = _marked_equity(equity, position, candle.close, fee_rate)
        peak, max_drawdown = _drawdown_state(marked, peak, max_drawdown)
        curve.append(marked)

    if position is not None:
        plan = position["plan"]
        last = candles[-1]
        raw_exit = last.close
        close_quantity = position["remaining"]
        exit_slip = _simulated_slippage_fraction(
            base_slippage_bps=slippage_bps,
            market_impact_bps=market_impact_bps,
            quantity=close_quantity,
            candle_volume=last.volume,
        )
        exit_price = raw_exit * (
            Decimal("1") - exit_slip if plan.side == Side.LONG else Decimal("1") + exit_slip
        )
        direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
        gross = (exit_price - position["entry"]) * close_quantity * direction
        fees = (position["entry"] + exit_price) * close_quantity * fee_rate
        raw_entry = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
        slippage_cost = (
            abs(position["entry"] - raw_entry) + abs(exit_price - raw_exit)
        ) * close_quantity
        funding_cost = _funding_cost(
            funding_rates,
            entry_time=position["time"],
            exit_time=last.timestamp,
            side=plan.side,
            quantity=close_quantity,
            fallback_price=position["entry"],
        )
        pnl = (gross - fees - funding_cost).quantize(Decimal("0.01"))
        equity += pnl
        trades.append(
            BacktestTrade(
                entry_time=position["time"].isoformat(),
                exit_time=last.timestamp.isoformat(),
                side=plan.side,
                algorithm=str(plan.algorithm or "unknown"),
                signal_regime=str(plan.regime),
                entry_price=position["entry"].quantize(Decimal("0.01")),
                exit_price=exit_price.quantize(Decimal("0.01")),
                quantity=close_quantity,
                intended_quantity=position["intended_quantity"],
                actual_entry_quantity=position["original_quantity"],
                position_limit_reason=position["position_limit_reason"],
                pnl=pnl,
                exit_reason="end_of_data",
                gross_pnl=gross.quantize(Decimal("0.01")),
                fees=fees.quantize(Decimal("0.01")),
                slippage_cost=slippage_cost.quantize(Decimal("0.01")),
                funding_cost=funding_cost,
                initial_risk_amount=(
                    abs(position["entry"] - plan.exit.stop_loss) * close_quantity
                ).quantize(Decimal("0.01")),
            )
        )
        terminal_position_closed = True
        peak, max_drawdown = _drawdown_state(equity, peak, max_drawdown)
        curve[-1] = equity

    result = _summarize(
        initial_equity,
        equity,
        max_drawdown,
        trades,
        curve,
        periods_per_year(timeframe),
        len(candles),
        exposure_bars,
        fee_bps,
        slippage_bps,
        len(funding_rates),
        terminal_position_closed,
    )
    result.recent_window = _recent_window_summary(result, candles)
    result.temporal_stability = _temporal_stability_report(result, candles)
    result.cost_model["direction_filter"] = side_filter.value if side_filter is not None else "both"
    result.cost_model["regime_family_fit"] = (
        "causal_library_policy" if causal_priors else require_regime_family_fit
    )
    result.cost_model["higher_timeframe_family_fit"] = "contextual_only"
    result.cost_model["confirmation_history_mode"] = "precomputed_causal_closed_bars"
    result.cost_model["confirmation_history_limit_bars"] = str(confirmation_history_limit)
    result.cost_model["signal_history_limit_bars"] = str(history_limit)
    result.cost_model["trailing_activation"] = (
        str(validated_spec.parameters.trailing_activation)
        if validated_spec is not None and hasattr(validated_spec.parameters, "trailing_activation")
        else "not_applicable"
        if validated_spec is not None
        else "per_candidate"
    )
    result.cost_model["maximum_holding_bars"] = (
        str(validated_spec.parameters.maximum_holding_bars)
        if validated_spec is not None and validated_spec.parameters.maximum_holding_bars is not None
        else "per_candidate"
        if causal_priors
        else "none"
    )
    result.cost_model["break_even_after_first_target"] = (
        bool(validated_spec.parameters.break_even_after_first_target)
        if validated_spec is not None
        else "per_candidate"
    )
    result.cost_model["execution_latency_bars"] = "1"
    result.cost_model["open_interest_points"] = str(len(open_interest))
    result.cost_model["open_interest_note"] = (
        "historical open interest used causally"
        if open_interest
        else "historical open interest unavailable; current values were not backfilled"
    )
    result.cost_model["positioning_points"] = str(len(positioning))
    result.cost_model["basis_points"] = str(len(basis))
    result.cost_model["order_flow_points"] = str(len(order_flow))
    result.cost_model["liquidation_flow_points"] = str(len(liquidation_flow))
    result.cost_model["liquidation_flow_role"] = (
        "coin_m_cross_contract_context_only" if liquidation_flow else "unavailable"
    )
    result.cost_model["derivatives_context_mode"] = "latest-known-causal-only"
    result.cost_model["order_flow_availability"] = "bar-close-not-bar-open"
    result.cost_model["maximum_volume_participation"] = str(maximum_volume_participation)
    result.cost_model["market_impact_bps_at_full_participation"] = str(market_impact_bps)
    result.cost_model["maximum_gross_exposure"] = str(maximum_gross_exposure)
    result.cost_model["maximum_leverage"] = str(maximum_leverage)
    result.cost_model["notional_cap_count"] = str(notional_cap_count)
    result.cost_model["gross_exposure_cap_count"] = str(gross_exposure_cap_count)
    result.cost_model["leverage_cap_count"] = str(leverage_cap_count)
    result.cost_model["volume_cap_count"] = str(volume_cap_count)
    total_initial_risk = sum(
        (episode.initial_risk_amount for episode in result.trade_episodes),
        Decimal("0"),
    )
    total_execution_cost = result.total_fees + result.total_slippage + result.total_funding
    result.cost_model["round_trip_cost_r"] = str(
        (total_execution_cost / total_initial_risk).quantize(Decimal("0.0001"))
        if total_initial_risk > 0
        else Decimal("0")
    )
    result.integrity_checks["volume_participation_capped"] = True
    result.integrity_checks["notional_exposure_capped"] = True
    result.integrity_checks["leverage_capped"] = True
    result.integrity_checks["nonlinear_market_impact_included"] = True
    result.integrity_checks["historical_open_interest_not_backfilled_from_current"] = True
    result.integrity_checks["historical_positioning_join_is_causal"] = True
    result.integrity_checks["historical_basis_join_is_causal"] = True
    result.integrity_checks["historical_order_flow_join_is_causal"] = True
    result.integrity_checks["order_flow_unavailable_until_bar_close"] = True
    result.integrity_checks["historical_liquidation_flow_join_is_causal"] = True
    result.integrity_checks["timestamp_availability_gate"] = True
    result.integrity_checks["coin_m_liquidation_not_exact_usdt_contract"] = True
    if causal_priors:
        result.strategy_fingerprint = policy_fingerprint
        result.cost_model["causal_library_policy_version"] = str(CAUSAL_LIBRARY_POLICY_VERSION)
        result.cost_model["causal_library_policy_fingerprint"] = str(policy_fingerprint)
        result.cost_model["causal_library_policy_prior_count"] = str(len(causal_priors))
        result.cost_model["causal_library_policy_max_candidates_per_bar"] = str(
            causal_policy_max_candidates_per_bar
        )
        result.cost_model["causal_library_policy_abstain_on_ambiguous_regime"] = (
            causal_policy_abstain_on_ambiguous_regime
        )
        result.cost_model["causal_library_policy_minimum_regime_confidence"] = str(
            causal_policy_minimum_regime_confidence
        )
        result.cost_model["causal_library_policy_minimum_signal_confidence"] = str(
            causal_policy_minimum_signal_confidence
        )
        result.cost_model["causal_library_policy_minimum_directional_consensus"] = str(
            causal_policy_minimum_directional_consensus
        )
        result.cost_model["causal_library_policy_selection_counts"] = json.dumps(
            causal_policy_selection_counts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result.cost_model["causal_library_policy_abstentions"] = str(causal_policy_abstentions)
        result.integrity_checks["causal_library_policy_pre_registered"] = True
        result.integrity_checks["causal_library_policy_return_inputs_forbidden"] = True
        result.integrity_checks["causal_library_policy_latest_known_features_only"] = True
    if not compute_stress:
        return result
    policy = validation or ResearchValidationPreferences()
    stressed = run_walk_forward_backtest(
        candles,
        initial_equity=initial_equity,
        fee_bps=fee_bps * policy.stress_fee_multiplier,
        slippage_bps=slippage_bps * policy.stress_slippage_multiplier,
        strategy_kind=strategy_kind,
        symbol=symbol,
        timeframe=timeframe,
        exchange=exchange,
        risk_fraction=risk_fraction,
        minimum_reward_risk=minimum_reward_risk,
        allow_short=allow_short,
        require_confirmation=require_confirmation,
        validation=policy,
        strategy_spec=strategy_spec,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        automatic_selection=automatic_selection,
        causal_strategy_priors=causal_priors,
        causal_policy_max_candidates_per_bar=causal_policy_max_candidates_per_bar,
        causal_policy_abstain_on_ambiguous_regime=(causal_policy_abstain_on_ambiguous_regime),
        causal_policy_minimum_regime_confidence=(causal_policy_minimum_regime_confidence),
        causal_policy_minimum_signal_confidence=(causal_policy_minimum_signal_confidence),
        causal_policy_minimum_directional_consensus=(causal_policy_minimum_directional_consensus),
        side_filter=side_filter,
        require_regime_family_fit=require_regime_family_fit,
        research_trial_count=research_trial_count,
        maximum_volume_participation=maximum_volume_participation,
        market_impact_bps=market_impact_bps * policy.stress_slippage_multiplier,
        maximum_gross_exposure=maximum_gross_exposure,
        maximum_leverage=maximum_leverage,
        compute_stress=False,
    )
    return validate_research_result(
        result,
        stressed,
        policy,
        research_trial_count=research_trial_count,
    )


class CanonicalIRExactTrade(BaseModel):
    side: Side
    entry_index: int
    exit_index: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    initial_risk_amount: Decimal
    fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    exit_reason: str
    partial_fill: bool = False


class CanonicalIRExactReplay(BaseModel):
    engine_version: str = "canonical-ir-exact-event-v1"
    ir_hash: str
    trades: list[CanonicalIRExactTrade]
    execution_latency_bars: int
    fee_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    maximum_volume_participation: Decimal

    @property
    def net_pnls(self) -> list[Decimal]:
        return [trade.net_pnl for trade in self.trades]

    @property
    def initial_risks(self) -> list[Decimal]:
        return [trade.initial_risk_amount for trade in self.trades]


def run_canonical_ir_exact_replay(
    ir,
    candles: list[Candle],
    *,
    feature_overrides: dict | None = None,
    initial_equity: Decimal = Decimal("100000"),
    fee_bps: Decimal = Decimal("4"),
    spread_bps: Decimal = Decimal("2"),
    slippage_bps: Decimal = Decimal("2"),
    execution_latency_bars: int = 1,
    maximum_volume_participation: Decimal = Decimal("0.01"),
) -> CanonicalIRExactReplay:
    """Authoritative event replay adapter for CanonicalStrategyIR intents.

    Strategy semantics come from the shared IR backend. This layer applies
    next-bar latency, adverse spread/slippage, volume participation partial
    fills, fixed pre-entry stop risk, fees and event-ordered exits.
    """
    import numpy as np

    from .strategy_backends import ExactIntentBackend

    if len(candles) < 3:
        raise ValueError("exact IR replay requires at least three candles")
    if execution_latency_bars < 1:
        raise ValueError("exact IR replay requires at least one bar of latency")
    features = {
        "open": np.asarray([float(item.open) for item in candles]),
        "high": np.asarray([float(item.high) for item in candles]),
        "low": np.asarray([float(item.low) for item in candles]),
        "close": np.asarray([float(item.close) for item in candles]),
        "volume": np.asarray([float(item.volume) for item in candles]),
    }
    for name, values in (feature_overrides or {}).items():
        features[name] = np.asarray(values, dtype=np.float64)
    intents = ExactIntentBackend().evaluate(ir, features)
    fee_fraction = fee_bps / Decimal("10000")
    half_spread = spread_bps / Decimal("20000")
    slip_fraction = slippage_bps / Decimal("10000")
    trades: list[CanonicalIRExactTrade] = []
    position: dict | None = None

    def adverse_price(raw: Decimal, side: int, *, entering: bool) -> Decimal:
        direction = side if entering else -side
        return raw * (Decimal("1") + Decimal(direction) * (half_spread + slip_fraction))

    def close_position(index: int, raw_price: Decimal, reason: str) -> None:
        nonlocal position
        if position is None:
            return
        side = int(position["side"])
        exit_price = adverse_price(raw_price, side, entering=False)
        quantity = position["quantity"]
        entry_price = position["entry_price"]
        gross = Decimal(side) * (exit_price - entry_price) * quantity
        exit_fee = exit_price * quantity * fee_fraction
        total_fees = position["entry_fee"] + exit_fee
        spread_cost = (position["raw_entry"] + raw_price) * quantity * half_spread
        slippage_cost = (position["raw_entry"] + raw_price) * quantity * slip_fraction
        trades.append(
            CanonicalIRExactTrade(
                side=Side.LONG if side > 0 else Side.SHORT,
                entry_index=position["entry_index"],
                exit_index=index,
                entry_price=entry_price,
                exit_price=exit_price,
                quantity=quantity,
                initial_risk_amount=position["initial_risk_amount"],
                fees=total_fees,
                spread_cost=spread_cost,
                slippage_cost=slippage_cost,
                net_pnl=gross - total_fees,
                exit_reason=reason,
                partial_fill=position["partial_fill"],
            )
        )
        position = None

    for signal_index in range(intents.warmup, len(candles) - execution_latency_bars):
        candle = candles[signal_index]
        if position is not None:
            side = int(position["side"])
            stop = position["stop_price"]
            stop_hit = candle.low <= stop if side > 0 else candle.high >= stop
            if stop_hit:
                close_position(signal_index, stop, "pre_committed_stop")
            elif bool(intents.exit_intent[signal_index]):
                close_position(signal_index, candle.close, "ir_exit_intent")
        direction = int(intents.entry_intent[signal_index])
        if position is not None or direction == 0:
            continue
        fill_index = signal_index + execution_latency_bars
        fill_candle = candles[fill_index]
        stop_distance = Decimal(str(intents.stop_distance[signal_index]))
        if not stop_distance.is_finite() or stop_distance <= 0:
            continue
        raw_entry = fill_candle.open
        entry_price = adverse_price(raw_entry, direction, entering=True)
        intended_quantity = initial_equity * Decimal(ir.risk_budget_fraction) / stop_distance
        volume_cap = fill_candle.volume * maximum_volume_participation
        quantity = min(intended_quantity, volume_cap)
        if quantity <= 0:
            continue
        position = {
            "side": direction,
            "entry_index": fill_index,
            "raw_entry": raw_entry,
            "entry_price": entry_price,
            "quantity": quantity,
            "entry_fee": entry_price * quantity * fee_fraction,
            "initial_risk_amount": stop_distance * quantity,
            "stop_price": entry_price - Decimal(direction) * stop_distance,
            "partial_fill": quantity < intended_quantity,
        }
    if position is not None:
        close_position(len(candles) - 1, candles[-1].close, "end_of_snapshot")
    return CanonicalIRExactReplay(
        ir_hash=ir.fingerprint,
        trades=trades,
        execution_latency_bars=execution_latency_bars,
        fee_bps=fee_bps,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        maximum_volume_participation=maximum_volume_participation,
    )
