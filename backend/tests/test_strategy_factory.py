from decimal import Decimal

import pytest
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import MarketSnapshot, Side, SignalDirection
from cosmos_quant.indicators import sma
from cosmos_quant.signals import NoTradeSignal, score_market_state, select_strategy_kind
from cosmos_quant.strategy import quantize_strategy_price
from cosmos_quant.strategy_factory import StrategyFactory


def actionable_candles(kind: StrategyKind):
    candles = demo_candles()
    if kind == StrategyKind.TREND:
        return candles

    flat = [
        candle.model_copy(
            update={
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("1000"),
            }
        )
        for candle in candles
    ]
    close = Decimal("80") if kind == StrategyKind.MEAN_REVERSION else Decimal("111")
    flat[-1] = flat[-1].model_copy(
        update={
            "open": Decimal("100"),
            "high": max(close, Decimal("112")),
            "low": min(close, Decimal("79")),
            "close": close,
            "volume": Decimal("5000"),
        }
    )
    return flat


@pytest.mark.parametrize("kind", list(StrategyKind))
def test_every_strategy_kind_emits_executable_levels(kind):
    candles = actionable_candles(kind)
    snapshot = snapshot_from_candles(candles)
    minimum_reward_risk = Decimal("1.8")
    plan = StrategyFactory().generate(
        kind,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=minimum_reward_risk,
    )

    entry = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    stop_distance = abs(entry - plan.exit.stop_loss)
    reward_distance = abs(plan.exit.take_profits[0].price - entry)
    assert plan.sizing.risk_amount == Decimal("500.00")
    assert reward_distance / stop_distance >= minimum_reward_risk
    if plan.side == Side.LONG:
        assert plan.exit.stop_loss < plan.entry.zone.lower
        assert plan.exit.take_profits[0].price > plan.entry.zone.upper
    else:
        assert plan.exit.stop_loss > plan.entry.zone.upper
        assert plan.exit.take_profits[0].price < plan.entry.zone.lower


def test_partial_first_target_preserves_weighted_reward_risk_floor():
    candles = actionable_candles(StrategyKind.TREND)
    snapshot = snapshot_from_candles(candles)
    minimum_reward_risk = Decimal("1.8")
    plan = StrategyFactory().generate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=minimum_reward_risk,
        strategy_spec={
            "family": "trend",
            "parameters": {"first_target_reward_risk": "1.0"},
        },
    )

    entry = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    stop_distance = abs(entry - plan.exit.stop_loss)
    target_ratios = [abs(target.price - entry) / stop_distance for target in plan.exit.take_profits]
    weighted = sum(
        ratio * target.close_fraction
        for ratio, target in zip(target_ratios, plan.exit.take_profits, strict=True)
    )

    assert target_ratios[0] < minimum_reward_risk
    assert weighted >= minimum_reward_risk
    assert sum(target.close_fraction for target in plan.exit.take_profits) == Decimal("1")


def test_mean_touch_exit_uses_visible_mean_and_preserves_weighted_reward_floor():
    candles = actionable_candles(StrategyKind.MEAN_REVERSION)
    snapshot = snapshot_from_candles(candles)
    minimum_reward_risk = Decimal("1.8")
    plan = StrategyFactory().generate(
        StrategyKind.MEAN_REVERSION,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=minimum_reward_risk,
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {
                "mean_completion_mode": "mean_touch",
                "break_even_after_first_target": True,
                "maximum_holding_bars": 40,
                "trailing_stop_atr": "1.2",
            },
        },
    )

    entry = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    stop_distance = abs(entry - plan.exit.stop_loss)
    target_ratios = [abs(target.price - entry) / stop_distance for target in plan.exit.take_profits]
    weighted = sum(
        ratio * target.close_fraction
        for ratio, target in zip(target_ratios, plan.exit.take_profits, strict=True)
    )
    visible_mean = quantize_strategy_price(sma([candle.close for candle in candles], 20))

    assert plan.exit.take_profits[0].price == visible_mean
    assert "统计均值" in plan.exit.take_profits[0].rationale
    assert weighted >= minimum_reward_risk
    assert plan.exit.maximum_holding_bars == 40
    assert plan.exit.break_even_after_first_target is True
    assert plan.exit.trailing_stop_atr == Decimal("1.2")


