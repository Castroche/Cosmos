from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from .configuration import StrategyKind
from .domain import (
    Candle,
    MarketRegime,
    MarketSnapshot,
    SignalDirection,
    StrategySignal,
)
from .indicators import (
    atr,
    average_directional_index,
    bollinger_bands,
    commodity_channel_index,
    ema,
    macd,
    rate_of_change,
    rsi,
    sma,
    standard_deviation,
    stochastic_oscillator,
    volume_weighted_average_price,
    williams_r,
)
from .strategy_spec import (
    BreakoutStrategySpec,
    MeanReversionStrategySpec,
    StrategySpec,
    TrendStrategySpec,
    minimum_signal_bars,
    validate_strategy_spec,
)


class NoTradeSignal(RuntimeError):
    def __init__(self, signal: StrategySignal):
        super().__init__("当前市场没有满足条件的可执行信号")
        self.signal = signal


class MarketStateAssessment(BaseModel):
    selected_kind: StrategyKind
    confidence: Decimal = Field(ge=0, le=1)
    ambiguous: bool
    scores: dict[str, Decimal]
    reasons: list[str]
    features: dict[str, Decimal | str | bool]
    primary_regime: str = "uncertain"
    trend_direction: str = "neutral"
    volatility_regime: str = "normal"
    transition_risk: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    preferred_archetypes: list[str] = Field(default_factory=list)
    avoided_archetypes: list[str] = Field(default_factory=list)


def _unit(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(value, Decimal("1")))


