from __future__ import annotations

import json
import math
from bisect import bisect_right
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from statistics import mean, pstdev
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .domain import Candle, FundingRatePoint
from .statistical_validation import StatisticalValidationReport, validate_statistical_evidence
from .timeframes import periods_per_year

OKX_FUNDING_ARBITRAGE = (
    "https://www.okx.com/en-eu/learn/funding-rate-arbitrage-crypto-derivatives"
)
CARRY_BACKTEST_ENGINE_VERSION = 1


class CarryStrategySpec(BaseModel):
    version: int = 1
    model: Literal["spot_perpetual_cash_and_carry"] = "spot_perpetual_cash_and_carry"
    entry_basis_rate: Decimal = Field(default=Decimal("0.001"), gt=0, le=Decimal("0.10"))
    exit_basis_rate: Decimal = Field(default=Decimal("0.0002"), ge=0, le=Decimal("0.05"))
    stop_basis_rate: Decimal = Field(default=Decimal("0.015"), gt=0, le=Decimal("0.25"))
    minimum_observed_funding_rate: Decimal = Field(
        default=Decimal("0.00005"),
        ge=Decimal("-0.01"),
        le=Decimal("0.01"),
    )
    funding_exit_rate: Decimal = Field(
        default=Decimal("-0.00005"),
        ge=Decimal("-0.01"),
        le=Decimal("0.01"),
    )
    confirmation_bars: int = Field(default=4, ge=1, le=96)
    minimum_holding_bars: int = Field(default=8, ge=1, le=1000)
    maximum_holding_bars: int = Field(default=2880, ge=24, le=50000)
    gross_leverage: Decimal = Field(default=Decimal("1"), gt=0, le=Decimal("2"))
    annual_capital_cost_rate: Decimal = Field(
        default=Decimal("0.05"),
        ge=0,
        le=Decimal("0.50"),
    )
    source_url: str = OKX_FUNDING_ARBITRAGE
    hypothesis: str = (
        "When a perpetual trades above spot and the latest already-published funding rate "
        "is positive, buy spot and short the same-exchange perpetual with equal entry "
        "notional. Test whether basis convergence plus realized funding exceeds dual-leg "
        "fees, slippage and capital cost; this is a falsifiable prior, not arbitrage proof."
    )

    @model_validator(mode="after")
    def validate_contract(self):
        if self.exit_basis_rate >= self.entry_basis_rate:
            raise ValueError("carry exit basis must be below entry basis")
        if self.stop_basis_rate <= self.entry_basis_rate:
            raise ValueError("carry stop basis must exceed entry basis")
        if self.minimum_holding_bars >= self.maximum_holding_bars:
            raise ValueError("carry minimum holding period must be below maximum")
        if self.funding_exit_rate >= self.minimum_observed_funding_rate:
            raise ValueError("carry funding exit rate must be below entry funding rate")
        return self

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()[:16]


CARRY_STRATEGY_LIBRARY: tuple[CarryStrategySpec, ...] = (
    CarryStrategySpec(),
    CarryStrategySpec(
        entry_basis_rate=Decimal("0.0025"),
        exit_basis_rate=Decimal("0.0005"),
        stop_basis_rate=Decimal("0.02"),
        minimum_observed_funding_rate=Decimal("0.0001"),
        confirmation_bars=8,
        maximum_holding_bars=5760,
        hypothesis=(
            "A conservative same-exchange cash-and-carry prior requiring at least 25bp "
            "perpetual premium and a positive published funding observation before the "
            "next-bar dual-leg entry."
        ),
    ),
)


class CarryTrade(BaseModel):
    entry_time: datetime
    exit_time: datetime
    symbol: str
    entry_basis_rate: Decimal
    exit_basis_rate: Decimal
    observed_entry_funding_rate: Decimal
    spot_notional: Decimal
    perpetual_notional: Decimal
    gross_pnl: Decimal
    fees: Decimal
    slippage_cost: Decimal
    funding_cashflow: Decimal
    capital_cost: Decimal
    net_pnl: Decimal
    return_percent: Decimal
    holding_bars: int
    exit_reason: str


