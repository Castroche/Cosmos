from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from math import sqrt

from pydantic import BaseModel, Field

from .domain import Candle, MarketSnapshot
from .timeframes import TIMEFRAME_SECONDS


def _bounded(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(value, upper))


def _return_series(candles: list[Candle], length: int) -> list[float]:
    closes = candles[-(length + 1) :]
    return [
        float((current.close - previous.close) / previous.close)
        for previous, current in zip(closes, closes[1:], strict=False)
        if previous.close > 0
    ]


def _realized_volatility(candles: list[Candle], length: int) -> Decimal:
    values = _return_series(candles, length)
    if len(values) < 2:
        return Decimal("0")
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return Decimal(str(sqrt(max(variance, 0))))


def _horizon_return(
    candles: list[Candle],
    timeframe: str,
    hours: int,
) -> Decimal | None:
    interval = TIMEFRAME_SECONDS.get(timeframe)
    if not interval:
        return None
    bars = max(1, hours * 60 * 60 // interval)
    if len(candles) <= bars or candles[-(bars + 1)].close <= 0:
        return None
    return (candles[-1].close - candles[-(bars + 1)].close) / candles[-(bars + 1)].close


class MarketEvidenceSnapshot(BaseModel):
    symbol: str
    timeframe: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data_as_of: datetime
    realized_volatility_20: Decimal
    realized_volatility_100: Decimal
    volatility_ratio: Decimal
    volatility_regime: str
    volume_ratio_20: Decimal
    range_position_50: Decimal
    one_bar_return: Decimal
    order_book_imbalance: Decimal | None = None
    bid_depth_quote: Decimal | None = None
    ask_depth_quote: Decimal | None = None
    trade_flow_imbalance: Decimal | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None
    open_interest_change_4h: Decimal | None = None
    open_interest_change_24h: Decimal | None = None
    funding_rate: Decimal | None = None
    basis_rate: Decimal | None = None
    long_short_ratio: Decimal | None = None
    positioning_regime: str = "unavailable"
    price_change_4h: Decimal | None = None
    price_change_24h: Decimal | None = None
    derivatives_regime: str = "unavailable"
    derivatives_confirmation: str = "neutral"
    derivatives_strength: Decimal = Decimal("0")
    microstructure_status: str = "unavailable"
    directional_bias: str = "neutral"
    directional_strength: Decimal = Decimal("0")
    reasons: list[str] = Field(default_factory=list)


def build_market_evidence(
    candles: list[Candle],
    market: MarketSnapshot,
    timeframe: str,
    microstructure: dict | None = None,
) -> MarketEvidenceSnapshot:
    if len(candles) < 21:
        raise ValueError("market evidence requires at least 21 closed candles")
    vol20 = _realized_volatility(candles, 20)
    vol100 = _realized_volatility(candles, min(100, len(candles) - 1))
    volatility_ratio = vol20 / vol100 if vol100 > 0 else Decimal("1")
    volatility_regime = (
        "high"
        if volatility_ratio >= Decimal("1.5")
        else "low"
        if volatility_ratio <= Decimal("0.65")
        else "normal"
    )
    recent_volume = candles[-20:]
    average_volume = sum((item.volume for item in recent_volume[:-1]), Decimal("0")) / Decimal(
        max(len(recent_volume) - 1, 1)
    )
    volume_ratio = recent_volume[-1].volume / average_volume if average_volume > 0 else Decimal("0")
    range_candles = candles[-50:]
    low = min(item.low for item in range_candles)
    high = max(item.high for item in range_candles)
    range_position = (candles[-1].close - low) / (high - low) if high > low else Decimal("0.5")
    one_bar_return = (
        (candles[-1].close - candles[-2].close) / candles[-2].close
        if candles[-2].close > 0
        else Decimal("0")
    )
    price_change_4h = _horizon_return(candles, timeframe, 4)
    price_change_24h = _horizon_return(candles, timeframe, 24)

    microstructure = microstructure or {}
    order_book = microstructure.get("order_book_imbalance")
    trade_flow = microstructure.get("trade_flow_imbalance")
    oi_change_4h = microstructure.get("open_interest_change_4h")
    oi_change_24h = microstructure.get("open_interest_change_24h")
    long_short_ratio = microstructure.get("long_short_ratio", market.long_short_ratio)
    normalized_long_short = Decimal(str(long_short_ratio)) if long_short_ratio is not None else None
    positioning_regime = (
        "long_crowded"
        if normalized_long_short is not None and normalized_long_short >= Decimal("1.5")
        else "short_crowded"
        if normalized_long_short is not None and normalized_long_short <= Decimal("0.667")
        else "balanced"
        if normalized_long_short is not None
        else "unavailable"
    )
    derivatives_regime = "unavailable"
    derivatives_confirmation = "neutral"
    derivatives_strength = Decimal("0")
    oi_change = (
        Decimal(str(oi_change_4h))
        if oi_change_4h is not None
        else Decimal(str(oi_change_24h))
        if oi_change_24h is not None
        else None
    )
    price_change = price_change_4h or price_change_24h
    if oi_change is not None and price_change is not None:
        if oi_change <= Decimal("-0.01"):
            derivatives_regime = "deleveraging"
        elif oi_change >= Decimal("0.01") and abs(price_change) >= Decimal("0.002"):
            derivatives_confirmation = "long" if price_change > 0 else "short"
            derivatives_regime = (
                "long_build" if derivatives_confirmation == "long" else "short_build"
            )
            derivatives_strength = min(
                Decimal("1"),
                abs(oi_change) / Decimal("0.05"),
            ) * min(
                Decimal("1"),
                abs(price_change) / Decimal("0.03"),
            )
        else:
            derivatives_regime = "stable"
    score = (range_position - Decimal("0.5")) * Decimal("0.20")
    score += _bounded(
        one_bar_return * Decimal("100") * min(volume_ratio, Decimal("2")),
        Decimal("-1"),
        Decimal("1"),
    ) * Decimal("0.15")
    weight = Decimal("0.35")
    if order_book is not None:
        score += Decimal(str(order_book)) * Decimal("0.35")
        weight += Decimal("0.35")
    if trade_flow is not None:
        score += Decimal(str(trade_flow)) * Decimal("0.30")
        weight += Decimal("0.30")
    normalized_score = _bounded(
        score / weight if weight > 0 else Decimal("0"),
        Decimal("-1"),
        Decimal("1"),
    )
    directional_bias = (
        "long"
        if normalized_score >= Decimal("0.15")
        else "short"
        if normalized_score <= Decimal("-0.15")
        else "neutral"
    )
    reasons = [
        f"20/100 实现波动率比值 {volatility_ratio:.2f}，状态 {volatility_regime}",
        f"最新成交量为 20 根均量的 {volume_ratio:.2f}x",
        f"价格位于近 50 根区间的 {range_position * 100:.1f}%",
    ]
    if order_book is not None:
        reasons.append(f"订单簿失衡 {Decimal(str(order_book)):.3f}")
    if trade_flow is not None:
        reasons.append(f"主动成交流失衡 {Decimal(str(trade_flow)):.3f}")
    if microstructure.get("open_interest") is not None:
        reasons.append(f"当前持仓量 {Decimal(str(microstructure['open_interest'])):f}")
    if oi_change_4h is not None:
        reasons.append(f"4h 持仓量变化 {Decimal(str(oi_change_4h)) * 100:+.2f}%")
    if oi_change_24h is not None:
        reasons.append(f"24h 持仓量变化 {Decimal(str(oi_change_24h)) * 100:+.2f}%")
    if derivatives_regime != "unavailable":
        reasons.append(
            f"衍生品参与状态 {derivatives_regime}，"
            f"方向确认 {derivatives_confirmation}，"
            f"强度 {derivatives_strength:.3f}"
        )
    if market.funding_rate is not None:
        reasons.append(f"Perpetual funding rate {market.funding_rate:.6%}")
    if market.basis_rate is not None:
        reasons.append(f"Mark/index basis {market.basis_rate:.4%}")
    if normalized_long_short is not None:
        reasons.append(f"Global long/short ratio {normalized_long_short:.3f}, {positioning_regime}")
    return MarketEvidenceSnapshot(
        symbol=market.symbol,
        timeframe=timeframe,
        data_as_of=market.timestamp,
        realized_volatility_20=vol20,
        realized_volatility_100=vol100,
        volatility_ratio=volatility_ratio,
        volatility_regime=volatility_regime,
        volume_ratio_20=volume_ratio,
        range_position_50=range_position,
        one_bar_return=one_bar_return,
        order_book_imbalance=order_book,
        bid_depth_quote=microstructure.get("bid_depth_quote"),
        ask_depth_quote=microstructure.get("ask_depth_quote"),
        trade_flow_imbalance=trade_flow,
        open_interest=microstructure.get("open_interest"),
        open_interest_value=microstructure.get("open_interest_value"),
        open_interest_change_4h=oi_change_4h,
        open_interest_change_24h=oi_change_24h,
        funding_rate=market.funding_rate,
        basis_rate=market.basis_rate,
        long_short_ratio=normalized_long_short,
        positioning_regime=positioning_regime,
        price_change_4h=price_change_4h,
        price_change_24h=price_change_24h,
        derivatives_regime=derivatives_regime,
        derivatives_confirmation=derivatives_confirmation,
        derivatives_strength=derivatives_strength,
        microstructure_status=str(microstructure.get("status") or "unavailable"),
        directional_bias=directional_bias,
        directional_strength=abs(normalized_score),
        reasons=reasons,
    )