def test_low_price_strategy_levels_keep_sub_cent_precision():
    scale = Decimal("0.0000011")
    tick = Decimal("0.00000001")
    candles = [
        candle.model_copy(
            update={
                field: (getattr(candle, field) * scale).quantize(tick)
                for field in ("open", "high", "low", "close")
            }
        )
        for candle in actionable_candles(StrategyKind.TREND)
    ]
    last = candles[-1].close
    snapshot = MarketSnapshot(
        symbol="XRP/USDT",
        timestamp=candles[-1].timestamp,
        bid=last - Decimal("0.00001"),
        ask=last + Decimal("0.00001"),
        last=last,
    )
    plan = StrategyFactory().generate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="historical",
        timeframe="1h",
        minimum_reward_risk=Decimal("1.8"),
    )

    assert Decimal("0") < abs(plan.entry.zone.lower - plan.exit.stop_loss) < Decimal("0.01")
    assert quantize_strategy_price(Decimal("0.071012898")) == Decimal("0.071013")


@pytest.mark.parametrize("kind", list(StrategyKind))
def test_spot_strategy_never_emits_an_unexecutable_short(kind):
    candles = actionable_candles(kind)
    snapshot = snapshot_from_candles(candles)
    plan = StrategyFactory().generate(
        kind,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=Decimal("1.8"),
        allow_short=False,
    )

    assert plan.side == Side.LONG


@pytest.mark.parametrize("kind", [StrategyKind.MEAN_REVERSION, StrategyKind.BREAKOUT])
def test_strategy_factory_stays_flat_without_a_confirmed_setup(kind):
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    factory = StrategyFactory()

    signal = factory.evaluate(
        kind,
        candles=candles,
        snapshot=snapshot,
        timeframe="1h",
    )

    assert signal.direction == SignalDirection.FLAT
    assert signal.actionable is False
    with pytest.raises(NoTradeSignal):
        factory.generate(
            kind,
            candles=candles,
            snapshot=snapshot,
            equity=Decimal("100000"),
            risk_fraction=Decimal("0.005"),
            exchange="okx",
            timeframe="1h",
            minimum_reward_risk=Decimal("1.8"),
        )


def test_positioning_and_basis_contrarian_models_use_causal_snapshot_fields():
    candles = demo_candles()
    base = snapshot_from_candles(candles)
    factory = StrategyFactory()

    positioning = factory.evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=candles,
        snapshot=base.model_copy(update={"long_short_ratio": Decimal("2")}),
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {
                "entry_model": "positioning_contrarian",
                "positioning_ratio_threshold": "1.5",
            },
        },
    )
    basis = factory.evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=candles,
        snapshot=base.model_copy(update={"basis_rate": Decimal("-0.002")}),
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {
                "entry_model": "basis_contrarian",
                "basis_rate_threshold": "0.0015",
            },
        },
    )

    assert positioning.direction == SignalDirection.SHORT
    assert positioning.features["long_short_ratio"] == Decimal("2")
    assert basis.direction == SignalDirection.LONG
    assert basis.features["basis_rate"] == Decimal("-0.002")


def test_order_flow_confirmation_rejects_an_opposing_closed_bar():
    candles = demo_candles()
    base = snapshot_from_candles(candles)
    spec = {
        "family": "trend",
        "parameters": {
            "signal_gap_atr": "0",
            "signal_slope_atr": "0",
            "order_flow_mode": "confirm_direction",
            "minimum_order_flow_imbalance": "0.05",
        },
    }

    opposed = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=base.model_copy(update={"order_flow_imbalance": Decimal("-0.2")}),
        timeframe="1h",
        strategy_spec=spec,
    )
    confirmed = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=base.model_copy(update={"order_flow_imbalance": Decimal("0.2")}),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert opposed.direction == SignalDirection.FLAT
    assert opposed.features["order_flow_filter_passed"] is False
    assert confirmed.direction == SignalDirection.LONG
    assert confirmed.features["order_flow_filter_passed"] is True


