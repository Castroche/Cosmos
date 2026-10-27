from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.backtest import (
    BacktestResult,
    BacktestTrade,
    BacktestWindowSummary,
    CausalStrategyPrior,
    TemporalStabilityReport,
    _aggregate_trade_episodes,
    _update_atr_trailing_stop,
    causal_library_policy_fingerprint,
    causal_liquidation_flow_context,
    causal_open_interest_context,
    causal_order_flow_context,
    latest_causal_funding_point,
    rank_causal_strategy_priors,
    run_bracket_backtest,
    run_walk_forward_backtest,
    validate_research_result,
)
from cosmos_quant.configuration import ResearchValidationPreferences, StrategyKind
from cosmos_quant.demo import demo_candles
from cosmos_quant.domain import (
    Candle,
    EntryRule,
    ExitPlan,
    FundingRatePoint,
    LiquidationFlowPoint,
    MarketSnapshot,
    OpenInterestPoint,
    OrderFlowBar,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
)
from cosmos_quant.signals import score_market_state
from cosmos_quant.strategy_library import library_entry


def _causal_prior(entry_id: str) -> CausalStrategyPrior:
    entry = library_entry(entry_id)
    return CausalStrategyPrior(
        id=entry.id,
        family=entry.family,
        archetype=entry.archetype,
        preferred_regimes=entry.preferred_regimes,
        avoid_regimes=entry.avoid_regimes,
        required_data=entry.required_data,
        spec=entry.spec,
        spec_fingerprint=entry.validated_spec.fingerprint,
    )


def _causal_policy_priors() -> list[CausalStrategyPrior]:
    return [
        _causal_prior("builtin_ema_12_26_trend_v2"),
        _causal_prior("builtin_sma_20_100_trend_v2"),
        _causal_prior("builtin_zscore_20_rsi_v2"),
    ]


def _plan(
    *,
    trailing_stop_atr: Decimal | None = None,
    trailing_activation: str = "first_target",
    break_even_after_first_target: bool = False,
    maximum_holding_bars: int | None = None,
) -> StrategyPlan:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return StrategyPlan(
        id="stg_backtest_truth",
        name="backtest truth fixture",
        exchange="historical",
        symbol="BTC/USDT",
        timeframe="1h",
        side="long",
        entry=EntryRule(zone=PriceZone(lower=Decimal("100"), upper=Decimal("100")), trigger="test"),
        exit=ExitPlan(
            stop_loss=Decimal("50"),
            take_profits=[
                ProfitTarget(price=Decimal("200"), close_fraction=Decimal("1"), rationale="test")
            ],
            trailing_stop_atr=trailing_stop_atr,
            trailing_activation=trailing_activation,
            break_even_after_first_target=break_even_after_first_target,
            maximum_holding_bars=maximum_holding_bars,
            invalidation="test",
        ),
        sizing=PositionSizing(
            risk_fraction=Decimal("0.01"),
            risk_amount=Decimal("50"),
            suggested_quantity=Decimal("1"),
            notional=Decimal("100"),
        ),
        minimum_reward_risk=Decimal("1"),
        thesis="test",
        reasons=["test"],
        snapshot=MarketSnapshot(
            symbol="BTC/USDT",
            timestamp=now,
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            last=Decimal("100"),
        ),
    )


def test_dynamic_stop_moves_to_actual_entry_after_first_target():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    plan = _plan(break_even_after_first_target=True)
    position = {
        "entry": Decimal("100.25"),
        "current_stop": Decimal("50"),
        "favorable_price": Decimal("100.25"),
        "target_index": 1,
    }
    candles = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("100"),
        )
        for index in range(2)
    ]

    _update_atr_trailing_stop(position, plan, candles, 1)

    assert position["current_stop"] == Decimal("100.25")