def score_market_state(candles: list[Candle]) -> MarketStateAssessment:
    """Score competing market structures from closed bars only."""

    if len(candles) < 55:
        raise ValueError("至少需要 55 根已收盘 K 线才能识别市场状态")
    closes = [candle.close for candle in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    volatility = atr(candles, 14)
    gap_atr = abs(fast[-1] - slow[-1]) / volatility if volatility else Decimal("0")
    slope_atr = abs(fast[-1] - fast[-6]) / volatility if volatility else Decimal("0")
    trend_score = _unit((gap_atr / Decimal("0.45") + slope_atr / Decimal("0.22")) / 2)
    window = candles[-21:-1]
    latest = candles[-1]
    average_volume = sum((item.volume for item in window), Decimal("0")) / Decimal(20)
    volume_ratio = latest.volume / average_volume if average_volume else Decimal("0")
    broke_range = latest.close > max(item.high for item in window) or latest.close < min(
        item.low for item in window
    )
    breakout_score = (
        _unit(Decimal("0.65") + (volume_ratio - Decimal("1")) / Decimal("2"))
        if broke_range
        else _unit(max(volume_ratio - Decimal("1"), Decimal("0")) / Decimal("3"))
    )
    mean = sma(closes, 20)
    deviation = standard_deviation(closes, 20)
    zscore = abs((latest.close - mean) / deviation) if deviation else Decimal("0")
    adx_value = average_directional_index(candles, 14)
    short_closes = closes[-21:]
    long_closes = closes[-101:]
    short_returns = [
        current / previous - Decimal("1")
        for previous, current in zip(short_closes[:-1], short_closes[1:], strict=True)
        if previous
    ]
    long_returns = [
        current / previous - Decimal("1")
        for previous, current in zip(long_closes[:-1], long_closes[1:], strict=True)
        if previous
    ]
    short_volatility = (
        Decimal(str(sum(float(value) ** 2 for value in short_returns) ** 0.5))
        if short_returns
        else Decimal("0")
    )
    long_volatility = (
        Decimal(str(sum(float(value) ** 2 for value in long_returns) ** 0.5))
        if long_returns
        else short_volatility
    )
    horizon_scale = Decimal(str((len(short_returns) / max(len(long_returns), 1)) ** 0.5))
    volatility_ratio = (
        short_volatility / (long_volatility * horizon_scale)
        if long_volatility and horizon_scale
        else Decimal("1")
    )
    volatility_regime = (
        "high"
        if volatility_ratio >= Decimal("1.35")
        else "low"
        if volatility_ratio <= Decimal("0.70")
        else "normal"
    )
    range_score = _unit(
        (Decimal("1") - trend_score) * Decimal("0.85")
        + min(zscore / Decimal("2"), Decimal("1")) * Decimal("0.15")
    )
    scores = {
        StrategyKind.TREND.value: trend_score.quantize(Decimal("0.001")),
        StrategyKind.MEAN_REVERSION.value: range_score.quantize(Decimal("0.001")),
        StrategyKind.BREAKOUT.value: breakout_score.quantize(Decimal("0.001")),
    }
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    selected_kind = StrategyKind(ordered[0][0])
    margin = ordered[0][1] - ordered[1][1]
    confidence = _unit(ordered[0][1] * Decimal("0.75") + margin * Decimal("0.25"))
    ambiguous = ordered[0][1] < Decimal("0.45") or margin < Decimal("0.08")
    trend_direction = (
        "long"
        if fast[-1] > slow[-1] and fast[-1] > fast[-6]
        else "short"
        if fast[-1] < slow[-1] and fast[-1] < fast[-6]
        else "neutral"
    )
    if broke_range and volatility_regime == "high":
        primary_regime = "volatility_breakout"
        preferred_archetypes = [
            "volatility_band_breakout",
            "range_expansion",
            "price_channel_breakout",
        ]
        avoided_archetypes = [
            "statistical_mean_reversion",
            "oscillator_reversal",
        ]
    elif trend_score >= Decimal("0.60") and adx_value >= Decimal("20"):
        primary_regime = "persistent_trend"
        preferred_archetypes = [
            "time_series_momentum",
            "moving_average_trend",
            "trend_pullback",
            "macd_momentum",
        ]
        avoided_archetypes = [
            "statistical_mean_reversion",
            "oscillator_reversal",
        ]
    elif trend_score <= Decimal("0.40") and not broke_range:
        primary_regime = "stable_range"
        preferred_archetypes = [
            "statistical_mean_reversion",
            "bollinger_reversion",
            "oscillator_reversal",
            "volume_weighted_reversion",
        ]
        avoided_archetypes = [
            "long_horizon_breakout",
            "range_expansion",
        ]
    else:
        primary_regime = "transition"
        preferred_archetypes = [
            "trend_pullback",
            "volatility_squeeze_release",
            "price_action_breakout",
        ]
        avoided_archetypes = []
    transition_risk = _unit(
        (Decimal("1") - margin) * (Decimal("0.65") if ambiguous else Decimal("0.35"))
        + (Decimal("0.20") if volatility_regime == "high" and not broke_range else Decimal("0"))
    )
    return MarketStateAssessment(
        selected_kind=selected_kind,
        confidence=confidence.quantize(Decimal("0.001")),
        ambiguous=ambiguous,
        scores=scores,
        reasons=[
            f"趋势结构评分 {scores['trend']:.3f}",
            f"均值回归结构评分 {scores['mean_reversion']:.3f}",
            f"突破结构评分 {scores['breakout']:.3f}",
            (
                "市场状态边界接近，降低自动决策确信度"
                if ambiguous
                else f"{selected_kind.value} 相对优势达到 {margin:.3f}"
            ),
        ],
        features={
            "ema_gap_atr": gap_atr,
            "ema_slope_atr_abs": slope_atr,
            "volume_ratio": volume_ratio,
            "absolute_zscore": zscore,
            "range_broken": broke_range,
            "adx14": adx_value,
            "short_long_volatility_ratio": volatility_ratio,
        },
        primary_regime=primary_regime,
        trend_direction=trend_direction,
        volatility_regime=volatility_regime,
        transition_risk=transition_risk.quantize(Decimal("0.001")),
        preferred_archetypes=preferred_archetypes,
        avoided_archetypes=avoided_archetypes,
    )


def select_strategy_kind(candles: list[Candle]) -> StrategyKind:
    return score_market_state(candles).selected_kind


def strategy_family_fit(
    assessment: MarketStateAssessment,
    kind: StrategyKind,
) -> tuple[bool, Decimal, Decimal, str]:
    """Apply the same bounded family-fit rule in live analysis and replay."""

    family_score = assessment.scores.get(kind.value, Decimal("0"))
    leading_score = max(assessment.scores.values(), default=Decimal("0"))
    relative_score = family_score / leading_score if leading_score > 0 else Decimal("0")
    exact_match = kind == assessment.selected_kind
    mixed_regime_support = family_score >= Decimal("0.45") and relative_score >= Decimal("0.60")
    supported = exact_match or mixed_regime_support
    reason = (
        f"{kind.value} 是当前结构首选家族"
        if exact_match
        else (
            f"{kind.value} 获得混合结构支持：绝对分 {family_score:f}，相对首选 {relative_score:.3f}"
            if mixed_regime_support
            else (
                f"{kind.value} 结构证据不足：绝对分 {family_score:f}，相对首选 {relative_score:.3f}"
            )
        )
    )
    return supported, family_score, relative_score, reason


def _confidence(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(value, Decimal("1"))).quantize(Decimal("0.001"))


def _base_signal(
    kind: StrategyKind,
    candles: list[Candle],
    snapshot: MarketSnapshot,
    timeframe: str,
    direction: SignalDirection,
    regime: MarketRegime,
    confidence: Decimal,
    reasons: list[str],
    features: dict[str, Decimal | str | bool],
) -> StrategySignal:
    return StrategySignal(
        algorithm=kind.value,
        symbol=snapshot.symbol,
        timeframe=timeframe,
        direction=direction,
        regime=regime,
        confidence=_confidence(confidence),
        signal_candle_at=candles[-1].timestamp,
        data_as_of=snapshot.timestamp,
        reasons=reasons,
        features=features,
    )


def evaluate_signal(
    kind: StrategyKind,
    *,
    candles: list[Candle],
    snapshot: MarketSnapshot,
    timeframe: str,
    allow_short: bool = True,
    confirmation_candles: list[Candle] | None = None,
    require_confirmation: bool = False,
    strategy_spec: StrategySpec | dict | None = None,
) -> StrategySignal:
    spec = validate_strategy_spec(strategy_spec, kind)
    required_bars = minimum_signal_bars(spec)
    if len(candles) < required_bars:
        raise ValueError(f"至少需要 {required_bars} 根已收盘 K 线才能评估当前策略规格")
    if isinstance(spec, TrendStrategySpec):
        signal = _evaluate_trend(candles, snapshot, timeframe, allow_short, spec)
    elif isinstance(spec, MeanReversionStrategySpec):
        signal = _evaluate_mean_reversion(candles, snapshot, timeframe, allow_short, spec)
    else:
        signal = _evaluate_breakout(candles, snapshot, timeframe, allow_short, spec)
    signal = _apply_trend_strength_filter(signal, candles, spec)
    signal = _apply_funding_filter(signal, snapshot, spec)
    signal = _apply_open_interest_filter(signal, snapshot, spec)
    signal = _apply_basis_filter(signal, snapshot, spec)
    signal = _apply_order_flow_filter(signal, snapshot, spec)
    signal = _apply_liquidation_flow_filter(signal, snapshot, spec)
    return _apply_confirmation_filter(signal, confirmation_candles, require_confirmation, spec)


def _apply_trend_strength_filter(
    signal: StrategySignal,
    candles: list[Candle],
    strategy_spec: StrategySpec,
) -> StrategySignal:
    mode = strategy_spec.parameters.trend_strength_mode
    features = {
        **signal.features,
        "trend_strength_mode": mode,
    }
    if mode == "ignore":
        return signal.model_copy(update={"features": features})
    adx_value = average_directional_index(
        candles,
        strategy_spec.parameters.adx_period,
    )
    threshold = strategy_spec.parameters.adx_threshold
    passed = adx_value >= threshold if mode == "require_trend" else adx_value <= threshold
    features.update(
        {
            "adx": adx_value,
            "adx_period": str(strategy_spec.parameters.adx_period),
            "adx_threshold": threshold,
            "trend_strength_filter_passed": passed,
        }
    )
    if not signal.actionable:
        return signal.model_copy(update={"features": features})
    if not passed:
        comparison = "不低于" if mode == "require_trend" else "不高于"
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"ADX {adx_value:.2f} 未满足{comparison} {threshold:.2f} 的状态门槛",
                ],
                "features": features,
            }
        )
    state = "趋势" if mode == "require_trend" else "震荡"
    return signal.model_copy(
        update={
            "reasons": [
                *signal.reasons,
                f"ADX {adx_value:.2f} 通过{state}状态过滤",
            ],
            "features": features,
        }
    )