def test_coinm_liquidation_context_is_explicit_and_can_only_filter_a_signal():
    candles = demo_candles()
    base = snapshot_from_candles(candles)
    spec = {
        "family": "trend",
        "parameters": {
            "signal_gap_atr": "0",
            "signal_slope_atr": "0",
            "liquidation_flow_mode": "confirm_direction",
            "minimum_liquidation_flow_imbalance": "0.10",
        },
    }
    provenance = {
        "liquidation_flow_time": base.timestamp,
        "liquidation_source_market_type": "coin_m_inverse_perpetual",
        "liquidation_source_instrument_id": "BTCUSD_PERP",
        "liquidation_evidence_role": "cross_contract_underlying_liquidation_context",
    }
    opposed = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=base.model_copy(
            update={**provenance, "liquidation_flow_imbalance": Decimal("-0.2")}
        ),
        timeframe="1h",
        strategy_spec=spec,
    )
    confirmed = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=base.model_copy(
            update={**provenance, "liquidation_flow_imbalance": Decimal("0.2")}
        ),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert opposed.direction == SignalDirection.FLAT
    assert opposed.features["liquidation_flow_filter_passed"] is False
    assert confirmed.direction == SignalDirection.LONG
    assert confirmed.features["liquidation_flow_filter_passed"] is True
    assert confirmed.features["liquidation_source_market_type"] == (
        "coin_m_inverse_perpetual"
    )


def test_adx_state_filter_separates_range_and_trend_hypotheses():
    candles = actionable_candles(StrategyKind.MEAN_REVERSION)
    snapshot = snapshot_from_candles(candles)
    base_parameters = {
        "entry_model": "zscore_rsi",
        "zscore_entry": "0.5",
        "rsi_oversold": "49",
        "rsi_overbought": "51",
        "adx_threshold": "20",
    }

    range_signal = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=candles,
        snapshot=snapshot,
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {
                **base_parameters,
                "trend_strength_mode": "require_range",
            },
        },
    )
    trend_only_signal = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=candles,
        snapshot=snapshot,
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {
                **base_parameters,
                "trend_strength_mode": "require_trend",
            },
        },
    )

    assert range_signal.actionable is True
    assert range_signal.features["adx"] < Decimal("20")
    assert range_signal.features["trend_strength_filter_passed"] is True
    assert trend_only_signal.direction == SignalDirection.FLAT
    assert trend_only_signal.features["trend_strength_filter_passed"] is False


def test_higher_timeframe_disagreement_turns_signal_flat():
    candles = demo_candles()
    descending = [
        candle.model_copy(
            update={
                "open": Decimal(300 - index),
                "high": Decimal(301 - index),
                "low": Decimal(298 - index),
                "close": Decimal(299 - index),
            }
        )
        for index, candle in enumerate(demo_candles(60))
    ]

    signal = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        confirmation_candles=descending,
    )

    assert signal.direction == SignalDirection.FLAT
    assert signal.features["higher_timeframe_confirmed"] is False


def test_trend_efficiency_filter_rejects_directionless_price_path():
    candles = [
        candle.model_copy(
            update={
                "open": Decimal("100") + Decimal(index) / Decimal("10"),
                "high": Decimal("103") + Decimal(index) / Decimal("10"),
                "low": Decimal("97") + Decimal(index) / Decimal("10"),
                "close": (
                    Decimal("102") + Decimal(index) / Decimal("10")
                    if index % 2
                    else Decimal("98") + Decimal(index) / Decimal("10")
                ),
            }
        )
        for index, candle in enumerate(demo_candles(100))
    ]

    signal = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        strategy_spec={
            "family": "trend",
            "parameters": {
                "signal_gap_atr": "0",
                "signal_slope_atr": "0",
                "efficiency_window": 20,
                "minimum_efficiency_ratio": "0.35",
            },
        },
    )

    assert signal.actionable is False
    assert signal.features["trend_efficiency_ratio"] < Decimal("0.35")
    assert any("方向效率" in reason for reason in signal.reasons)


def test_trend_pullback_reclaim_requires_a_fresh_entry_event():
    candles = [
        candle.model_copy(
            update={
                "open": Decimal("99.8") + Decimal(index) / Decimal("2"),
                "high": Decimal("101") + Decimal(index) / Decimal("2"),
                "low": Decimal("99") + Decimal(index) / Decimal("2"),
                "close": Decimal("100") + Decimal(index) / Decimal("2"),
                "volume": Decimal("1000"),
            }
        )
        for index, candle in enumerate(demo_candles(100))
    ]
    spec = {
        "family": "trend",
        "parameters": {
            "signal_gap_atr": "0",
            "signal_slope_atr": "0",
            "pullback_mode": "close_reclaim",
            "maximum_entry_extension_atr": "5",
        },
    }
    persistent = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert persistent.actionable is False
    assert persistent.features["trend_entry_event_confirmed"] is False

    reclaimed = list(candles)
    reclaimed[-2] = reclaimed[-2].model_copy(
        update={"open": 140, "high": 141, "low": 139, "close": 140}
    )
    reclaimed[-1] = reclaimed[-1].model_copy(
        update={"open": 150, "high": 152, "low": 149, "close": 151}
    )
    signal = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=reclaimed,
        snapshot=snapshot_from_candles(reclaimed),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert signal.direction == SignalDirection.LONG
    assert signal.features["trend_entry_event_confirmed"] is True