def test_bracket_backtest_closes_at_declared_maximum_holding_bars():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal(str(100 + index)),
            volume=Decimal("100"),
        )
        for index in range(3)
    ]

    result = run_bracket_backtest(
        _plan(maximum_holding_bars=2),
        candles,
        fee_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "time_stop"
    assert result.trades[0].exit_time == candles[2].timestamp.isoformat()
    assert result.trades[0].exit_price == candles[2].close


def test_walk_forward_backtest_generates_orders_after_signal_bar():
    result = run_walk_forward_backtest(demo_candles())

    assert result.total_trades > 0
    assert result.total_trades == len(result.trade_episodes)
    assert result.total_trades <= len(result.trades)
    assert sum(item.total_trades for item in result.strategy_attribution) == result.total_trades
    assert all(trade.algorithm != "unknown" for trade in result.trades)
    assert len(result.equity_curve) > 100
    assert all(trade.entry_time < trade.exit_time for trade in result.trades)
    assert result.annualization_periods == 8760
    assert result.sample_bars == len(demo_candles())
    assert result.stress_return_percent is not None
    assert result.cost_model["funding_included"] is False
    assert result.cost_model["confirmation_history_mode"] == "precomputed_causal_closed_bars"
    assert result.cost_model["confirmation_history_limit_bars"] == "300"
    assert result.total_fees >= 0
    assert result.recent_window is not None
    assert result.recent_window.sample_bars == len(demo_candles())
    assert result.recent_window.data_end == demo_candles()[-1].timestamp
    assert result.recent_window.total_trades <= result.total_trades
    assert result.temporal_stability is not None
    assert result.temporal_stability.lookahead_safe is True
    assert result.temporal_stability.parameter_mode == "immutable_no_in_sample_tuning"
    assert len(result.temporal_stability.windows) == 4
    assert result.robustness_passed is False
    assert "研究样本少于 1000 根 K 线" in result.validation_reasons
    assert any("有效交易少于 100 笔" in item for item in result.validation_reasons)


def test_causal_library_policy_is_fingerprint_bound_and_order_independent():
    priors = _causal_policy_priors()

    first = causal_library_policy_fingerprint(
        priors,
        maximum_candidates_per_bar=3,
        abstain_on_ambiguous_regime=True,
    )
    reordered = causal_library_policy_fingerprint(
        list(reversed(priors)),
        maximum_candidates_per_bar=3,
        abstain_on_ambiguous_regime=True,
    )
    changed_rule = causal_library_policy_fingerprint(
        priors,
        maximum_candidates_per_bar=2,
        abstain_on_ambiguous_regime=True,
    )
    changed_quality_gate = causal_library_policy_fingerprint(
        priors,
        maximum_candidates_per_bar=3,
        abstain_on_ambiguous_regime=True,
        minimum_regime_confidence=Decimal("0.7"),
        minimum_signal_confidence=Decimal("0.8"),
        minimum_directional_consensus=2,
    )

    assert first == reordered
    assert first != changed_rule
    assert first != changed_quality_gate
    with pytest.raises(ValueError, match="fingerprint"):
        CausalStrategyPrior.model_validate(
            {
                **priors[0].model_dump(mode="json"),
                "spec_fingerprint": "0" * 16,
            }
        )


def test_causal_library_policy_excludes_priors_with_unavailable_features():
    ohlcv = _causal_prior("builtin_ema_12_26_trend_v2")
    funding = _causal_prior("builtin_funding_aware_roc_24_v3")
    assessment = score_market_state(demo_candles(200))

    without_funding = rank_causal_strategy_priors(
        [funding, ohlcv],
        assessment,
        {"ohlcv"},
    )
    with_funding = rank_causal_strategy_priors(
        [funding, ohlcv],
        assessment,
        {"ohlcv", "funding_rate"},
    )

    assert funding.id not in {item[0].id for item in without_funding}
    assert ohlcv.id in {item[0].id for item in without_funding}
    assert funding.id in {item[0].id for item in with_funding}


def test_walk_forward_causal_library_policy_records_selected_public_priors():
    result = run_walk_forward_backtest(
        demo_candles(500),
        causal_strategy_priors=_causal_policy_priors(),
        causal_policy_max_candidates_per_bar=3,
        causal_policy_abstain_on_ambiguous_regime=False,
        compute_stress=False,
    )

    assert result.total_trades > 0
    assert result.strategy_fingerprint == result.cost_model[
        "causal_library_policy_fingerprint"
    ]
    assert result.integrity_checks["causal_library_policy_pre_registered"] is True
    assert result.integrity_checks["causal_library_policy_return_inputs_forbidden"] is True
    assert all(item.algorithm.startswith("library:builtin_") for item in result.trades)
    assert len(result.strategy_attribution) >= 2


def test_causal_library_policy_quality_gates_are_frozen_and_audited():
    baseline = run_walk_forward_backtest(
        demo_candles(500),
        causal_strategy_priors=_causal_policy_priors(),
        causal_policy_max_candidates_per_bar=3,
        causal_policy_abstain_on_ambiguous_regime=False,
        compute_stress=False,
    )
    selective = run_walk_forward_backtest(
        demo_candles(500),
        causal_strategy_priors=_causal_policy_priors(),
        causal_policy_max_candidates_per_bar=3,
        causal_policy_abstain_on_ambiguous_regime=True,
        causal_policy_minimum_regime_confidence=Decimal("0.6"),
        causal_policy_minimum_signal_confidence=Decimal("0.6"),
        causal_policy_minimum_directional_consensus=2,
        compute_stress=False,
    )

    assert selective.strategy_fingerprint != baseline.strategy_fingerprint
    assert selective.cost_model["causal_library_policy_minimum_regime_confidence"] == "0.6"
    assert selective.cost_model["causal_library_policy_minimum_signal_confidence"] == "0.6"
    assert selective.cost_model[
        "causal_library_policy_minimum_directional_consensus"
    ] == "2"
    with pytest.raises(ValueError, match="directional consensus"):
        run_walk_forward_backtest(
            demo_candles(500),
            causal_strategy_priors=_causal_policy_priors(),
            causal_policy_max_candidates_per_bar=1,
            causal_policy_minimum_directional_consensus=2,
            compute_stress=False,
        )


def test_causal_library_policy_cannot_rewrite_trades_with_future_candles():
    base = demo_candles(500)
    last = base[-1]
    future = [
        Candle(
            timestamp=last.timestamp + timedelta(hours=index + 1),
            open=last.close,
            high=last.close * (Decimal("1.40") if index % 2 == 0 else Decimal("1.02")),
            low=last.close * (Decimal("0.60") if index % 2 == 1 else Decimal("0.98")),
            close=last.close * (Decimal("1.25") if index % 2 == 0 else Decimal("0.75")),
            volume=last.volume * Decimal("10"),
        )
        for index in range(80)
    ]
    kwargs = {
        "causal_strategy_priors": _causal_policy_priors(),
        "causal_policy_max_candidates_per_bar": 3,
        "causal_policy_abstain_on_ambiguous_regime": False,
        "compute_stress": False,
    }

    original = run_walk_forward_backtest(base, **kwargs)
    extended = run_walk_forward_backtest([*base, *future], **kwargs)
    cutoff = last.timestamp.isoformat()

    original_closed = [
        item.model_dump(mode="json") for item in original.trades if item.exit_time < cutoff
    ]
    extended_closed = [
        item.model_dump(mode="json") for item in extended.trades if item.exit_time < cutoff
    ]
    assert original_closed
    assert original_closed == extended_closed


def test_walk_forward_time_stop_uses_strategy_spec_and_is_audited():
    result = run_walk_forward_backtest(
        demo_candles(500),
        strategy_kind=StrategyKind.TREND,
        strategy_spec={
            "family": "trend",
            "parameters": {"maximum_holding_bars": 1},
        },
        require_confirmation=False,
        compute_stress=False,
    )

    assert result.trades
    assert {trade.exit_reason for trade in result.trades} == {"time_stop"}
    assert result.cost_model["maximum_holding_bars"] == "1"


def test_walk_forward_backtest_quantizes_synthetic_spread_for_low_price_assets():
    tick = Decimal("0.00000001")
    scale = Decimal("0.00071")
    candles = [
        candle.model_copy(
            update={
                field: (getattr(candle, field) * scale).quantize(tick)
                for field in ("open", "high", "low", "close")
            }
        )
        for candle in demo_candles()
    ]

    result = run_walk_forward_backtest(candles, symbol="XRP/USDT")

    assert result.sample_bars == len(candles)


def test_partial_exits_are_one_independent_trade_episode():
    first = BacktestTrade(
        entry_time="2026-01-01T00:00:00+00:00",
        exit_time="2026-01-01T01:00:00+00:00",
        side=Side.LONG,
        algorithm="trend",
        signal_regime="trend",
        entry_price="100",
        exit_price="110",
        quantity="0.7",
        pnl="7",
        exit_reason="take_profit",
        fees="0.1",
    )
    second = first.model_copy(
        update={
            "exit_time": "2026-01-01T02:00:00+00:00",
            "exit_price": Decimal("95"),
            "quantity": Decimal("0.3"),
            "pnl": Decimal("-1.5"),
            "exit_reason": "trailing_stop",
            "fees": Decimal("0.05"),
        }
    )

    episodes = _aggregate_trade_episodes([first, second])

    assert len(episodes) == 1
    assert episodes[0].pnl == Decimal("5.5")
    assert episodes[0].fees == Decimal("0.15")
    assert episodes[0].exit_reasons == ["take_profit", "trailing_stop"]


def test_walk_forward_uses_strategy_specific_indicator_warmup():
    candles = demo_candles(300)

    result = run_walk_forward_backtest(
        candles,
        strategy_kind=StrategyKind.TREND,
        strategy_spec={
            "family": "trend",
            "parameters": {
                "ema_fast": 20,
                "ema_slow": 60,
                "confirmation_ema_fast": 20,
                "confirmation_ema_slow": 60,
            },
        },
        require_confirmation=True,
        compute_stress=False,
    )

    assert result.sample_bars == 300
    assert result.temporal_stability is not None
    assert int(result.cost_model["signal_history_limit_bars"]) >= 366


def test_walk_forward_records_live_regime_family_fit_parity():
    result = run_walk_forward_backtest(
        demo_candles(),
        compute_stress=False,
    )

    assert result.cost_model["regime_family_fit"] is True


def test_walk_forward_can_validate_strategy_without_external_regime_gate():
    result = run_walk_forward_backtest(
        demo_candles(),
        require_regime_family_fit=False,
        compute_stress=False,
    )

    assert result.cost_model["regime_family_fit"] is False


def test_four_hour_backtest_expands_history_for_slow_daily_confirmation():
    result = run_walk_forward_backtest(
        demo_candles(300),
        timeframe="4h",
        strategy_spec={
            "family": "trend",
            "parameters": {
                "ema_fast": 32,
                "ema_slow": 96,
                "confirmation_ema_fast": 32,
                "confirmation_ema_slow": 96,
            },
        },
        require_confirmation=True,
        compute_stress=False,
    )

    assert int(result.cost_model["signal_history_limit_bars"]) >= 582


def test_walk_forward_backtest_can_bind_evidence_to_one_trade_direction():
    candles = demo_candles()

    long_only = run_walk_forward_backtest(candles, side_filter=Side.LONG)
    short_only = run_walk_forward_backtest(candles, side_filter=Side.SHORT)

    assert long_only.total_trades > 0
    assert all(trade.side == Side.LONG for trade in long_only.trades)
    assert all(trade.side == Side.SHORT for trade in short_only.trades)
    assert long_only.cost_model["direction_filter"] == "long"
    assert short_only.cost_model["direction_filter"] == "short"


def test_near_standard_trade_sample_requires_recent_and_stress_corroboration():
    now = datetime.now(UTC)
    recent = BacktestWindowSummary(
        label="latest_25_percent",
        sample_bars=500,
        data_start=now - timedelta(days=30),
        data_end=now,
        net_profit=Decimal("72"),
        return_percent=Decimal("0.72"),
        max_drawdown_percent=Decimal("0.38"),
        win_rate_percent=Decimal("56.67"),
        profit_factor=Decimal("1.61"),
        total_trades=30,
    )
    base = BacktestResult(
        initial_equity=Decimal("10000"),
        final_equity=Decimal("10079"),
        net_profit=Decimal("79"),
        return_percent=Decimal("0.79"),
        max_drawdown_percent=Decimal("1.13"),
        win_rate_percent=Decimal("46.67"),
        profit_factor=Decimal("1.18"),
        sharpe=Decimal("1"),
        total_trades=90,
        trades=[],
        equity_curve=[Decimal("10000"), Decimal("10079")],
        sample_bars=1999,
        sample_years=Decimal("0.228"),
        recent_window=recent,
        temporal_stability=TemporalStabilityReport(
            fold_count=4,
            minimum_trades_per_fold=10,
            evaluable_folds=4,
            positive_folds=3,
            positive_fold_ratio=Decimal("0.75"),
            worst_fold_return_percent=Decimal("-0.10"),
            passed=True,
            reasons=["fixed-spec evidence passed"],
            windows=[],
        ),
    )
    stressed = base.model_copy(update={"return_percent": Decimal("0.11")})

    accepted = validate_research_result(base, stressed, ResearchValidationPreferences())

    assert accepted.robustness_passed is True
    assert accepted.trade_sample_standard == 100
    assert accepted.trade_sample_floor == 80
    assert accepted.trade_sample_corroborated is True
    assert any("一致证据补偿" in item for item in accepted.validation_reasons)

    stale_recent = base.model_copy(
        update={
            "recent_window": recent.model_copy(
                update={
                    "return_percent": Decimal("-0.10"),
                    "profit_factor": Decimal("0.90"),
                }
            )
        }
    )
    rejected = validate_research_result(stale_recent, stressed, ResearchValidationPreferences())
    assert rejected.robustness_passed is False
    assert rejected.trade_sample_corroborated is False


def test_strong_multi_evidence_can_replace_arbitrary_near_standard_trade_count():
    now = datetime.now(UTC)
    temporal = TemporalStabilityReport(
        fold_count=4,
        minimum_trades_per_fold=4,
        evaluable_folds=3,
        positive_folds=3,
        positive_fold_ratio=Decimal("1"),
        worst_fold_return_percent=Decimal("0.31"),
        passed=True,
        reasons=["fixed-spec evidence passed"],
        windows=[],
    )
    result = BacktestResult(
        initial_equity=Decimal("80000"),
        final_equity=Decimal("81560"),
        net_profit=Decimal("1560"),
        return_percent=Decimal("1.95"),
        max_drawdown_percent=Decimal("1.19"),
        win_rate_percent=Decimal("54.05"),
        profit_factor=Decimal("1.47"),
        sharpe=Decimal("1"),
        total_trades=37,
        trades=[],
        equity_curve=[Decimal("80000"), Decimal("81560")],
        sample_bars=1999,
        sample_years=Decimal("0.228"),
        recent_window=BacktestWindowSummary(
            label="latest_25_percent",
            sample_bars=500,
            data_start=now - timedelta(days=20),
            data_end=now,
            net_profit=Decimal("400"),
            return_percent=Decimal("0.50"),
            max_drawdown_percent=Decimal("1.19"),
            win_rate_percent=Decimal("52.17"),
            profit_factor=Decimal("1.18"),
            total_trades=23,
        ),
        temporal_stability=temporal,
    )
    stressed = result.model_copy(update={"return_percent": Decimal("1.43")})

    accepted = validate_research_result(
        result,
        stressed,
        ResearchValidationPreferences(),
    )

    assert accepted.robustness_passed is True
    assert accepted.trade_sample_strong_floor == 30
    assert accepted.trade_sample_strongly_corroborated is True
    assert accepted.trade_sample_corroborated is True
    assert accepted.trade_sample_tier == "strong_multi_evidence"
    assert any("参数前推验证" in reason for reason in accepted.validation_reasons)


def test_high_win_rate_cannot_hide_a_poor_average_win_loss_ratio():
    now = datetime.now(UTC)
    result = BacktestResult(
        initial_equity=Decimal("10000"),
        final_equity=Decimal("10100"),
        net_profit=Decimal("100"),
        return_percent=Decimal("1"),
        max_drawdown_percent=Decimal("2"),
        win_rate_percent=Decimal("80"),
        profit_factor=Decimal("1.30"),
        sharpe=Decimal("1"),
        total_trades=120,
        trades=[],
        equity_curve=[Decimal("10000"), Decimal("10100")],
        sample_bars=2000,
        recent_window=BacktestWindowSummary(
            label="latest_25_percent",
            sample_bars=500,
            data_start=now - timedelta(days=30),
            data_end=now,
            net_profit=Decimal("50"),
            return_percent=Decimal("0.5"),
            max_drawdown_percent=Decimal("1"),
            win_rate_percent=Decimal("80"),
            profit_factor=Decimal("1.30"),
            total_trades=30,
        ),
        temporal_stability=TemporalStabilityReport(
            fold_count=4,
            minimum_trades_per_fold=10,
            evaluable_folds=4,
            positive_folds=4,
            positive_fold_ratio=Decimal("1"),
            worst_fold_return_percent=Decimal("0.1"),
            passed=True,
            windows=[],
        ),
    )

    validated = validate_research_result(
        result,
        result.model_copy(update={"return_percent": Decimal("0.2")}),
        ResearchValidationPreferences(),
    )

    assert validated.robustness_passed is False
    assert validated.payoff_ratio < Decimal("1.20")
    assert any("平均盈利/平均亏损" in reason for reason in validated.validation_reasons)


def test_walk_forward_backtest_applies_historical_funding_costs():
    candles = demo_candles()
    funding = [
        FundingRatePoint(
            timestamp=candle.timestamp,
            rate=Decimal("0.01"),
            mark_price=candle.close,
        )
        for candle in candles
    ]

    baseline = run_walk_forward_backtest(candles, allow_short=False)
    funded = run_walk_forward_backtest(
        candles,
        allow_short=False,
        funding_rates=funding,
    )

    assert funded.cost_model["funding_included"] is True
    assert funded.total_funding > 0
    assert funded.final_equity < baseline.final_equity


def test_walk_forward_records_intended_and_exposure_capped_position():
    result = run_walk_forward_backtest(
        demo_candles(),
        maximum_gross_exposure=Decimal("0.05"),
        maximum_leverage=Decimal("3"),
    )

    assert int(result.cost_model["gross_exposure_cap_count"]) > 0
    assert int(result.cost_model["leverage_cap_count"]) == 0
    assert result.trades
    assert all(trade.intended_quantity is not None for trade in result.trades)
    assert all(trade.actual_entry_quantity is not None for trade in result.trades)
    assert all(
        trade.actual_entry_quantity <= trade.intended_quantity
        for trade in result.trades
        if trade.actual_entry_quantity is not None and trade.intended_quantity is not None
    )
    assert any("gross_exposure" in trade.position_limit_reason for trade in result.trades)


def test_historical_funding_context_is_causal_and_expires_when_stale():
    timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    points = [
        FundingRatePoint(
            timestamp=timestamp - timedelta(hours=25),
            rate=Decimal("0.001"),
        ),
        FundingRatePoint(
            timestamp=timestamp + timedelta(hours=1),
            rate=Decimal("-0.001"),
        ),
    ]
    point_times = [item.timestamp for item in points]

    assert latest_causal_funding_point(points, point_times, timestamp) is None
    recent = FundingRatePoint(
        timestamp=timestamp - timedelta(hours=8),
        rate=Decimal("0.0002"),
    )
    points.insert(1, recent)
    point_times = [item.timestamp for item in points]
    assert latest_causal_funding_point(points, point_times, timestamp) == recent


def test_causal_open_interest_context_rejects_future_and_stale_baseline():
    timestamp = datetime(2026, 1, 2, 12, tzinfo=UTC)
    points = [
        OpenInterestPoint(
            timestamp=timestamp - timedelta(hours=24),
            open_interest_amount=Decimal("100"),
        ),
        OpenInterestPoint(
            timestamp=timestamp,
            open_interest_amount=Decimal("110"),
        ),
        OpenInterestPoint(
            timestamp=timestamp + timedelta(hours=1),
            open_interest_amount=Decimal("999"),
        ),
    ]
    known, change = causal_open_interest_context(
        points,
        [item.timestamp for item in points],
        timestamp,
    )

    assert known == points[1]
    assert change == Decimal("0.1")

    stale = points[0].model_copy(update={"timestamp": timestamp - timedelta(hours=49)})
    stale_points = [stale, points[1], points[2]]
    _, missing_change = causal_open_interest_context(
        stale_points,
        [item.timestamp for item in stale_points],
        timestamp,
    )
    assert missing_change is None


def test_order_flow_is_unavailable_until_its_bar_closes():
    bar_start = datetime(2026, 1, 2, 12, tzinfo=UTC)
    point = OrderFlowBar(
        timestamp=bar_start,
        aggregate_count=10,
        underlying_trade_count=20,
        base_volume=Decimal("10"),
        quote_volume=Decimal("1000"),
        taker_buy_base_volume=Decimal("7"),
        taker_sell_base_volume=Decimal("3"),
        delta_base_volume=Decimal("4"),
        maximum_trade_quote=Decimal("200"),
    )
    available_at = bar_start + timedelta(hours=1)

    hidden, hidden_imbalance = causal_order_flow_context(
        [point],
        [available_at],
        bar_start + timedelta(minutes=59),
    )
    known, imbalance = causal_order_flow_context(
        [point],
        [available_at],
        available_at,
    )

    assert hidden is None
    assert hidden_imbalance is None
    assert known == point
    assert imbalance == Decimal("0.4")


def test_liquidation_flow_uses_only_the_closed_24_hour_prefix():
    start = datetime(2026, 1, 2, 12, tzinfo=UTC)
    points = [
        LiquidationFlowPoint(
            timestamp=start + timedelta(hours=index + 1),
            interval_start=start + timedelta(hours=index),
            long_liquidated_contracts=Decimal("2"),
            short_liquidated_contracts=Decimal("6"),
            unique_event_count=2,
            source_instrument_id="BTCUSD_PERP",
        )
        for index in range(3)
    ]
    as_of = points[1].timestamp
    known, long_contracts, short_contracts, imbalance = causal_liquidation_flow_context(
        points,
        [item.timestamp for item in points],
        as_of,
    )

    assert known == points[1]
    assert long_contracts == Decimal("4")
    assert short_contracts == Decimal("12")
    assert imbalance == Decimal("0.5")
    changed_future = points[2].model_copy(
        update={"short_liquidated_contracts": Decimal("9999")}
    )
    repeated = causal_liquidation_flow_context(
        [points[0], points[1], changed_future],
        [item.timestamp for item in points],
        as_of,
    )
    assert repeated == (known, long_contracts, short_contracts, imbalance)


def test_bracket_backtest_marks_open_risk_and_closes_terminal_position():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(timestamp=start, open=100, high=101, low=99, close=100, volume=10),
        Candle(
            timestamp=start + timedelta(hours=1),
            open=100,
            high=101,
            low=90,
            close=90,
            volume=10,
        ),
    ]

    result = run_bracket_backtest(
        _plan(), candles, initial_equity=Decimal("1000"), fee_bps=0, slippage_bps=0
    )

    assert result.terminal_position_closed is True
    assert result.trades[-1].exit_reason == "end_of_data"
    assert result.final_equity == Decimal("990.00")
    assert result.max_drawdown_percent == Decimal("1.00")
    assert result.equity_curve[-1] == result.final_equity