def _apply_funding_filter(
    signal: StrategySignal,
    snapshot: MarketSnapshot,
    strategy_spec: StrategySpec,
) -> StrategySignal:
    funding_rate = snapshot.funding_rate
    features = {
        **signal.features,
        "funding_rate": funding_rate if funding_rate is not None else "unavailable",
        "funding_mode": strategy_spec.parameters.funding_mode,
        "funding_observed_at": (
            snapshot.funding_time.isoformat() if snapshot.funding_time else "unavailable"
        ),
    }
    if not signal.actionable or strategy_spec.parameters.funding_mode == "ignore":
        return signal.model_copy(update={"features": features})
    if funding_rate is None:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.35")),
                "reasons": [
                    *signal.reasons,
                    "策略要求历史资金费证据，当前时点不可用",
                ],
                "features": {**features, "funding_filter_passed": False},
            }
        )
    threshold = strategy_spec.parameters.maximum_paying_funding_rate
    paying_extreme = (signal.direction == SignalDirection.LONG and funding_rate > threshold) or (
        signal.direction == SignalDirection.SHORT and funding_rate < -threshold
    )
    if paying_extreme:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"当前方向需支付资金费 {funding_rate:.6f}，超过 {threshold:.6f}",
                ],
                "features": {**features, "funding_filter_passed": False},
            }
        )
    return signal.model_copy(
        update={
            "reasons": [*signal.reasons, "资金费成本过滤通过"],
            "features": {**features, "funding_filter_passed": True},
        }
    )


def _apply_open_interest_filter(
    signal: StrategySignal,
    snapshot: MarketSnapshot,
    strategy_spec: StrategySpec,
) -> StrategySignal:
    change = snapshot.open_interest_change_24h
    features = {
        **signal.features,
        "open_interest": (
            snapshot.open_interest if snapshot.open_interest is not None else "unavailable"
        ),
        "open_interest_value": (
            snapshot.open_interest_value
            if snapshot.open_interest_value is not None
            else "unavailable"
        ),
        "open_interest_change_24h": (change if change is not None else "unavailable"),
        "open_interest_mode": strategy_spec.parameters.open_interest_mode,
        "open_interest_observed_at": (
            snapshot.open_interest_time.isoformat()
            if snapshot.open_interest_time
            else "unavailable"
        ),
    }
    if not signal.actionable or strategy_spec.parameters.open_interest_mode == "ignore":
        return signal.model_copy(update={"features": features})
    if change is None:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.35")),
                "reasons": [
                    *signal.reasons,
                    "策略要求因果持仓量变化证据，当前时点不可用",
                ],
                "features": {
                    **features,
                    "open_interest_filter_passed": False,
                },
            }
        )
    threshold = strategy_spec.parameters.minimum_open_interest_change_24h
    if change < threshold:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"24小时持仓量变化 {change:.2%} 未达到扩张门槛 {threshold:.2%}",
                ],
                "features": {
                    **features,
                    "open_interest_filter_passed": False,
                },
            }
        )
    return signal.model_copy(
        update={
            "reasons": [
                *signal.reasons,
                f"24小时持仓量扩张 {change:.2%}，趋势确认过滤通过",
            ],
            "features": {
                **features,
                "open_interest_filter_passed": True,
            },
        }
    )


def _apply_basis_filter(
    signal: StrategySignal,
    snapshot: MarketSnapshot,
    strategy_spec: StrategySpec,
) -> StrategySignal:
    basis_rate = snapshot.basis_rate
    features = {
        **signal.features,
        "basis_rate": basis_rate if basis_rate is not None else "unavailable",
        "basis_mode": strategy_spec.parameters.basis_mode,
        "basis_observed_at": (
            snapshot.basis_time.isoformat() if snapshot.basis_time else "unavailable"
        ),
    }
    if not signal.actionable or strategy_spec.parameters.basis_mode == "ignore":
        return signal.model_copy(update={"features": features})
    if basis_rate is None:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.35")),
                "reasons": [
                    *signal.reasons,
                    "Required causal futures-basis evidence is unavailable",
                ],
                "features": {**features, "basis_filter_passed": False},
            }
        )
    threshold = strategy_spec.parameters.maximum_directional_basis_rate
    crowded = (signal.direction == SignalDirection.LONG and basis_rate > threshold) or (
        signal.direction == SignalDirection.SHORT and basis_rate < -threshold
    )
    if crowded:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"Directional basis {basis_rate:.4%} exceeds crowding limit {threshold:.4%}",
                ],
                "features": {**features, "basis_filter_passed": False},
            }
        )
    return signal.model_copy(
        update={
            "reasons": [*signal.reasons, "Causal futures-basis crowding filter passed"],
            "features": {**features, "basis_filter_passed": True},
        }
    )


def _apply_order_flow_filter(
    signal: StrategySignal,
    snapshot: MarketSnapshot,
    strategy_spec: StrategySpec,
) -> StrategySignal:
    imbalance = snapshot.order_flow_imbalance
    features = {
        **signal.features,
        "order_flow_imbalance": imbalance if imbalance is not None else "unavailable",
        "order_flow_mode": strategy_spec.parameters.order_flow_mode,
        "order_flow_observed_at": (
            snapshot.order_flow_time.isoformat() if snapshot.order_flow_time else "unavailable"
        ),
    }
    if not signal.actionable or strategy_spec.parameters.order_flow_mode == "ignore":
        return signal.model_copy(update={"features": features})
    if imbalance is None:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.35")),
                "reasons": [
                    *signal.reasons,
                    "Required closed-bar taker-flow evidence is unavailable",
                ],
                "features": {**features, "order_flow_filter_passed": False},
            }
        )
    threshold = strategy_spec.parameters.minimum_order_flow_imbalance
    aligned = (signal.direction == SignalDirection.LONG and imbalance >= threshold) or (
        signal.direction == SignalDirection.SHORT and imbalance <= -threshold
    )
    if not aligned:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"Closed-bar taker-flow imbalance {imbalance:.3f} does not confirm direction",
                ],
                "features": {**features, "order_flow_filter_passed": False},
            }
        )
    return signal.model_copy(
        update={
            "reasons": [*signal.reasons, "Closed-bar taker-flow direction confirmed"],
            "features": {**features, "order_flow_filter_passed": True},
        }
    )