def test_donchian_retest_waits_for_a_later_causal_level_reclaim():
    candles = [
        candle.model_copy(
            update={
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("1000"),
            }
        )
        for candle in demo_candles(80)
    ]
    candles[-3] = candles[-3].model_copy(
        update={
            "open": Decimal("100"),
            "high": Decimal("106"),
            "low": Decimal("100"),
            "close": Decimal("105"),
            "volume": Decimal("2000"),
        }
    )
    candles[-2] = candles[-2].model_copy(
        update={
            "open": Decimal("104"),
            "high": Decimal("105"),
            "low": Decimal("103"),
            "close": Decimal("104"),
            "volume": Decimal("900"),
        }
    )
    candles[-1] = candles[-1].model_copy(
        update={
            "open": Decimal("101.4"),
            "high": Decimal("102.5"),
            "low": Decimal("100.8"),
            "close": Decimal("102"),
            "volume": Decimal("1000"),
        }
    )
    spec = {
        "family": "breakout",
        "parameters": {
            "entry_model": "donchian_retest",
            "lookback": 40,
            "minimum_volume_ratio": "1.15",
            "retest_window": 5,
            "retest_tolerance_atr": "0.25",
        },
    }

    signal = StrategyFactory().evaluate(
        StrategyKind.BREAKOUT,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert signal.direction == SignalDirection.LONG
    assert signal.features["breakout_retest_level"] == Decimal("101")
    assert signal.features["breakout_retest_bars_ago"] == "2"
    assert signal.features["breakout_retest_confirmed"] is True

    missed = list(candles)
    missed[-1] = missed[-1].model_copy(
        update={"open": Decimal("103"), "low": Decimal("102.8"), "close": Decimal("104")}
    )
    no_signal = StrategyFactory().evaluate(
        StrategyKind.BREAKOUT,
        candles=missed,
        snapshot=snapshot_from_candles(missed),
        timeframe="1h",
        strategy_spec=spec,
    )
    assert no_signal.actionable is False
    assert no_signal.features["breakout_retest_confirmed"] is False


def test_bollinger_reentry_requires_closed_extreme_then_closed_recovery():
    candles = [
        candle.model_copy(
            update={
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("1000"),
            }
        )
        for candle in demo_candles(61)
    ]
    candles[-3] = candles[-3].model_copy(
        update={
            "open": Decimal("100"),
            "high": Decimal("100"),
            "low": Decimal("79"),
            "close": Decimal("80"),
        }
    )
    candles[-2] = candles[-2].model_copy(
        update={
            "open": Decimal("82"),
            "high": Decimal("91"),
            "low": Decimal("81"),
            "close": Decimal("90"),
        }
    )
    spec = {
        "family": "mean_reversion",
        "parameters": {
            "entry_model": "bollinger_reentry",
            "mean_window": 20,
            "band_deviations": "2",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
        },
    }

    closed = candles[:-1]
    signal = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=closed,
        snapshot=snapshot_from_candles(closed),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert signal.direction == SignalDirection.LONG
    assert signal.features["bollinger_reentry_confirmed"] is True
    assert signal.features["bollinger_previous_close"] == Decimal("80")

    future_changed = list(candles)
    future_changed[-1] = future_changed[-1].model_copy(
        update={
            "open": Decimal("1"),
            "high": Decimal("1000"),
            "low": Decimal("1"),
            "close": Decimal("999"),
        }
    )
    unchanged = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=future_changed[:-1],
        snapshot=snapshot_from_candles(future_changed[:-1]),
        timeframe="1h",
        strategy_spec=spec,
    )
    assert unchanged.direction == signal.direction
    assert unchanged.features == signal.features

    no_reentry = list(closed)
    no_reentry[-1] = no_reentry[-1].model_copy(
        update={
            "open": Decimal("81"),
            "high": Decimal("83"),
            "low": Decimal("75"),
            "close": Decimal("79"),
        }
    )
    flat = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=no_reentry,
        snapshot=snapshot_from_candles(no_reentry),
        timeframe="1h",
        strategy_spec=spec,
    )
    assert flat.actionable is False
    assert flat.features["bollinger_reentry_confirmed"] is False