class CarryBacktestReport(BaseModel):
    engine_version: int = CARRY_BACKTEST_ENGINE_VERSION
    id: str = Field(default_factory=lambda: f"carrybt_{uuid4().hex[:16]}")
    strategy_spec: CarryStrategySpec
    strategy_fingerprint: str
    spot_dataset_id: str
    perpetual_dataset_id: str
    spot_data_fingerprint: str
    perpetual_data_fingerprint: str
    exchange: str
    symbol: str
    timeframe: str
    aligned_bars: int
    formation_bars: int
    evaluation_bars: int
    evaluation_start: datetime
    data_end: datetime
    initial_equity: Decimal
    final_equity: Decimal
    return_percent: Decimal
    max_drawdown_percent: Decimal
    total_trades: int
    win_rate_percent: Decimal
    payoff_ratio: Decimal
    profit_factor: Decimal
    total_fees: Decimal
    total_slippage: Decimal
    total_funding_cashflow: Decimal
    total_capital_cost: Decimal
    funding_coverage_percent: Decimal
    terminal_position_closed: bool
    statistical_validation: StatisticalValidationReport
    research_gate_passed: bool = False
    robustness_passed: bool = False
    execution_eligible: Literal[False] = False
    validation_reasons: list[str]
    integrity_checks: dict[str, bool | int | str]
    trades: list[CarryTrade]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class _FundingLedger:
    def __init__(self, points: list[FundingRatePoint]):
        ordered = sorted(points, key=lambda item: item.timestamp)
        self.timestamps = [item.timestamp for item in ordered]
        self.rates = [item.rate for item in ordered]
        self.cumulative = [Decimal("0")]
        total = Decimal("0")
        for rate in self.rates:
            total += rate
            self.cumulative.append(total)

    def latest_at(self, timestamp: datetime) -> Decimal | None:
        index = bisect_right(self.timestamps, timestamp) - 1
        return self.rates[index] if index >= 0 else None

    def cost_between(
        self,
        entry_time: datetime,
        exit_time: datetime,
        *,
        side: int,
        notional: Decimal,
    ) -> Decimal:
        left = bisect_right(self.timestamps, entry_time)
        right = bisect_right(self.timestamps, exit_time)
        return (self.cumulative[right] - self.cumulative[left]) * notional * Decimal(side)


def _aligned_candles(
    spot: list[Candle],
    perpetual: list[Candle],
) -> list[tuple[Candle, Candle]]:
    perpetual_by_time = {item.timestamp: item for item in perpetual}
    return [
        (item, perpetual_by_time[item.timestamp])
        for item in spot
        if item.timestamp in perpetual_by_time
    ]


def _basis_rate(spot: Candle, perpetual: Candle) -> Decimal:
    return perpetual.close / spot.close - Decimal("1")


def _funding_coverage_percent(
    points: list[FundingRatePoint],
    *,
    start: datetime,
    end: datetime,
) -> Decimal:
    if len(points) < 2 or end <= start:
        return Decimal("0")
    ordered = sorted(points, key=lambda item: item.timestamp)
    covered_start = max(ordered[0].timestamp, start)
    covered_end = min(ordered[-1].timestamp, end)
    if covered_end <= covered_start:
        return Decimal("0")
    return min(
        Decimal(str((covered_end - covered_start).total_seconds()))
        / Decimal(str((end - start).total_seconds()))
        * Decimal("100"),
        Decimal("100"),
    )