def _apply_liquidation_flow_filter(
    signal: StrategySignal,
    snapshot: MarketSnapshot,
    strategy_spec: StrategySpec,
) -> StrategySignal:
    imbalance = snapshot.liquidation_flow_imbalance
    mode = strategy_spec.parameters.liquidation_flow_mode
    features = {
        **signal.features,
        "liquidation_flow_imbalance": (
            imbalance if imbalance is not None else "unavailable"
        ),
        "liquidation_flow_mode": mode,
        "liquidation_flow_observed_at": (
            snapshot.liquidation_flow_time.isoformat()
            if snapshot.liquidation_flow_time
            else "unavailable"
        ),
        "liquidation_source_market_type": (
            snapshot.liquidation_source_market_type or "unavailable"
        ),
        "liquidation_source_instrument_id": (
            snapshot.liquidation_source_instrument_id or "unavailable"
        ),
        "liquidation_evidence_role": snapshot.liquidation_evidence_role or "unavailable",
    }
    if not signal.actionable or mode == "ignore":
        return signal.model_copy(update={"features": features})
    if (
        imbalance is None
        or snapshot.liquidation_source_market_type != "coin_m_inverse_perpetual"
        or snapshot.liquidation_evidence_role
        != "cross_contract_underlying_liquidation_context"
    ):
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.35")),
                "reasons": [
                    *signal.reasons,
                    "策略要求已验证的跨合约爆仓上下文，当前时点不可用",
                ],
                "features": {**features, "liquidation_flow_filter_passed": False},
            }
        )
    threshold = strategy_spec.parameters.minimum_liquidation_flow_imbalance
    aligned = (signal.direction == SignalDirection.LONG and imbalance >= threshold) or (
        signal.direction == SignalDirection.SHORT and imbalance <= -threshold
    )
    opposed = (signal.direction == SignalDirection.LONG and imbalance <= -threshold) or (
        signal.direction == SignalDirection.SHORT and imbalance >= threshold
    )
    passed = aligned if mode == "confirm_direction" else not opposed
    if not passed:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"COIN-M 跨合约爆仓失衡 {imbalance:.3f} 未通过 {mode} 门槛",
                ],
                "features": {**features, "liquidation_flow_filter_passed": False},
            }
        )
    return signal.model_copy(
        update={
            "reasons": [*signal.reasons, "COIN-M 跨合约爆仓上下文过滤通过"],
            "features": {**features, "liquidation_flow_filter_passed": True},
        }
    )


def _apply_confirmation_filter(
    signal: StrategySignal,
    confirmation_candles: list[Candle] | None,
    require_confirmation: bool,
    strategy_spec: StrategySpec,
) -> StrategySignal:
    if not signal.actionable:
        return signal
    if confirmation_candles is None:
        if not require_confirmation:
            return signal
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.35")),
                "reasons": [*signal.reasons, "生产模式要求高周期确认，当前周期无法提供"],
                "features": {
                    **signal.features,
                    "higher_timeframe_confirmed": False,
                },
            }
        )
    confirmation_fast = strategy_spec.parameters.confirmation_ema_fast
    confirmation_slow = strategy_spec.parameters.confirmation_ema_slow
    required_confirmation_bars = max(55, confirmation_slow)
    if len(confirmation_candles) < required_confirmation_bars:
        if require_confirmation:
            return signal.model_copy(
                update={
                    "direction": SignalDirection.FLAT,
                    "confidence": min(signal.confidence, Decimal("0.35")),
                    "reasons": [
                        *signal.reasons,
                        f"高周期已收盘 K 线不足 {required_confirmation_bars} 根，生产模式保持观望",
                    ],
                    "features": {
                        **signal.features,
                        "higher_timeframe_confirmed": False,
                    },
                }
            )
        return signal.model_copy(
            update={
                "confidence": min(signal.confidence, Decimal("0.60")),
                "reasons": [
                    *signal.reasons,
                    "高周期已收盘 K 线不足 "
                    f"{required_confirmation_bars} 根，本次仅使用入场周期信号",
                ],
                "features": {
                    **signal.features,
                    "higher_timeframe_confirmed": False,
                },
            }
        )
    closes = [candle.close for candle in confirmation_candles]
    fast = ema(closes, confirmation_fast)[-1]
    slow = ema(closes, confirmation_slow)[-1]
    confirmation_features = {
        "higher_timeframe_ema_fast": fast,
        "higher_timeframe_ema_slow": slow,
        "higher_timeframe_ema_fast_period": str(confirmation_fast),
        "higher_timeframe_ema_slow_period": str(confirmation_slow),
        f"higher_timeframe_ema{confirmation_fast}": fast,
        f"higher_timeframe_ema{confirmation_slow}": slow,
    }
    aligned = (signal.direction == SignalDirection.LONG and fast > slow) or (
        signal.direction == SignalDirection.SHORT and fast < slow
    )
    if not aligned:
        return signal.model_copy(
            update={
                "direction": SignalDirection.FLAT,
                "confidence": min(signal.confidence, Decimal("0.49")),
                "reasons": [
                    *signal.reasons,
                    f"高周期 EMA{confirmation_fast}/EMA{confirmation_slow} 方向不一致，保持观望",
                ],
                "features": {
                    **signal.features,
                    **confirmation_features,
                    "higher_timeframe_confirmed": False,
                },
            }
        )
    return signal.model_copy(
        update={
            "confidence": min(signal.confidence + Decimal("0.08"), Decimal("1")),
            "reasons": [
                *signal.reasons,
                f"高周期 EMA{confirmation_fast}/EMA{confirmation_slow} 与入场方向一致",
            ],
            "features": {
                **signal.features,
                **confirmation_features,
                "higher_timeframe_confirmed": True,
            },
        }
    )