def test_zscore_reentry_requires_closed_extreme_then_closed_dual_recovery():
    candles = [
        candle.model_copy(
            update={
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("1000"),
            }
        )
        for candle in demo_candles(61)
    ]
    candles[-3] = candles[-3].model_copy(
        update={
            "open": Decimal("100"),
            "high": Decimal("100"),
            "low": Decimal("79"),
            "close": Decimal("80"),
        }
    )
    candles[-2] = candles[-2].model_copy(
        update={
            "open": Decimal("82"),
            "high": Decimal("91"),
            "low": Decimal("81"),
            "close": Decimal("90"),
        }
    )
    spec = {
        "family": "mean_reversion",
        "parameters": {
            "entry_model": "zscore_reentry",
            "mean_window": 20,
            "zscore_entry": "2",
            "rsi_oversold": "30",
            "rsi_overbought": "70",
        },
    }

    closed = candles[:-1]
    signal = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=closed,
        snapshot=snapshot_from_candles(closed),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert signal.direction == SignalDirection.LONG
    assert signal.features["zscore_reentry_confirmed"] is True
    assert signal.features["zscore_previous_close"] == Decimal("80")
    assert signal.features["zscore_previous"] <= Decimal("-2")
    assert signal.features["zscore"] > Decimal("-2")

    future_changed = list(candles)
    future_changed[-1] = future_changed[-1].model_copy(
        update={
            "open": Decimal("1"),
            "high": Decimal("1000"),
            "low": Decimal("1"),
            "close": Decimal("999"),
        }
    )
    unchanged = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=future_changed[:-1],
        snapshot=snapshot_from_candles(future_changed[:-1]),
        timeframe="1h",
        strategy_spec=spec,
    )
    assert unchanged.direction == signal.direction
    assert unchanged.features == signal.features

    no_reentry = list(closed)
    no_reentry[-1] = no_reentry[-1].model_copy(
        update={
            "open": Decimal("81"),
            "high": Decimal("83"),
            "low": Decimal("75"),
            "close": Decimal("79"),
        }
    )
    flat = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=no_reentry,
        snapshot=snapshot_from_candles(no_reentry),
        timeframe="1h",
        strategy_spec=spec,
    )
    assert flat.actionable is False
    assert flat.features["zscore_reentry_confirmed"] is False


def test_zscore_reentry_window_allows_gradual_but_single_causal_recovery():
    candles = [
        candle.model_copy(
            update={
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal("100"),
                "volume": Decimal("1000"),
            }
        )
        for candle in demo_candles(65)
    ]
    for index, close in zip((-4, -3, -2), (65, 80, 82), strict=True):
        value = Decimal(close)
        candles[index] = candles[index].model_copy(
            update={
                "open": value,
                "high": value + Decimal("1"),
                "low": value - Decimal("1"),
                "close": value,
            }
        )
    closed = candles[:-1]
    base_parameters = {
        "entry_model": "zscore_reentry",
        "mean_window": 20,
        "zscore_entry": "2",
        "rsi_oversold": "30",
        "rsi_overbought": "70",
    }

    exact = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=closed,
        snapshot=snapshot_from_candles(closed),
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {**base_parameters, "reentry_window": 1},
        },
    )
    gradual = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=closed,
        snapshot=snapshot_from_candles(closed),
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {**base_parameters, "reentry_window": 3},
        },
    )

    assert exact.actionable is False
    assert gradual.direction == SignalDirection.LONG
    assert gradual.features["zscore_extreme_bars_ago"] == "2"
    assert gradual.features["zscore_reentry_window"] == "3"


def test_production_confirmation_fails_closed_when_higher_sample_is_short():
    candles = demo_candles()
    short_confirmation = demo_candles(40)
    factory = StrategyFactory()

    research_signal = factory.evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        confirmation_candles=short_confirmation,
        require_confirmation=False,
    )
    production_signal = factory.evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        confirmation_candles=short_confirmation,
        require_confirmation=True,
    )

    assert research_signal.actionable is True
    assert production_signal.direction == SignalDirection.FLAT
    assert production_signal.confidence <= Decimal("0.35")
    assert "生产模式保持观望" in production_signal.reasons[-1]