def run_carry_backtest(
    *,
    spot_dataset,
    spot_candles: list[Candle],
    perpetual_dataset,
    perpetual_candles: list[Candle],
    funding_rates: list[FundingRatePoint],
    strategy_spec: CarryStrategySpec | dict | None = None,
    initial_equity: Decimal = Decimal("100000"),
    spot_fee_bps: Decimal = Decimal("8"),
    perpetual_fee_bps: Decimal = Decimal("6"),
    slippage_bps: Decimal = Decimal("2"),
    capital_cost_multiplier: Decimal = Decimal("1"),
    research_trial_count: int = 1,
    evaluation_start_index: int | None = None,
) -> CarryBacktestReport:
    spec = (
        strategy_spec
        if isinstance(strategy_spec, CarryStrategySpec)
        else CarryStrategySpec.model_validate(strategy_spec or {})
    )
    if spot_dataset.exchange != perpetual_dataset.exchange:
        raise ValueError("carry legs must use the same exchange")
    if spot_dataset.symbol != perpetual_dataset.symbol:
        raise ValueError("carry legs must use the same canonical symbol")
    if spot_dataset.timeframe != perpetual_dataset.timeframe:
        raise ValueError("carry legs must use the same timeframe")
    if spot_dataset.market_type != "spot":
        raise ValueError("carry spot leg must use a spot dataset")
    if perpetual_dataset.market_type != "swap":
        raise ValueError("carry perpetual leg must use a swap dataset")
    if min(initial_equity, capital_cost_multiplier) <= 0:
        raise ValueError("carry equity and capital-cost multiplier must be positive")
    if min(spot_fee_bps, perpetual_fee_bps, slippage_bps) < 0:
        raise ValueError("carry costs cannot be negative")
    aligned = _aligned_candles(spot_candles, perpetual_candles)
    minimum_history = spec.confirmation_bars + 2
    if len(aligned) < minimum_history:
        raise ValueError("carry research has insufficient synchronized closed bars")
    evaluation_index = max(
        spec.confirmation_bars,
        evaluation_start_index or spec.confirmation_bars,
    )
    if evaluation_index >= len(aligned) - 1:
        raise ValueError("carry research has insufficient out-of-sample trading bars")

    ledger = _FundingLedger(funding_rates)
    spot_fee_rate = spot_fee_bps / Decimal("10000")
    perpetual_fee_rate = perpetual_fee_bps / Decimal("10000")
    slip_rate = slippage_bps / Decimal("10000")
    seconds_per_year = Decimal(str(365.25 * 24 * 60 * 60))
    capital_apr = spec.annual_capital_cost_rate * capital_cost_multiplier
    equity = initial_equity
    peak = initial_equity
    max_drawdown = Decimal("0")
    position: dict | None = None
    pending_entry: tuple[Decimal, Decimal] | None = None
    pending_exit: tuple[str, Decimal] | None = None
    trades: list[CarryTrade] = []
    terminal_closed = False
    margin_breach = False
    marked_returns: list[float] = []
    previous_marked = initial_equity
    confirmation_count = 0

    def capital_cost(entry_time: datetime, exit_time: datetime, gross_notional: Decimal) -> Decimal:
        elapsed = Decimal(str(max((exit_time - entry_time).total_seconds(), 0)))
        return gross_notional * capital_apr * elapsed / seconds_per_year

    def close_position(index: int, reason: str, exit_basis: Decimal) -> None:
        nonlocal equity, position
        assert position is not None
        spot, perpetual = aligned[index]
        exit_spot = spot.open * (Decimal("1") - slip_rate)
        exit_perpetual = perpetual.open * (Decimal("1") + slip_rate)
        gross_spot = (exit_spot - position["entry_spot"]) * position["spot_quantity"]
        gross_perpetual = (
            position["entry_perpetual"] - exit_perpetual
        ) * position["perpetual_quantity"]
        fees = (
            (position["entry_spot"] + exit_spot)
            * position["spot_quantity"]
            * spot_fee_rate
            + (position["entry_perpetual"] + exit_perpetual)
            * position["perpetual_quantity"]
            * perpetual_fee_rate
        )
        slippage = (
            abs(position["entry_spot"] - position["raw_entry_spot"])
            * position["spot_quantity"]
            + abs(exit_spot - spot.open) * position["spot_quantity"]
            + abs(position["entry_perpetual"] - position["raw_entry_perpetual"])
            * position["perpetual_quantity"]
            + abs(exit_perpetual - perpetual.open) * position["perpetual_quantity"]
        )
        funding_cost = ledger.cost_between(
            position["entry_time"],
            spot.timestamp,
            side=-1,
            notional=position["perpetual_notional"],
        )
        financing = capital_cost(
            position["entry_time"],
            spot.timestamp,
            position["gross_notional"],
        )
        gross = gross_spot + gross_perpetual
        net = gross - fees - funding_cost - financing
        equity_before = equity
        equity += net
        trades.append(
            CarryTrade(
                entry_time=position["entry_time"],
                exit_time=spot.timestamp,
                symbol=spot_dataset.symbol,
                entry_basis_rate=position["entry_basis"],
                exit_basis_rate=exit_basis,
                observed_entry_funding_rate=position["entry_funding"],
                spot_notional=position["spot_notional"].quantize(Decimal("0.01")),
                perpetual_notional=position["perpetual_notional"].quantize(Decimal("0.01")),
                gross_pnl=gross.quantize(Decimal("0.01")),
                fees=fees.quantize(Decimal("0.01")),
                slippage_cost=slippage.quantize(Decimal("0.01")),
                funding_cashflow=(-funding_cost).quantize(Decimal("0.01")),
                capital_cost=financing.quantize(Decimal("0.01")),
                net_pnl=net.quantize(Decimal("0.01")),
                return_percent=(net / equity_before * Decimal("100")).quantize(
                    Decimal("0.0001")
                ),
                holding_bars=index - position["entry_index"],
                exit_reason=reason,
            )
        )
        position = None

    def marked_equity(index: int) -> Decimal:
        if position is None:
            return equity
        spot, perpetual = aligned[index]
        spot_pnl = (spot.close - position["entry_spot"]) * position["spot_quantity"]
        perpetual_pnl = (
            position["entry_perpetual"] - perpetual.close
        ) * position["perpetual_quantity"]
        entry_fees = (
            position["entry_spot"] * position["spot_quantity"] * spot_fee_rate
            + position["entry_perpetual"]
            * position["perpetual_quantity"]
            * perpetual_fee_rate
        )
        funding_cost = ledger.cost_between(
            position["entry_time"],
            spot.timestamp,
            side=-1,
            notional=position["perpetual_notional"],
        )
        financing = capital_cost(
            position["entry_time"],
            spot.timestamp,
            position["gross_notional"],
        )
        return equity + spot_pnl + perpetual_pnl - entry_fees - funding_cost - financing

    for index in range(evaluation_index, len(aligned)):
        spot, perpetual = aligned[index]
        basis = _basis_rate(spot, perpetual)
        known_funding = ledger.latest_at(spot.timestamp)
        if pending_exit is not None and position is not None:
            close_position(index, pending_exit[0], pending_exit[1])
            pending_exit = None
        if pending_entry is not None and position is None and index < len(aligned) - 1:
            signal_basis, signal_funding = pending_entry
            gross_notional = equity * spec.gross_leverage
            spot_notional = gross_notional / Decimal("2")
            perpetual_notional = gross_notional - spot_notional
            raw_entry_spot = spot.open
            raw_entry_perpetual = perpetual.open
            entry_spot = raw_entry_spot * (Decimal("1") + slip_rate)
            entry_perpetual = raw_entry_perpetual * (Decimal("1") - slip_rate)
            position = {
                "entry_time": spot.timestamp,
                "entry_index": index,
                "entry_basis": signal_basis,
                "entry_funding": signal_funding,
                "raw_entry_spot": raw_entry_spot,
                "raw_entry_perpetual": raw_entry_perpetual,
                "entry_spot": entry_spot,
                "entry_perpetual": entry_perpetual,
                "spot_notional": spot_notional,
                "perpetual_notional": perpetual_notional,
                "gross_notional": gross_notional,
                "spot_quantity": spot_notional / raw_entry_spot,
                "perpetual_quantity": perpetual_notional / raw_entry_perpetual,
            }
            pending_entry = None
        elif pending_entry is not None and index == len(aligned) - 1:
            pending_entry = None

        entry_condition = bool(
            basis >= spec.entry_basis_rate
            and known_funding is not None
            and known_funding >= spec.minimum_observed_funding_rate
        )
        confirmation_count = confirmation_count + 1 if entry_condition else 0
        if position is None and pending_entry is None:
            if confirmation_count >= spec.confirmation_bars and known_funding is not None:
                pending_entry = (basis, known_funding)
                confirmation_count = 0
        elif position is not None:
            holding = index - position["entry_index"]
            if holding >= spec.minimum_holding_bars:
                if basis <= spec.exit_basis_rate:
                    pending_exit = ("basis_convergence", basis)
                elif known_funding is not None and known_funding <= spec.funding_exit_rate:
                    pending_exit = ("funding_reversal", basis)
            if pending_exit is None and basis >= spec.stop_basis_rate:
                pending_exit = ("basis_stop", basis)
            if pending_exit is None and holding >= spec.maximum_holding_bars:
                pending_exit = ("maximum_holding", basis)

        current_marked = marked_equity(index)
        if current_marked <= 0 and position is not None:
            margin_breach = True
            pending_exit = ("margin_breach", basis)
        if previous_marked > 0:
            marked_returns.append(float(current_marked / previous_marked - Decimal("1")))
        previous_marked = current_marked
        peak = max(peak, current_marked)
        if peak > 0:
            max_drawdown = max(
                max_drawdown,
                (peak - current_marked) / peak * Decimal("100"),
            )

    if position is not None:
        last_index = len(aligned) - 1
        last_spot, last_perpetual = aligned[last_index]
        aligned[last_index] = (
            last_spot.model_copy(update={"open": last_spot.close}),
            last_perpetual.model_copy(update={"open": last_perpetual.close}),
        )
        close_position(last_index, "end_of_data", _basis_rate(last_spot, last_perpetual))
        terminal_closed = True

    normalized_pnls = [trade.return_percent / Decimal("100") for trade in trades]
    wins = [value for value in normalized_pnls if value > 0]
    losses = [-value for value in normalized_pnls if value < 0]
    win_rate = (
        Decimal(len(wins)) / Decimal(len(normalized_pnls)) * Decimal("100")
        if normalized_pnls
        else Decimal("0")
    )
    payoff = (
        (sum(wins, Decimal("0")) / len(wins))
        / (sum(losses, Decimal("0")) / len(losses))
        if wins and losses
        else Decimal("0")
    )
    profit_factor = (
        sum(wins, Decimal("0")) / sum(losses, Decimal("0"))
        if losses
        else Decimal("0")
    )
    sharpe = (
        Decimal(
            str(
                mean(marked_returns)
                / pstdev(marked_returns)
                * math.sqrt(float(periods_per_year(spot_dataset.timeframe)))
            )
        )
        if len(marked_returns) > 1 and pstdev(marked_returns)
        else Decimal("0")
    )
    statistical = validate_statistical_evidence(
        annualized_sharpe=sharpe,
        annualization_periods=periods_per_year(spot_dataset.timeframe),
        observation_count=len(marked_returns),
        pnls=normalized_pnls,
        initial_equity=Decimal("1"),
        trial_count=research_trial_count,
    )
    funding_coverage = _funding_coverage_percent(
        funding_rates,
        start=aligned[evaluation_index][0].timestamp,
        end=aligned[-1][0].timestamp,
    )
    reasons: list[str] = []
    if len(trades) < 10:
        reasons.append(f"carry trades {len(trades)} below minimum 10")
    if win_rate < Decimal("50"):
        reasons.append(f"carry win rate {win_rate:.2f}% below 50%")
    if payoff < Decimal("1.30"):
        reasons.append(f"carry payoff ratio {payoff:.2f} below 1.30")
    if profit_factor < Decimal("1.25"):
        reasons.append(f"carry profit factor {profit_factor:.2f} below 1.25")
    if equity <= initial_equity:
        reasons.append("carry return is not positive after all modeled costs")
    if funding_coverage < Decimal("95"):
        reasons.append(f"funding history coverage {funding_coverage:.2f}% below 95%")
    if margin_breach:
        reasons.append("carry marked equity breached zero")
    if not statistical.uncertainty_passed:
        reasons.extend(statistical.reasons)
    research_gate_passed = not reasons
    validation_reasons = list(
        dict.fromkeys(
            [
                *reasons,
                "single development sample cannot prove cross-market or blind robustness",
                "atomic spot-perpetual execution and partial-fill compensation are not verified",
            ]
        )
    )
    return CarryBacktestReport(
        strategy_spec=spec,
        strategy_fingerprint=spec.fingerprint,
        spot_dataset_id=spot_dataset.id,
        perpetual_dataset_id=perpetual_dataset.id,
        spot_data_fingerprint=spot_dataset.data_fingerprint,
        perpetual_data_fingerprint=perpetual_dataset.data_fingerprint,
        exchange=spot_dataset.exchange,
        symbol=spot_dataset.symbol,
        timeframe=spot_dataset.timeframe,
        aligned_bars=len(aligned),
        formation_bars=evaluation_index,
        evaluation_bars=len(aligned) - evaluation_index,
        evaluation_start=aligned[evaluation_index][0].timestamp,
        data_end=aligned[-1][0].timestamp,
        initial_equity=initial_equity,
        final_equity=equity.quantize(Decimal("0.01")),
        return_percent=((equity / initial_equity - Decimal("1")) * Decimal("100")).quantize(
            Decimal("0.01")
        ),
        max_drawdown_percent=max_drawdown.quantize(Decimal("0.01")),
        total_trades=len(trades),
        win_rate_percent=win_rate.quantize(Decimal("0.01")),
        payoff_ratio=payoff.quantize(Decimal("0.01")),
        profit_factor=profit_factor.quantize(Decimal("0.01")),
        total_fees=sum((trade.fees for trade in trades), Decimal("0")),
        total_slippage=sum((trade.slippage_cost for trade in trades), Decimal("0")),
        total_funding_cashflow=sum(
            (trade.funding_cashflow for trade in trades), Decimal("0")
        ),
        total_capital_cost=sum((trade.capital_cost for trade in trades), Decimal("0")),
        funding_coverage_percent=funding_coverage.quantize(Decimal("0.01")),
        terminal_position_closed=terminal_closed,
        statistical_validation=statistical,
        research_gate_passed=research_gate_passed,
        validation_reasons=validation_reasons,
        integrity_checks={
            "closed_bars_only": True,
            "signal_to_fill_latency_bars": 1,
            "synchronized_spot_perpetual_timestamps": True,
            "same_exchange_same_symbol": True,
            "long_spot_short_perpetual_only": True,
            "latest_published_funding_signal_only": True,
            "actual_historical_funding_cashflows": True,
            "dual_leg_fees_and_slippage": True,
            "capital_cost_included": True,
            "strict_formation_trading_separation": True,
            "formation_bars_excluded_from_metrics": evaluation_index,
            "complete_funding_coverage": funding_coverage >= Decimal("95"),
            "mark_to_market_drawdown": True,
            "execution_enabled": False,
        },
        trades=trades,
    )