def _evaluate_trend(
    candles: list[Candle],
    snapshot: MarketSnapshot,
    timeframe: str,
    allow_short: bool,
    strategy_spec: TrendStrategySpec,
) -> StrategySignal:
    closes = [candle.close for candle in candles]
    parameters = strategy_spec.parameters
    fast = ema(closes, parameters.ema_fast)
    slow = ema(closes, parameters.ema_slow)
    volatility = atr(candles, 14)
    gap_atr = abs(fast[-1] - slow[-1]) / volatility if volatility else Decimal("0")
    slope_atr = (fast[-1] - fast[-6]) / volatility if volatility else Decimal("0")
    efficiency_closes = closes[-(parameters.efficiency_window + 1) :]
    efficiency_path = sum(
        (
            abs(current - previous)
            for previous, current in zip(
                efficiency_closes,
                efficiency_closes[1:],
                strict=False,
            )
        ),
        Decimal("0"),
    )
    efficiency_ratio = (
        abs(efficiency_closes[-1] - efficiency_closes[0]) / efficiency_path
        if efficiency_path
        else Decimal("0")
    )
    signal_price = closes[-1]
    previous_price = closes[-2]
    if fast[-1] > slow[-1]:
        entry_extension_atr = (signal_price - fast[-1]) / volatility if volatility else Decimal("0")
    else:
        entry_extension_atr = (fast[-1] - signal_price) / volatility if volatility else Decimal("0")
    if parameters.pullback_mode == "close_reclaim":
        bullish_entry_event = previous_price <= fast[-2] and signal_price > fast[-1]
        bearish_entry_event = previous_price >= fast[-2] and signal_price < fast[-1]
    elif parameters.pullback_mode == "wick_reclaim":
        tolerance = volatility * parameters.pullback_tolerance_atr
        bullish_entry_event = (
            candles[-2].low <= fast[-2] + tolerance
            and signal_price > fast[-1]
            and signal_price > previous_price
        )
        bearish_entry_event = (
            candles[-2].high >= fast[-2] - tolerance
            and signal_price < fast[-1]
            and signal_price < previous_price
        )
    else:
        bullish_entry_event = True
        bearish_entry_event = True
    model_features: dict[str, Decimal | str | bool] = {
        "entry_model": parameters.entry_model,
    }
    if parameters.entry_model == "sma_crossover":
        fast_value = sma(closes, parameters.ema_fast)
        slow_value = sma(closes, parameters.ema_slow)
        bullish = fast_value > slow_value
        bearish = fast_value < slow_value
        model_strength = abs(fast_value - slow_value) / volatility if volatility else Decimal("0")
        model_features.update({"sma_fast": fast_value, "sma_slow": slow_value})
    elif parameters.entry_model == "macd_crossover":
        momentum = macd(
            closes,
            parameters.ema_fast,
            parameters.ema_slow,
            parameters.macd_signal,
        )
        bullish = momentum["line"] > momentum["signal"] and momentum["line"] > 0
        bearish = momentum["line"] < momentum["signal"] and momentum["line"] < 0
        model_strength = abs(momentum["histogram"]) / volatility if volatility else Decimal("0")
        model_features.update(
            {
                "macd_line": momentum["line"],
                "macd_signal": momentum["signal"],
                "macd_histogram": momentum["histogram"],
            }
        )
    elif parameters.entry_model == "roc_momentum":
        momentum = rate_of_change(closes, parameters.roc_period)
        bullish = momentum >= parameters.roc_threshold_percent and closes[-1] > slow[-1]
        bearish = momentum <= -parameters.roc_threshold_percent and closes[-1] < slow[-1]
        model_strength = abs(momentum) / Decimal("10")
        model_features.update(
            {
                "roc_percent": momentum,
                "roc_period": str(parameters.roc_period),
            }
        )
    elif parameters.entry_model == "price_ema_trend":
        bullish = closes[-1] > slow[-1] and slope_atr > 0
        bearish = closes[-1] < slow[-1] and slope_atr < 0
        model_strength = abs(closes[-1] - slow[-1]) / volatility if volatility else Decimal("0")
        model_features["price_ema_distance_atr"] = model_strength
    else:
        bullish = fast[-1] > slow[-1] and slope_atr > 0
        bearish = fast[-1] < slow[-1] and slope_atr < 0
        model_strength = gap_atr
    structural_strength = (
        model_strength >= parameters.signal_gap_atr
        if parameters.entry_model in {"ema_crossover", "sma_crossover", "price_ema_trend"}
        else True
    )
    strong_enough = (
        structural_strength
        and abs(slope_atr) >= parameters.signal_slope_atr
        and efficiency_ratio >= parameters.minimum_efficiency_ratio
        and entry_extension_atr <= parameters.maximum_entry_extension_atr
    )
    if strong_enough and bullish and bullish_entry_event:
        direction = SignalDirection.LONG
    elif strong_enough and bearish and bearish_entry_event and allow_short:
        direction = SignalDirection.SHORT
    else:
        direction = SignalDirection.FLAT
    confidence = (gap_atr / Decimal("0.75") + abs(slope_atr) / Decimal("0.35")) / 2
    if parameters.minimum_efficiency_ratio > 0:
        confidence = (
            confidence * Decimal("2") + min(efficiency_ratio / Decimal("0.50"), Decimal("1"))
        ) / Decimal("3")
    reasons = [
        f"EMA{parameters.ema_fast} 与 EMA{parameters.ema_slow} 距离为 "
        f"{gap_atr:.3f} ATR（门槛 {parameters.signal_gap_atr:.3f}）",
        f"EMA{parameters.ema_fast} 近 5 个观测斜率为 {slope_atr:.3f} ATR"
        f"（绝对门槛 {parameters.signal_slope_atr:.3f}）",
        f"{parameters.efficiency_window} 周期方向效率为 {efficiency_ratio:.3f}"
        f"（门槛 {parameters.minimum_efficiency_ratio:.3f}）",
        f"入场事件模式 {parameters.pullback_mode}，相对快线延伸 "
        f"{entry_extension_atr:.3f} ATR（上限 {parameters.maximum_entry_extension_atr:.3f}）",
    ]
    if direction == SignalDirection.FLAT:
        reasons.append("趋势强度或方向一致性不足，保持观望")
    return _base_signal(
        StrategyKind.TREND,
        candles,
        snapshot,
        timeframe,
        direction,
        MarketRegime.TREND if direction != SignalDirection.FLAT else MarketRegime.UNCERTAIN,
        confidence if direction != SignalDirection.FLAT else min(confidence, Decimal("0.49")),
        reasons,
        {
            **model_features,
            "ema_fast": fast[-1],
            "ema_slow": slow[-1],
            "ema_fast_period": str(parameters.ema_fast),
            "ema_slow_period": str(parameters.ema_slow),
            "atr14": volatility,
            "ema_gap_atr": gap_atr,
            "ema_slope_atr": slope_atr,
            "trend_efficiency_window": str(parameters.efficiency_window),
            "trend_efficiency_ratio": efficiency_ratio,
            "trend_pullback_mode": parameters.pullback_mode,
            "trend_entry_event_confirmed": (
                bullish_entry_event if bullish else bearish_entry_event
            ),
            "trend_entry_extension_atr": entry_extension_atr,
        },
    )


