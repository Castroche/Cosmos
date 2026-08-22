import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.demo import demo_candles
from cosmos_quant.domain import FundingRatePoint
from cosmos_quant.relative_value import (
    CROSS_SECTIONAL_MOMENTUM,
    ENGLE_GRANGER_COINTEGRATION,
    PairStrategySpec,
    _causal_pair_signals,
    _funding_cost,
    _FundingLedger,
    _residual_stability_evidence,
    run_pair_backtest,
)
from cosmos_quant.research import build_research_dataset
from cosmos_quant.storage import SqliteStore


def _pair_fixture():
    base = demo_candles(800)
    second = [
        candle.model_copy(
            update={
                "open": candle.open * Decimal("0.05"),
                "high": candle.high * Decimal("0.05"),
                "low": candle.low * Decimal("0.05"),
                "close": candle.close * Decimal("0.05"),
            }
        )
        for candle in base
    ]
    first = []
    for index, candle in enumerate(base):
        factor = Decimal(str(1 + 0.04 * math.sin(index / 12)))
        first.append(
            candle.model_copy(
                update={
                    "open": candle.open * factor,
                    "high": candle.high * factor,
                    "low": candle.low * factor,
                    "close": candle.close * factor,
                }
            )
        )
    dataset_a = build_research_dataset(
        first,
        exchange="test",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    dataset_b = build_research_dataset(
        second,
        exchange="test",
        market_type="swap",
        symbol="ETH/USDT",
        timeframe="1h",
    )
    return dataset_a, first, dataset_b, second


def _residual_fixture(*, integrated: bool = False, evaluation_break: bool = False):
    base = demo_candles(700)
    second = []
    first = []
    residual = 0.0
    pseudo_random_state = 7
    for index, candle in enumerate(base):
        pseudo_random_state = (
            1664525 * pseudo_random_state + 1013904223
        ) % (2**32)
        innovation = (pseudo_random_state / 2**32 - 0.5) * 0.025
        residual = (
            residual + 0.0025 + innovation * 0.05
            if integrated
            else 0.65 * residual + innovation
        )
        shifted_residual = residual + (0.25 if evaluation_break and index >= 300 else 0)
        first_factor = Decimal(str(math.exp(shifted_residual)))
        second_factor = Decimal("0.05")
        first.append(
            candle.model_copy(
                update={
                    "open": candle.open * first_factor,
                    "high": candle.high * first_factor,
                    "low": candle.low * first_factor,
                    "close": candle.close * first_factor,
                }
            )
        )
        second.append(
            candle.model_copy(
                update={
                    "open": candle.open * second_factor,
                    "high": candle.high * second_factor,
                    "low": candle.low * second_factor,
                    "close": candle.close * second_factor,
                }
            )
        )
    return first, second


def test_pair_backtest_uses_synchronized_next_bar_double_leg_execution():
    dataset_a, candles_a, dataset_b, candles_b = _pair_fixture()
    report = run_pair_backtest(
        dataset_a=dataset_a,
        candles_a=candles_a,
        funding_a=[],
        dataset_b=dataset_b,
        candles_b=candles_b,
        funding_b=[],
        strategy_spec=PairStrategySpec(
            formation_window=60,
            entry_zscore=Decimal("1.5"),
            exit_zscore=Decimal("0.3"),
            stop_zscore=Decimal("4"),
            maximum_holding_bars=80,
        ),
    )

    assert report.aligned_bars == 800
    assert report.total_trades > 10
    assert all(trade.entry_time < trade.exit_time for trade in report.trades)
    assert report.total_fees > 0
    assert report.integrity_checks["signal_to_fill_latency_bars"] == 1
    assert report.integrity_checks["equal_dollar_entry_legs"] is True
    assert report.integrity_checks["mark_to_market_drawdown"] is True
    assert report.integrity_checks["bar_return_sharpe"] is True
    assert report.integrity_checks["normalized_trade_statistics"] is True
    assert report.integrity_checks["complete_funding_coverage"] is False
    assert report.integrity_checks["execution_enabled"] is False
    assert report.engine_version == 6
    assert report.formation_bars == 60
    assert report.evaluation_bars == 740
    assert report.evaluation_start == candles_a[60].timestamp
    assert report.integrity_checks["strict_formation_trading_separation"] is True
    assert all(trade.entry_time > report.evaluation_start for trade in report.trades)
    assert report.research_gate_passed is False
    assert report.robustness_passed is False
    assert report.execution_eligible is False
    assert all(trade.return_percent != 0 for trade in report.trades)
    assert any("资金费率时间覆盖" in reason for reason in report.validation_reasons)


def test_pair_backtest_charges_both_legs_and_persists_report(tmp_path):
    dataset_a, candles_a, dataset_b, candles_b = _pair_fixture()
    kwargs = {
        "dataset_a": dataset_a,
        "candles_a": candles_a,
        "funding_a": [],
        "dataset_b": dataset_b,
        "candles_b": candles_b,
        "funding_b": [],
        "strategy_spec": PairStrategySpec(
            formation_window=60,
            entry_zscore=Decimal("1.5"),
        ),
        "slippage_bps": Decimal("0"),
    }
    without_fees = run_pair_backtest(**kwargs, fee_bps=Decimal("0"))
    with_fees = run_pair_backtest(**kwargs, fee_bps=Decimal("20"))

    assert with_fees.final_equity < without_fees.final_equity
    store = SqliteStore(tmp_path / "relative.db")
    store.save_relative_value_report(with_fees)
    assert store.load_relative_value_report(with_fees.id)["total_trades"] == (
        with_fees.total_trades
    )


def test_pair_backtest_rejects_mismatched_timeframes():
    dataset_a, candles_a, dataset_b, candles_b = _pair_fixture()
    dataset_b = dataset_b.model_copy(update={"timeframe": "4h"})

    with pytest.raises(ValueError, match="same timeframe"):
        run_pair_backtest(
            dataset_a=dataset_a,
            candles_a=candles_a,
            funding_a=[],
            dataset_b=dataset_b,
            candles_b=candles_b,
            funding_b=[],
        )


def test_pair_backtest_rejects_evaluation_inside_formation_window():
    dataset_a, candles_a, dataset_b, candles_b = _pair_fixture()

    with pytest.raises(ValueError, match="after the complete formation window"):
        run_pair_backtest(
            dataset_a=dataset_a,
            candles_a=candles_a,
            funding_a=[],
            dataset_b=dataset_b,
            candles_b=candles_b,
            funding_b=[],
            strategy_spec=PairStrategySpec(formation_window=60),
            evaluation_start_index=59,
        )


def test_rolling_beta_residual_freezes_a_causal_non_equal_dollar_hedge():
    dataset_a, candles_a, dataset_b, candles_b = _pair_fixture()
    spec = PairStrategySpec(
        model="rolling_beta_residual",
        formation_window=60,
        entry_zscore=Decimal("1.5"),
        exit_zscore=Decimal("0.3"),
        stop_zscore=Decimal("5"),
        maximum_holding_bars=80,
    )

    report = run_pair_backtest(
        dataset_a=dataset_a.model_copy(update={"market_type": "spot"}),
        candles_a=candles_a,
        funding_a=[],
        dataset_b=dataset_b.model_copy(update={"market_type": "spot"}),
        candles_b=candles_b,
        funding_b=[],
        strategy_spec=spec,
    )

    assert report.total_trades > 5
    assert report.integrity_checks["causal_rolling_hedge_ratio"] is True
    assert report.integrity_checks["entry_hedge_ratio_frozen"] is True
    assert report.integrity_checks["gross_notional_normalized"] is True
    assert all(
        spec.minimum_hedge_ratio <= abs(trade.entry_hedge_ratio) <= spec.maximum_hedge_ratio
        for trade in report.trades
    )
    assert any(
        abs(trade.entry_hedge_ratio - Decimal("1")) > Decimal("0.0001")
        for trade in report.trades
    )
    assert spec.source_url == ENGLE_GRANGER_COINTEGRATION


def test_rolling_beta_signal_does_not_change_when_only_future_bars_change():
    _, candles_a, _, candles_b = _pair_fixture()
    spec = PairStrategySpec(model="rolling_beta_residual", formation_window=60)
    baseline = _causal_pair_signals(list(zip(candles_a, candles_b, strict=True)), spec)
    changed = list(candles_a)
    mutation_index = 500
    changed[mutation_index] = changed[mutation_index].model_copy(
        update={"close": changed[mutation_index].close * Decimal("1.5")}
    )
    mutated = _causal_pair_signals(list(zip(changed, candles_b, strict=True)), spec)

    assert mutated[:mutation_index] == baseline[:mutation_index]
    assert mutated[mutation_index] != baseline[mutation_index]


def test_residual_stationarity_gate_accepts_mean_reverting_formation_only():
    candles_a, candles_b = _residual_fixture()
    spec = PairStrategySpec(formation_window=60, maximum_holding_bars=120)

    evidence = _residual_stability_evidence(
        list(zip(candles_a, candles_b, strict=True)),
        spec,
        300,
    )

    assert evidence.formation_stationarity_passed is True
    assert evidence.formation_structure_stable is True
    assert evidence.evaluation_structure_stable is True
    assert evidence.passed is True
    assert evidence.dickey_fuller_t_statistic < Decimal("-2.86")
    assert evidence.estimated_half_life_bars < spec.maximum_holding_bars


def test_residual_stationarity_gate_rejects_integrated_spread():
    candles_a, candles_b = _residual_fixture(integrated=True)

    evidence = _residual_stability_evidence(
        list(zip(candles_a, candles_b, strict=True)),
        PairStrategySpec(formation_window=60),
        300,
    )

    assert evidence.formation_stationarity_passed is False
    assert evidence.passed is False
    assert any("stationarity gate" in reason for reason in evidence.reasons)


def test_residual_break_check_keeps_formation_evidence_causal():
    baseline_a, candles_b = _residual_fixture()
    broken_a, _ = _residual_fixture(evaluation_break=True)
    spec = PairStrategySpec(formation_window=60)
    baseline = _residual_stability_evidence(
        list(zip(baseline_a, candles_b, strict=True)), spec, 300
    )
    broken = _residual_stability_evidence(
        list(zip(broken_a, candles_b, strict=True)), spec, 300
    )

    assert broken.dickey_fuller_t_statistic == baseline.dickey_fuller_t_statistic
    assert broken.ar1_coefficient == baseline.ar1_coefficient
    assert broken.formation_mean_shift_z == baseline.formation_mean_shift_z
    assert broken.evaluation_structure_stable is False
    assert broken.passed is False


def test_relative_momentum_is_causal_and_uses_public_provenance():
    _, candles_a, _, candles_b = _pair_fixture()
    spec = PairStrategySpec(
        model="relative_momentum_equal_dollar",
        formation_window=60,
    )
    baseline = _causal_pair_signals(list(zip(candles_a, candles_b, strict=True)), spec)
    changed = list(candles_a)
    mutation_index = 500
    changed[mutation_index] = changed[mutation_index].model_copy(
        update={"close": changed[mutation_index].close * Decimal("1.5")}
    )
    mutated = _causal_pair_signals(list(zip(changed, candles_b, strict=True)), spec)

    assert mutated[:mutation_index] == baseline[:mutation_index]
    assert mutated[mutation_index] != baseline[mutation_index]
    assert spec.source_url == CROSS_SECTIONAL_MOMENTUM


def test_relative_momentum_trades_stronger_leg_and_honors_holding_limit():
    dataset_a, candles_a, dataset_b, candles_b = _pair_fixture()
    report = run_pair_backtest(
        dataset_a=dataset_a.model_copy(update={"market_type": "spot"}),
        candles_a=candles_a,
        funding_a=[],
        dataset_b=dataset_b.model_copy(update={"market_type": "spot"}),
        candles_b=candles_b,
        funding_b=[],
        strategy_spec=PairStrategySpec(
            model="relative_momentum_equal_dollar",
            formation_window=60,
            entry_zscore=Decimal("1"),
            exit_zscore=Decimal("0"),
            stop_zscore=Decimal("10"),
            maximum_holding_bars=10,
        ),
    )

    assert report.total_trades > 10
    assert report.integrity_checks["causal_relative_momentum"] is True
    assert any(trade.exit_reason == "maximum_holding" for trade in report.trades)
    assert all(trade.holding_bars <= 11 for trade in report.trades)
    assert all(
        (trade.entry_zscore > 0 and trade.long_symbol == report.symbol_a)
        or (trade.entry_zscore < 0 and trade.long_symbol == report.symbol_b)
        for trade in report.trades
    )


def test_funding_prefix_ledger_preserves_entry_exclusive_exit_inclusive_costs():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ledger = _FundingLedger(
        [
            FundingRatePoint(timestamp=start + timedelta(hours=index), rate=Decimal("0.001"))
            for index in range(5)
        ]
    )

    cost = _funding_cost(
        ledger,
        entry_time=start + timedelta(hours=1),
        exit_time=start + timedelta(hours=3),
        side=1,
        notional=Decimal("1000"),
    )

    assert cost == Decimal("2")