def test_bracket_backtest_executes_declared_atr_trailing_stop():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=10,
        )
        for index in range(15)
    ]
    candles.extend(
        [
            Candle(
                timestamp=start + timedelta(hours=15),
                open=100,
                high=110,
                low=100,
                close=108,
                volume=20,
            ),
            Candle(
                timestamp=start + timedelta(hours=16),
                open=108,
                high=108,
                low=105,
                close=106,
                volume=20,
            ),
        ]
    )

    result = run_bracket_backtest(
        _plan(
            trailing_stop_atr=Decimal("1"),
            trailing_activation="immediate",
        ),
        candles,
        initial_equity=Decimal("1000"),
        fee_bps=0,
        slippage_bps=0,
    )

    assert result.terminal_position_closed is False
    assert result.trades[-1].exit_reason == "trailing_stop"
    assert result.final_equity > Decimal("1000")


def test_bracket_backtest_does_not_trail_before_declared_first_target():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=10,
        )
        for index in range(15)
    ]
    candles.extend(
        [
            Candle(
                timestamp=start + timedelta(hours=15),
                open=100,
                high=110,
                low=100,
                close=108,
                volume=20,
            ),
            Candle(
                timestamp=start + timedelta(hours=16),
                open=108,
                high=108,
                low=105,
                close=106,
                volume=20,
            ),
        ]
    )

    result = run_bracket_backtest(
        _plan(trailing_stop_atr=Decimal("1")),
        candles,
        initial_equity=Decimal("1000"),
        fee_bps=0,
        slippage_bps=0,
    )

    assert result.terminal_position_closed is True
    assert result.trades[-1].exit_reason == "end_of_data"