def _evaluate_mean_reversion(
    candles: list[Candle],
    snapshot: MarketSnapshot,
    timeframe: str,
    allow_short: bool,
    strategy_spec: MeanReversionStrategySpec,
) -> StrategySignal:
    closes = [candle.close for candle in candles]
    parameters = strategy_spec.parameters
    mean = sma(closes, parameters.mean_window)
    deviation = standard_deviation(closes, parameters.mean_window)
    momentum = rsi(closes, parameters.rsi_period)
    signal_price = closes[-1]
    zscore = (signal_price - mean) / deviation if deviation else Decimal("0")
    volatility = atr(candles, 14)
    model_features: dict[str, Decimal | str | bool] = {
        "entry_model": parameters.entry_model,
    }
    long_condition = zscore <= -parameters.zscore_entry and momentum <= parameters.rsi_oversold
    short_condition = zscore >= parameters.zscore_entry and momentum >= parameters.rsi_overbought
    if parameters.entry_model == "zscore_reentry":
        current_index = len(closes) - 1
        search_start = max(
            parameters.mean_window - 1,
            current_index - parameters.reentry_window,
        )
        extreme_direction = SignalDirection.FLAT
        extreme_price = Decimal("0")
        extreme_zscore = Decimal("0")
        extreme_momentum = Decimal("50")
        extreme_bars_ago = 0
        for event_index in range(search_start, current_index):
            event_closes = closes[: event_index + 1]
            event_mean = sma(event_closes, parameters.mean_window)
            event_deviation = standard_deviation(
                event_closes,
                parameters.mean_window,
            )
            event_price = event_closes[-1]
            event_zscore = (
                (event_price - event_mean) / event_deviation
                if event_deviation
                else Decimal("0")
            )
            event_momentum = rsi(event_closes, parameters.rsi_period)
            if (
                event_zscore <= -parameters.zscore_entry
                and event_momentum <= parameters.rsi_oversold
            ):
                extreme_direction = SignalDirection.LONG
            elif (
                event_zscore >= parameters.zscore_entry
                and event_momentum >= parameters.rsi_overbought
            ):
                extreme_direction = SignalDirection.SHORT
            else:
                continue
            extreme_price = event_price
            extreme_zscore = event_zscore
            extreme_momentum = event_momentum
            extreme_bars_ago = current_index - event_index
        immediate_closes = closes[:-1]
        immediate_mean = sma(immediate_closes, parameters.mean_window)
        immediate_deviation = standard_deviation(
            immediate_closes,
            parameters.mean_window,
        )
        immediate_zscore = (
            (immediate_closes[-1] - immediate_mean) / immediate_deviation
            if immediate_deviation
            else Decimal("0")
        )
        immediate_momentum = rsi(immediate_closes, parameters.rsi_period)
        previous_long_recovered = bool(
            immediate_zscore > -parameters.zscore_entry
            and immediate_momentum > parameters.rsi_oversold
        )
        previous_short_recovered = bool(
            immediate_zscore < parameters.zscore_entry
            and immediate_momentum < parameters.rsi_overbought
        )
        long_condition = bool(
            extreme_direction == SignalDirection.LONG
            and zscore > -parameters.zscore_entry
            and signal_price < mean
            and momentum > parameters.rsi_oversold
            and not previous_long_recovered
        )
        short_condition = bool(
            extreme_direction == SignalDirection.SHORT
            and zscore < parameters.zscore_entry
            and signal_price > mean
            and momentum < parameters.rsi_overbought
            and not previous_short_recovered
        )
        model_features.update(
            {
                "zscore_previous": extreme_zscore,
                "zscore_previous_close": extreme_price,
                "zscore_previous_rsi": extreme_momentum,
                "zscore_extreme_bars_ago": str(extreme_bars_ago),
                "zscore_reentry_window": str(parameters.reentry_window),
                "zscore_reentry_confirmed": long_condition or short_condition,
            }
        )
    elif parameters.entry_model == "bollinger_rsi":
        bands = bollinger_bands(
            closes,
            parameters.mean_window,
            parameters.band_deviations,
        )
        long_condition = signal_price <= bands["lower"] and momentum <= parameters.rsi_oversold
        short_condition = signal_price >= bands["upper"] and momentum >= parameters.rsi_overbought
        model_features.update(
            {
                "bollinger_lower": bands["lower"],
                "bollinger_upper": bands["upper"],
                "bollinger_percent_b": bands["percent_b"],
            }
        )
    elif parameters.entry_model == "bollinger_reentry":
        previous_closes = closes[:-1]
        previous_bands = bollinger_bands(
            previous_closes,
            parameters.mean_window,
            parameters.band_deviations,
        )
        bands = bollinger_bands(
            closes,
            parameters.mean_window,
            parameters.band_deviations,
        )
        previous_momentum = rsi(previous_closes, parameters.rsi_period)
        previous_price = previous_closes[-1]
        long_condition = bool(
            previous_price <= previous_bands["lower"]
            and previous_momentum <= parameters.rsi_oversold
            and signal_price > bands["lower"]
            and signal_price < mean
            and momentum > parameters.rsi_oversold
        )
        short_condition = bool(
            previous_price >= previous_bands["upper"]
            and previous_momentum >= parameters.rsi_overbought
            and signal_price < bands["upper"]
            and signal_price > mean
            and momentum < parameters.rsi_overbought
        )
        model_features.update(
            {
                "bollinger_lower": bands["lower"],
                "bollinger_upper": bands["upper"],
                "bollinger_percent_b": bands["percent_b"],
                "bollinger_previous_lower": previous_bands["lower"],
                "bollinger_previous_upper": previous_bands["upper"],
                "bollinger_previous_close": previous_price,
                "bollinger_previous_rsi": previous_momentum,
                "bollinger_reentry_confirmed": long_condition or short_condition,
            }
        )
    elif parameters.entry_model == "rsi_reversal":
        long_condition = momentum <= parameters.rsi_oversold
        short_condition = momentum >= parameters.rsi_overbought
    elif parameters.entry_model == "stochastic_reversal":
        oscillator = stochastic_oscillator(
            candles,
            parameters.oscillator_period,
        )
        long_condition = oscillator["k"] <= parameters.oscillator_oversold
        short_condition = oscillator["k"] >= parameters.oscillator_overbought
        model_features.update({"stochastic_k": oscillator["k"], "stochastic_d": oscillator["d"]})
    elif parameters.entry_model == "cci_reversal":
        oscillator = commodity_channel_index(candles, parameters.oscillator_period)
        long_condition = oscillator <= -parameters.cci_threshold
        short_condition = oscillator >= parameters.cci_threshold
        model_features["cci"] = oscillator
    elif parameters.entry_model == "williams_reversal":
        oscillator = williams_r(candles, parameters.oscillator_period)
        long_condition = oscillator <= parameters.williams_oversold
        short_condition = oscillator >= parameters.williams_overbought
        model_features["williams_r"] = oscillator
    elif parameters.entry_model == "vwap_reversion":
        vwap = volume_weighted_average_price(candles, parameters.mean_window)
        distance_atr = (signal_price - vwap) / volatility if volatility else Decimal("0")
        long_condition = distance_atr <= -parameters.vwap_deviation_atr
        short_condition = distance_atr >= parameters.vwap_deviation_atr
        model_features.update({"vwap": vwap, "vwap_distance_atr": distance_atr})
    elif parameters.entry_model == "funding_contrarian":
        funding_rate = snapshot.funding_rate
        threshold = parameters.funding_rate_threshold
        long_condition = bool(
            funding_rate is not None and funding_rate <= -threshold and momentum <= Decimal("55")
        )
        short_condition = bool(
            funding_rate is not None and funding_rate >= threshold and momentum >= Decimal("45")
        )
        model_features.update(
            {
                "funding_rate": (funding_rate if funding_rate is not None else "unavailable"),
                "funding_rate_threshold": threshold,
                "funding_contrarian_data_available": funding_rate is not None,
            }
        )
    elif parameters.entry_model == "positioning_contrarian":
        ratio = snapshot.long_short_ratio
        threshold = parameters.positioning_ratio_threshold
        inverse_threshold = Decimal("1") / threshold
        long_condition = bool(ratio is not None and ratio <= inverse_threshold)
        short_condition = bool(ratio is not None and ratio >= threshold)
        model_features.update(
            {
                "long_short_ratio": ratio if ratio is not None else "unavailable",
                "positioning_ratio_threshold": threshold,
                "positioning_data_available": ratio is not None,
            }
        )
    elif parameters.entry_model == "basis_contrarian":
        basis_rate = snapshot.basis_rate
        threshold = parameters.basis_rate_threshold
        long_condition = bool(basis_rate is not None and basis_rate <= -threshold)
        short_condition = bool(basis_rate is not None and basis_rate >= threshold)
        model_features.update(
            {
                "basis_rate": basis_rate if basis_rate is not None else "unavailable",
                "basis_rate_threshold": threshold,
                "basis_data_available": basis_rate is not None,
            }
        )
    if long_condition:
        direction = SignalDirection.LONG
    elif short_condition and allow_short:
        direction = SignalDirection.SHORT
    else:
        direction = SignalDirection.FLAT
    confidence = min(abs(zscore) / Decimal("2"), Decimal("1"))
    if parameters.entry_model == "funding_contrarian":
        funding_rate = snapshot.funding_rate
        confidence = (
            min(
                abs(funding_rate) / parameters.funding_rate_threshold,
                Decimal("1"),
            )
            if funding_rate is not None
            else Decimal("0")
        )
    elif parameters.entry_model == "positioning_contrarian":
        ratio = snapshot.long_short_ratio
        confidence = (
            min(
                max(ratio, Decimal("1") / ratio) / parameters.positioning_ratio_threshold,
                Decimal("1"),
            )
            if ratio is not None and ratio > 0
            else Decimal("0")
        )
    elif parameters.entry_model == "basis_contrarian":
        basis_rate = snapshot.basis_rate
        confidence = (
            min(abs(basis_rate) / parameters.basis_rate_threshold, Decimal("1"))
            if basis_rate is not None
            else Decimal("0")
        )
    reasons = [
        f"收盘价相对 {parameters.mean_window} 周期均值偏离 {zscore:.3f} 个标准差",
        f"{parameters.rsi_period} 周期 RSI 为 {momentum:.1f}",
    ]
    if parameters.entry_model == "funding_contrarian":
        reasons.append(
            "资金费拥挤反转：负费率偏向做多、正费率偏向做空；这是方向性假设，不是市场中性套利"
        )
    elif parameters.entry_model == "positioning_contrarian":
        reasons.append(
            "Positioning contrarian: fade only causally observed extreme global long/short ratios"
        )
    elif parameters.entry_model == "basis_contrarian":
        reasons.append(
            "Basis contrarian: fade only causally observed extreme perpetual mark/index basis"
        )
    if direction == SignalDirection.FLAT:
        if parameters.entry_model == "bollinger_reentry":
            reasons.append("前一根极端偏离尚未在当前收盘完成波动带与 RSI 双重再进入，保持观望")
        elif parameters.entry_model == "zscore_reentry":
            reasons.append("前一根统计极端尚未在当前收盘完成 Z-Score 与 RSI 双重重返，保持观望")
        else:
            reasons.append("价格偏离与 RSI 未同时达到反转门槛，保持观望")
    elif parameters.entry_model == "bollinger_reentry":
        reasons.append("前一根极端偏离后，当前收盘完成波动带与 RSI 双重再进入确认")
    elif parameters.entry_model == "zscore_reentry":
        reasons.append("前一根统计极端后，当前收盘完成 Z-Score 与 RSI 双重重返确认")
    return _base_signal(
        StrategyKind.MEAN_REVERSION,
        candles,
        snapshot,
        timeframe,
        direction,
        MarketRegime.RANGE,
        confidence if direction != SignalDirection.FLAT else min(confidence, Decimal("0.49")),
        reasons,
        {
            **model_features,
            "mean": mean,
            "mean_window": str(parameters.mean_window),
            "standard_deviation": deviation,
            "zscore": zscore,
            "rsi14": momentum,
        },
    )