def test_funding_contrarian_uses_current_snapshot_without_claiming_arbitrage():
    candles = actionable_candles(StrategyKind.MEAN_REVERSION)
    snapshot = snapshot_from_candles(candles).model_copy(
        update={
            "funding_rate": Decimal("-0.0004"),
            "funding_time": candles[-1].timestamp,
        }
    )

    signal = StrategyFactory().evaluate(
        StrategyKind.MEAN_REVERSION,
        candles=candles,
        snapshot=snapshot,
        timeframe="1h",
        strategy_spec={
            "family": "mean_reversion",
            "parameters": {
                "entry_model": "funding_contrarian",
                "funding_rate_threshold": "0.0003",
            },
        },
    )

    assert signal.direction == SignalDirection.LONG
    assert signal.features["funding_rate"] == Decimal("-0.0004")
    assert any("不是市场中性套利" in reason for reason in signal.reasons)


def test_funding_cost_filter_rejects_expensive_trend_direction():
    candles = actionable_candles(StrategyKind.TREND)
    snapshot = snapshot_from_candles(candles).model_copy(
        update={
            "funding_rate": Decimal("0.001"),
            "funding_time": candles[-1].timestamp,
        }
    )

    signal = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot,
        timeframe="1h",
        strategy_spec={
            "family": "trend",
            "parameters": {
                "funding_mode": "avoid_paying_extreme",
                "maximum_paying_funding_rate": "0.0002",
            },
        },
    )

    assert signal.direction == SignalDirection.FLAT
    assert signal.features["funding_filter_passed"] is False


def test_open_interest_expansion_filter_is_causal_and_fails_closed():
    candles = actionable_candles(StrategyKind.TREND)
    snapshot = snapshot_from_candles(candles)
    spec = {
        "family": "trend",
        "parameters": {
            "open_interest_mode": "confirm_expansion",
            "minimum_open_interest_change_24h": "0.02",
        },
    }

    missing = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot,
        timeframe="1h",
        strategy_spec=spec,
    )
    confirmed = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot.model_copy(
            update={
                "open_interest": Decimal("1100"),
                "open_interest_change_24h": Decimal("0.10"),
                "open_interest_time": candles[-1].timestamp,
            }
        ),
        timeframe="1h",
        strategy_spec=spec,
    )

    assert missing.direction == SignalDirection.FLAT
    assert missing.features["open_interest_filter_passed"] is False
    assert confirmed.actionable is True
    assert confirmed.features["open_interest_filter_passed"] is True


def test_confirmation_warmup_respects_adaptive_slow_ema_period():
    candles = demo_candles()
    confirmation = demo_candles(55)

    signal = StrategyFactory().evaluate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        timeframe="1h",
        confirmation_candles=confirmation,
        require_confirmation=True,
        strategy_spec={
            "family": "trend",
            "parameters": {
                "confirmation_ema_fast": 20,
                "confirmation_ema_slow": 60,
            },
        },
    )

    assert signal.direction == SignalDirection.FLAT
    assert signal.confidence <= Decimal("0.35")
    assert "不足 60 根" in signal.reasons[-1]
    assert signal.features["higher_timeframe_confirmed"] is False


def test_entry_warmup_respects_adaptive_slow_ema_period():
    candles = demo_candles(55)

    with pytest.raises(ValueError, match="至少需要 60 根"):
        StrategyFactory().evaluate(
            StrategyKind.TREND,
            candles=candles,
            snapshot=snapshot_from_candles(candles),
            timeframe="1h",
            strategy_spec={
                "family": "trend",
                "parameters": {
                    "ema_fast": 20,
                    "ema_slow": 60,
                },
            },
        )


def test_regime_selector_uses_trend_family_for_directional_market():
    assert select_strategy_kind(demo_candles()) == StrategyKind.TREND


def test_market_state_scoring_is_explainable_and_bounded():
    assessment = score_market_state(demo_candles())

    assert assessment.selected_kind == StrategyKind.TREND
    assert Decimal("0") <= assessment.confidence <= Decimal("1")
    assert set(assessment.scores) == {"trend", "mean_reversion", "breakout"}
    assert len(assessment.reasons) == 4


def test_automatic_ensemble_selects_confirmed_breakout():
    candles = actionable_candles(StrategyKind.BREAKOUT)
    plan = StrategyFactory().generate_automatic(
        candles=candles,
        snapshot=snapshot_from_candles(candles),
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=Decimal("1.8"),
    )

    assert plan.algorithm == StrategyKind.BREAKOUT
