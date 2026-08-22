from decimal import Decimal

import pytest
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.strategy_factory import StrategyFactory
from cosmos_quant.strategy_spec import (
    TrendStrategySpec,
    default_strategy_spec,
    validate_strategy_spec,
)
from pydantic import ValidationError


def test_default_strategy_spec_fingerprint_is_stable():
    first = default_strategy_spec(StrategyKind.TREND)
    second = default_strategy_spec(StrategyKind.TREND)

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != default_strategy_spec(StrategyKind.BREAKOUT).fingerprint


def test_optional_exit_defaults_preserve_mean_reversion_identity():
    baseline = default_strategy_spec(StrategyKind.MEAN_REVERSION)
    explicit_defaults = validate_strategy_spec(
        {
            "family": "mean_reversion",
            "parameters": {
                "maximum_holding_bars": None,
                "mean_completion_mode": "minimum_reward",
                "trailing_stop_atr": None,
                "trailing_activation": "first_target",
            },
        }
    )
    structural_exit = validate_strategy_spec(
        {
            "family": "mean_reversion",
            "parameters": {
                "maximum_holding_bars": 40,
                "mean_completion_mode": "mean_touch",
            },
        }
    )

    assert explicit_defaults.fingerprint == baseline.fingerprint
    assert structural_exit.fingerprint != baseline.fingerprint


def test_zscore_reentry_default_window_preserves_identity_but_expansion_changes_it():
    omitted = validate_strategy_spec(
        {
            "family": "mean_reversion",
            "parameters": {"entry_model": "zscore_reentry"},
        }
    )
    explicit_default = validate_strategy_spec(
        {
            "family": "mean_reversion",
            "parameters": {
                "entry_model": "zscore_reentry",
                "reentry_window": 1,
            },
        }
    )
    expanded = validate_strategy_spec(
        {
            "family": "mean_reversion",
            "parameters": {
                "entry_model": "zscore_reentry",
                "reentry_window": 3,
            },
        }
    )

    assert omitted.fingerprint == explicit_default.fingerprint
    assert expanded.fingerprint != omitted.fingerprint


def test_strategy_spec_rejects_invalid_period_order():
    with pytest.raises(ValidationError, match="fast EMA"):
        TrendStrategySpec(parameters={"ema_fast": 50, "ema_slow": 20})


def test_strategy_spec_family_must_match_requested_strategy():
    with pytest.raises(ValueError, match="does not match"):
        validate_strategy_spec(
            default_strategy_spec(StrategyKind.BREAKOUT),
            StrategyKind.TREND,
        )


def test_custom_strategy_spec_changes_deterministic_plan_levels():
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    factory = StrategyFactory()
    baseline = factory.generate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=Decimal("1.8"),
    )
    custom_spec = TrendStrategySpec(
        source="operator",
        hypothesis="更宽的波动率止损候选",
        parameters={"stop_atr": "2.2", "entry_zone_atr": "0.2"},
    )
    custom = factory.generate(
        StrategyKind.TREND,
        candles=candles,
        snapshot=snapshot,
        equity=Decimal("100000"),
        risk_fraction=Decimal("0.005"),
        exchange="okx",
        timeframe="1h",
        minimum_reward_risk=Decimal("1.8"),
        strategy_spec=custom_spec,
    )

    assert custom.strategy_spec_fingerprint == custom_spec.fingerprint
    assert custom.strategy_spec["source"] == "operator"
    assert custom.exit.stop_loss != baseline.exit.stop_loss
    assert custom.entry.zone != baseline.entry.zone
    assert custom.sizing.risk_amount == baseline.sizing.risk_amount
