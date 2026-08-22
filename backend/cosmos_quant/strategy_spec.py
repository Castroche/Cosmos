from __future__ import annotations

import json
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, computed_field, model_validator

from .configuration import StrategyKind


class SpecParameters(BaseModel):
    confirmation_ema_fast: int = Field(default=20, ge=5, le=100)
    confirmation_ema_slow: int = Field(default=50, ge=20, le=300)
    trend_strength_mode: Literal["ignore", "require_trend", "require_range"] = "ignore"
    adx_period: int = Field(default=14, ge=5, le=50)
    adx_threshold: Decimal = Field(default=Decimal("25"), ge=5, le=80)
    funding_mode: Literal["ignore", "avoid_paying_extreme"] = "ignore"
    maximum_paying_funding_rate: Decimal = Field(
        default=Decimal("0.0005"),
        ge=0,
        le=Decimal("0.01"),
    )
    open_interest_mode: Literal["ignore", "confirm_expansion"] = "ignore"
    minimum_open_interest_change_24h: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=Decimal("1"),
    )
    basis_mode: Literal["ignore", "avoid_crowded_direction"] = "ignore"
    maximum_directional_basis_rate: Decimal = Field(
        default=Decimal("0.003"),
        ge=0,
        le=Decimal("0.10"),
    )
    order_flow_mode: Literal["ignore", "confirm_direction"] = "ignore"
    minimum_order_flow_imbalance: Decimal = Field(
        default=Decimal("0.05"),
        ge=0,
        le=Decimal("1"),
    )
    liquidation_flow_mode: Literal[
        "ignore",
        "avoid_opposed_extreme",
        "confirm_direction",
    ] = "ignore"
    minimum_liquidation_flow_imbalance: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        le=Decimal("1"),
    )
    first_target_reward_risk: Decimal | None = Field(
        default=None,
        ge=Decimal("0.5"),
        le=Decimal("5"),
    )
    first_target_fraction: Decimal | None = Field(
        default=None,
        ge=Decimal("0.2"),
        le=Decimal("0.9"),
    )
    break_even_after_first_target: bool = False
    maximum_holding_bars: int | None = Field(default=None, ge=1, le=2000)

    @model_validator(mode="after")
    def validate_confirmation_periods(self):
        if self.confirmation_ema_fast >= self.confirmation_ema_slow:
            raise ValueError("confirmation fast EMA must be below slow EMA")
        return self


class TrendParameters(SpecParameters):
    entry_model: Literal[
        "ema_crossover",
        "sma_crossover",
        "macd_crossover",
        "roc_momentum",
        "price_ema_trend",
    ] = "ema_crossover"
    ema_fast: int = Field(default=20, ge=5, le=50)
    ema_slow: int = Field(default=50, ge=20, le=200)
    macd_signal: int = Field(default=9, ge=3, le=30)
    roc_period: int = Field(default=20, ge=3, le=200)
    roc_threshold_percent: Decimal = Field(default=Decimal("1"), ge=0, le=50)
    signal_gap_atr: Decimal = Field(default=Decimal("0.15"), ge=0, le=3)
    signal_slope_atr: Decimal = Field(default=Decimal("0.05"), ge=0, le=2)
    efficiency_window: int = Field(default=20, ge=5, le=100)
    minimum_efficiency_ratio: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    pullback_mode: Literal["none", "close_reclaim", "wick_reclaim"] = "none"
    pullback_tolerance_atr: Decimal = Field(default=Decimal("0.15"), ge=0, le=2)
    maximum_entry_extension_atr: Decimal = Field(default=Decimal("0.75"), gt=0, le=5)
    entry_zone_atr: Decimal = Field(default=Decimal("0.12"), gt=0, le=1)
    stop_atr: Decimal = Field(default=Decimal("1.6"), ge=Decimal("0.5"), le=5)
    reward_buffer: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    second_target_extension: Decimal = Field(default=Decimal("1.6"), ge=1, le=5)
    trailing_stop_atr: Decimal = Field(default=Decimal("1.2"), ge=Decimal("0.5"), le=5)
    trailing_activation: Literal["immediate", "first_target", "one_r"] = "first_target"

    @model_validator(mode="after")
    def validate_periods(self):
        if self.ema_fast >= self.ema_slow:
            raise ValueError("trend fast EMA must be below slow EMA")
        return self


