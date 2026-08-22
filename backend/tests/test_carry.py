from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.carry import CarryStrategySpec, run_carry_backtest
from cosmos_quant.domain import Candle, FundingRatePoint
from cosmos_quant.research import build_research_dataset


def _carry_fixture(count: int = 1400):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    spot = []
    perpetual = []
    quantum = Decimal("0.00000001")
    for index in range(count):
        timestamp = start + timedelta(hours=index)
        spot_price = Decimal("100") + Decimal(index) * Decimal("0.005")
        phase = index % 100
        if phase < 20 or phase >= 75:
            basis = Decimal("0.0001")
        elif phase < 50:
            basis = Decimal("0.006")
        else:
            basis = Decimal("0.006") * Decimal(75 - phase) / Decimal(25)
        perpetual_price = (spot_price * (Decimal("1") + basis)).quantize(quantum)
        spot.append(
            Candle(
                timestamp=timestamp,
                open=spot_price,
                high=(spot_price * Decimal("1.001")).quantize(quantum),
                low=(spot_price * Decimal("0.999")).quantize(quantum),
                close=spot_price,
                volume=Decimal("1000"),
            )
        )
        perpetual.append(
            Candle(
                timestamp=timestamp,
                open=perpetual_price,
                high=(perpetual_price * Decimal("1.001")).quantize(quantum),
                low=(perpetual_price * Decimal("0.999")).quantize(quantum),
                close=perpetual_price,
                volume=Decimal("1000"),
            )
        )
    funding = [
        FundingRatePoint(
            timestamp=start + timedelta(hours=index),
            rate=Decimal("0.0001"),
        )
        for index in range(0, count, 8)
    ]
    spot_dataset = build_research_dataset(
        spot,
        exchange="test",
        market_type="spot",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    perpetual_dataset = build_research_dataset(
        perpetual,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        funding_rates=funding,
    )
    return spot_dataset, spot, perpetual_dataset, perpetual, funding


def _spec():
    return CarryStrategySpec(
        entry_basis_rate=Decimal("0.001"),
        exit_basis_rate=Decimal("0.0004"),
        stop_basis_rate=Decimal("0.02"),
        minimum_observed_funding_rate=Decimal("0.00005"),
        confirmation_bars=2,
        minimum_holding_bars=2,
        maximum_holding_bars=100,
    )


def test_spot_perpetual_carry_is_causal_costed_and_research_only():
    spot_dataset, spot, perpetual_dataset, perpetual, funding = _carry_fixture()
    report = run_carry_backtest(
        spot_dataset=spot_dataset,
        spot_candles=spot,
        perpetual_dataset=perpetual_dataset,
        perpetual_candles=perpetual,
        funding_rates=funding,
        strategy_spec=_spec(),
        evaluation_start_index=280,
    )

    assert report.total_trades >= 10
    assert report.return_percent > 0
    assert report.total_fees > 0
    assert report.total_slippage > 0
    assert report.total_funding_cashflow > 0
    assert report.total_capital_cost > 0
    assert report.funding_coverage_percent >= Decimal("95")
    assert report.formation_bars == 280
    assert all(trade.entry_time > report.evaluation_start for trade in report.trades)
    assert all(trade.spot_notional > 0 for trade in report.trades)
    assert all(trade.perpetual_notional > 0 for trade in report.trades)
    assert report.integrity_checks["latest_published_funding_signal_only"] is True
    assert report.integrity_checks["strict_formation_trading_separation"] is True
    assert report.execution_eligible is False


def test_carry_cost_stress_reduces_return():
    spot_dataset, spot, perpetual_dataset, perpetual, funding = _carry_fixture()
    kwargs = {
        "spot_dataset": spot_dataset,
        "spot_candles": spot,
        "perpetual_dataset": perpetual_dataset,
        "perpetual_candles": perpetual,
        "funding_rates": funding,
        "strategy_spec": _spec(),
        "evaluation_start_index": 280,
    }
    base = run_carry_backtest(**kwargs)
    stress = run_carry_backtest(
        **kwargs,
        spot_fee_bps=Decimal("16"),
        perpetual_fee_bps=Decimal("12"),
        slippage_bps=Decimal("4"),
        capital_cost_multiplier=Decimal("1.5"),
    )

    assert stress.return_percent < base.return_percent
    assert stress.final_equity < base.final_equity


def test_future_funding_change_cannot_change_earlier_carry_trades():
    spot_dataset, spot, perpetual_dataset, perpetual, funding = _carry_fixture()
    mutation_time = spot[900].timestamp
    changed = [
        point.model_copy(update={"rate": Decimal("-0.005")})
        if point.timestamp >= mutation_time
        else point
        for point in funding
    ]
    baseline = run_carry_backtest(
        spot_dataset=spot_dataset,
        spot_candles=spot,
        perpetual_dataset=perpetual_dataset,
        perpetual_candles=perpetual,
        funding_rates=funding,
        strategy_spec=_spec(),
        evaluation_start_index=280,
    )
    mutated = run_carry_backtest(
        spot_dataset=spot_dataset,
        spot_candles=spot,
        perpetual_dataset=perpetual_dataset,
        perpetual_candles=perpetual,
        funding_rates=changed,
        strategy_spec=_spec(),
        evaluation_start_index=280,
    )

    baseline_before = [trade for trade in baseline.trades if trade.exit_time < mutation_time]
    mutated_before = [trade for trade in mutated.trades if trade.exit_time < mutation_time]
    assert mutated_before == baseline_before


def test_carry_rejects_non_spot_and_swap_pair():
    spot_dataset, spot, perpetual_dataset, perpetual, funding = _carry_fixture(200)
    invalid_spot = spot_dataset.model_copy(update={"market_type": "swap"})

    with pytest.raises(ValueError, match="spot leg"):
        run_carry_backtest(
            spot_dataset=invalid_spot,
            spot_candles=spot,
            perpetual_dataset=perpetual_dataset,
            perpetual_candles=perpetual,
            funding_rates=funding,
        )