def _evaluate_breakout(
    candles: list[Candle],
    snapshot: MarketSnapshot,
    timeframe: str,
    allow_short: bool,
    strategy_spec: BreakoutStrategySpec,
) -> StrategySignal:
    parameters = strategy_spec.parameters
    window = candles[-(parameters.lookback + 1) : -1]
    signal = candles[-1]
    upper = max(candle.high for candle in window)
    lower = min(candle.low for candle in window)
    average_volume = sum((candle.volume for candle in window), Decimal("0")) / Decimal(len(window))
    volume_ratio = signal.volume / average_volume if average_volume else Decimal("0")
    volume_confirmed = (
        volume_ratio >= parameters.minimum_volume_ratio
        if parameters.entry_model != "high_low_breakout"
        else True
    )
    long_condition = signal.close > upper and volume_confirmed
    short_condition = signal.close < lower and volume_confirmed
    model_features: dict[str, Decimal | str | bool] = {
        "entry_model": parameters.entry_model,
    }
    if parameters.entry_model == "donchian_retest":
        volatility = atr(candles[:-1], 14)
        tolerance = volatility * parameters.retest_tolerance_atr
        recent_start = max(parameters.lookback, len(candles) - 1 - parameters.retest_window)
        breakout_direction = SignalDirection.FLAT
        breakout_level = Decimal("0")
        breakout_volume_ratio = Decimal("0")
        breakout_bars_ago = 0
        for event_index in range(recent_start, len(candles) - 1):
            event_window = candles[event_index - parameters.lookback : event_index]
            event = candles[event_index]
            event_upper = max(candle.high for candle in event_window)
            event_lower = min(candle.low for candle in event_window)
            event_average_volume = sum(
                (candle.volume for candle in event_window),
                Decimal("0"),
            ) / Decimal(len(event_window))
            event_volume_ratio = (
                event.volume / event_average_volume
                if event_average_volume
                else Decimal("0")
            )
            if (
                event.close > event_upper
                and event_volume_ratio >= parameters.minimum_volume_ratio
            ):
                breakout_direction = SignalDirection.LONG
                breakout_level = event_upper
                breakout_volume_ratio = event_volume_ratio
                breakout_bars_ago = len(candles) - 1 - event_index
            elif (
                event.close < event_lower
                and event_volume_ratio >= parameters.minimum_volume_ratio
            ):
                breakout_direction = SignalDirection.SHORT
                breakout_level = event_lower
                breakout_volume_ratio = event_volume_ratio
                breakout_bars_ago = len(candles) - 1 - event_index
        long_condition = bool(
            breakout_direction == SignalDirection.LONG
            and abs(signal.low - breakout_level) <= tolerance
            and signal.close > breakout_level
            and signal.close > signal.open
        )
        short_condition = bool(
            breakout_direction == SignalDirection.SHORT
            and abs(signal.high - breakout_level) <= tolerance
            and signal.close < breakout_level
            and signal.close < signal.open
        )
        volume_ratio = breakout_volume_ratio
        volume_confirmed = breakout_direction != SignalDirection.FLAT
        model_features.update(
            {
                "breakout_retest_level": breakout_level,
                "breakout_retest_bars_ago": str(breakout_bars_ago),
                "breakout_retest_tolerance_atr": parameters.retest_tolerance_atr,
                "breakout_event_volume_ratio": breakout_volume_ratio,
                "breakout_retest_confirmed": long_condition or short_condition,
            }
        )
    elif parameters.entry_model == "bollinger_expansion":
        closes = [candle.close for candle in candles]
        bands = bollinger_bands(
            closes[:-1],
            parameters.lookback,
            parameters.band_deviations,
        )
        bandwidth_confirmed = bands["bandwidth"] >= parameters.minimum_bandwidth
        long_condition = signal.close > bands["upper"] and volume_confirmed and bandwidth_confirmed
        short_condition = signal.close < bands["lower"] and volume_confirmed and bandwidth_confirmed
        model_features.update(
            {
                "bollinger_upper": bands["upper"],
                "bollinger_lower": bands["lower"],
                "bollinger_bandwidth": bands["bandwidth"],
                "bandwidth_confirmed": bandwidth_confirmed,
            }
        )
    elif parameters.entry_model == "atr_expansion":
        volatility = atr(candles[:-1], 14)
        signal_range_atr = (signal.high - signal.low) / volatility if volatility else Decimal("0")
        long_condition = (
            signal.close > signal.open
            and signal_range_atr >= parameters.minimum_range_atr
            and volume_confirmed
        )
        short_condition = (
            signal.close < signal.open
            and signal_range_atr >= parameters.minimum_range_atr
            and volume_confirmed
        )
        model_features["signal_range_atr"] = signal_range_atr
    if long_condition:
        direction = SignalDirection.LONG
    elif short_condition and allow_short:
        direction = SignalDirection.SHORT
    else:
        direction = SignalDirection.FLAT
    if signal.close > upper:
        distance = (signal.close - upper) / max(upper - lower, Decimal("0.00000001"))
    elif signal.close < lower:
        distance = (lower - signal.close) / max(upper - lower, Decimal("0.00000001"))
    else:
        distance = Decimal("0")
    confidence = min(
        distance * Decimal("8") + max(volume_ratio - Decimal("1"), Decimal("0")),
        Decimal("1"),
    )
    reasons = [
        f"过去 {parameters.lookback} 根 K 线区间为 {lower:f}–{upper:f}",
        f"最新已收盘 K 线成交量为区间均量的 {volume_ratio:.2f} 倍"
        f"（门槛 {parameters.minimum_volume_ratio:.2f}）",
    ]
    if direction == SignalDirection.FLAT:
        reasons.append("尚未形成带成交量确认的收盘突破，保持观望")
    return _base_signal(
        StrategyKind.BREAKOUT,
        candles,
        snapshot,
        timeframe,
        direction,
        MarketRegime.BREAKOUT if direction != SignalDirection.FLAT else MarketRegime.RANGE,
        confidence if direction != SignalDirection.FLAT else min(confidence, Decimal("0.49")),
        reasons,
        {
            **model_features,
            "donchian_upper": upper,
            "donchian_lower": lower,
            "donchian_lookback": str(parameters.lookback),
            "volume_ratio": volume_ratio,
            "volume_confirmed": volume_confirmed,
        },
    )