class MeanReversionParameters(SpecParameters):
    entry_model: Literal[
        "zscore_rsi",
        "zscore_reentry",
        "bollinger_rsi",
        "bollinger_reentry",
        "rsi_reversal",
        "stochastic_reversal",
        "cci_reversal",
        "williams_reversal",
        "vwap_reversion",
        "funding_contrarian",
        "positioning_contrarian",
        "basis_contrarian",
    ] = "zscore_rsi"
    mean_window: int = Field(default=20, ge=10, le=100)
    rsi_period: int = Field(default=14, ge=5, le=50)
    zscore_entry: Decimal = Field(default=Decimal("1"), ge=Decimal("0.5"), le=4)
    band_deviations: Decimal = Field(default=Decimal("2"), ge=Decimal("0.5"), le=4)
    rsi_oversold: Decimal = Field(default=Decimal("40"), ge=5, le=49)
    rsi_overbought: Decimal = Field(default=Decimal("60"), ge=51, le=95)
    reentry_window: int = Field(default=1, ge=1, le=10)
    oscillator_period: int = Field(default=14, ge=5, le=50)
    oscillator_oversold: Decimal = Field(default=Decimal("20"), ge=-250, le=49)
    oscillator_overbought: Decimal = Field(default=Decimal("80"), ge=51, le=250)
    cci_threshold: Decimal = Field(default=Decimal("100"), ge=50, le=250)
    williams_oversold: Decimal = Field(default=Decimal("-80"), ge=-100, le=-50)
    williams_overbought: Decimal = Field(default=Decimal("-20"), ge=-50, le=0)
    vwap_deviation_atr: Decimal = Field(default=Decimal("1"), ge=Decimal("0.25"), le=5)
    funding_rate_threshold: Decimal = Field(
        default=Decimal("0.0001"),
        gt=0,
        le=Decimal("0.01"),
    )
    positioning_ratio_threshold: Decimal = Field(
        default=Decimal("1.5"),
        ge=Decimal("1.05"),
        le=Decimal("10"),
    )
    basis_rate_threshold: Decimal = Field(
        default=Decimal("0.0015"),
        gt=0,
        le=Decimal("0.10"),
    )
    entry_zone_atr: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    stop_atr: Decimal = Field(default=Decimal("1.4"), ge=Decimal("0.5"), le=5)
    reward_buffer: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    second_target_extension: Decimal = Field(default=Decimal("1.5"), ge=1, le=5)
    mean_completion_mode: Literal["minimum_reward", "mean_touch"] = "minimum_reward"
    trailing_stop_atr: Decimal | None = Field(
        default=None,
        ge=Decimal("0.5"),
        le=5,
    )
    trailing_activation: Literal["immediate", "first_target", "one_r"] = "first_target"

    @model_validator(mode="after")
    def validate_rsi_band(self):
        if self.rsi_oversold >= self.rsi_overbought:
            raise ValueError("RSI oversold threshold must be below overbought")
        return self


class BreakoutParameters(SpecParameters):
    entry_model: Literal[
        "donchian_volume",
        "donchian_retest",
        "bollinger_expansion",
        "atr_expansion",
        "high_low_breakout",
    ] = "donchian_volume"
    lookback: int = Field(default=20, ge=10, le=200)
    minimum_volume_ratio: Decimal = Field(default=Decimal("1.10"), ge=1, le=5)
    band_deviations: Decimal = Field(default=Decimal("2"), ge=Decimal("0.5"), le=4)
    minimum_bandwidth: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    minimum_range_atr: Decimal = Field(default=Decimal("1.25"), ge=Decimal("0.5"), le=10)
    retest_window: int = Field(default=5, ge=1, le=30)
    retest_tolerance_atr: Decimal = Field(default=Decimal("0.20"), ge=0, le=2)
    entry_zone_atr: Decimal = Field(default=Decimal("0.15"), gt=0, le=1)
    stop_atr: Decimal = Field(default=Decimal("1.5"), ge=Decimal("0.5"), le=5)
    reward_buffer: Decimal = Field(default=Decimal("0.02"), ge=0, le=1)
    second_target_extension: Decimal = Field(default=Decimal("1.6"), ge=1, le=5)
    trailing_stop_atr: Decimal = Field(default=Decimal("1.3"), ge=Decimal("0.5"), le=5)
    trailing_activation: Literal["immediate", "first_target", "one_r"] = "first_target"


class StrategySpecBase(BaseModel):
    version: int = Field(default=1, ge=1)
    source: Literal["system", "operator", "ai", "ml"] = "system"
    hypothesis: str = Field(default="", max_length=2000)

    @computed_field
    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"fingerprint"})
        parameters = payload.get("parameters", {})
        # Preserve fingerprints of pre-funding StrategySpecs. New optional
        # crypto context participates in identity only when it changes
        # behavior, so old persisted candidates remain immutable and valid.
        if parameters.get("funding_mode") == "ignore":
            parameters.pop("funding_mode", None)
            parameters.pop("maximum_paying_funding_rate", None)
        if parameters.get("trend_strength_mode") == "ignore":
            parameters.pop("trend_strength_mode", None)
            parameters.pop("adx_period", None)
            parameters.pop("adx_threshold", None)
        if parameters.get("entry_model") != "funding_contrarian":
            parameters.pop("funding_rate_threshold", None)
        if parameters.get("open_interest_mode") == "ignore":
            parameters.pop("open_interest_mode", None)
            parameters.pop("minimum_open_interest_change_24h", None)
        if parameters.get("basis_mode") == "ignore":
            parameters.pop("basis_mode", None)
            parameters.pop("maximum_directional_basis_rate", None)
        if parameters.get("order_flow_mode") == "ignore":
            parameters.pop("order_flow_mode", None)
            parameters.pop("minimum_order_flow_imbalance", None)
        if parameters.get("liquidation_flow_mode") == "ignore":
            parameters.pop("liquidation_flow_mode", None)
            parameters.pop("minimum_liquidation_flow_imbalance", None)
        if parameters.get("entry_model") != "positioning_contrarian":
            parameters.pop("positioning_ratio_threshold", None)
        if parameters.get("entry_model") != "basis_contrarian":
            parameters.pop("basis_rate_threshold", None)
        if parameters.get("first_target_reward_risk") is None:
            parameters.pop("first_target_reward_risk", None)
        if parameters.get("first_target_fraction") is None:
            parameters.pop("first_target_fraction", None)
        if not parameters.get("break_even_after_first_target", False):
            parameters.pop("break_even_after_first_target", None)
        if parameters.get("maximum_holding_bars") is None:
            parameters.pop("maximum_holding_bars", None)
        if payload.get("family") == StrategyKind.MEAN_REVERSION:
            if (
                parameters.get("entry_model") != "zscore_reentry"
                or parameters.get("reentry_window") == 1
            ):
                parameters.pop("reentry_window", None)
            if parameters.get("mean_completion_mode") == "minimum_reward":
                parameters.pop("mean_completion_mode", None)
            if parameters.get("trailing_stop_atr") is None:
                parameters.pop("trailing_stop_atr", None)
                parameters.pop("trailing_activation", None)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()[:16]


class TrendStrategySpec(StrategySpecBase):
    family: Literal[StrategyKind.TREND] = StrategyKind.TREND
    parameters: TrendParameters = Field(default_factory=TrendParameters)


class MeanReversionStrategySpec(StrategySpecBase):
    family: Literal[StrategyKind.MEAN_REVERSION] = StrategyKind.MEAN_REVERSION
    parameters: MeanReversionParameters = Field(default_factory=MeanReversionParameters)


class BreakoutStrategySpec(StrategySpecBase):
    family: Literal[StrategyKind.BREAKOUT] = StrategyKind.BREAKOUT
    parameters: BreakoutParameters = Field(default_factory=BreakoutParameters)


StrategySpec = Annotated[
    TrendStrategySpec | MeanReversionStrategySpec | BreakoutStrategySpec,
    Field(discriminator="family"),
]
STRATEGY_SPEC_ADAPTER = TypeAdapter(StrategySpec)


def default_strategy_spec(kind: StrategyKind) -> StrategySpec:
    if kind == StrategyKind.TREND:
        return TrendStrategySpec()
    if kind == StrategyKind.MEAN_REVERSION:
        return MeanReversionStrategySpec()
    return BreakoutStrategySpec()


def validate_strategy_spec(
    payload: StrategySpec | dict | None,
    expected_kind: StrategyKind | None = None,
) -> StrategySpec:
    spec = (
        default_strategy_spec(expected_kind)
        if payload is None and expected_kind is not None
        else STRATEGY_SPEC_ADAPTER.validate_python(payload)
    )
    if expected_kind is not None and spec.family != expected_kind:
        raise ValueError(f"strategy spec family {spec.family} does not match {expected_kind}")
    return spec


def minimum_signal_bars(spec: StrategySpec) -> int:
    """Return the causal warm-up required by a validated strategy spec."""

    parameters = spec.parameters
    strength_warmup = (
        parameters.adx_period * 2 + 1 if parameters.trend_strength_mode != "ignore" else 0
    )
    if isinstance(spec, TrendStrategySpec):
        model_warmup = parameters.ema_slow
        if parameters.entry_model == "macd_crossover":
            model_warmup = parameters.ema_slow + parameters.macd_signal - 1
        elif parameters.entry_model == "roc_momentum":
            model_warmup = max(model_warmup, parameters.roc_period + 1)
        return max(
            55,
            model_warmup,
            parameters.ema_fast + 5,
            parameters.efficiency_window + 1,
            strength_warmup,
            15,
        )
    if isinstance(spec, MeanReversionStrategySpec):
        reentry_model = parameters.entry_model in {
            "bollinger_reentry",
            "zscore_reentry",
        }
        oscillator_warmup = (
            parameters.oscillator_period + 2
            if parameters.entry_model
            in {
                "stochastic_reversal",
                "cci_reversal",
                "williams_reversal",
            }
            else 0
        )
        return max(
            55,
            parameters.mean_window
            + (parameters.reentry_window if reentry_model else 0),
            parameters.rsi_period
            + (parameters.reentry_window + 1 if reentry_model else 1),
            oscillator_warmup,
            strength_warmup,
        )
    breakout_warmup = parameters.lookback + 1
    if parameters.entry_model == "donchian_retest":
        breakout_warmup += parameters.retest_window
    return max(55, breakout_warmup, strength_warmup)


def strategy_spec_json_schema() -> dict:
    return STRATEGY_SPEC_ADAPTER.json_schema()
