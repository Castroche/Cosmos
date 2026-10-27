from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from math import sqrt
from typing import Any

from .domain import Candle, MarketSnapshot
from .indicators import (
    atr,
    average_directional_index,
    ema,
    macd,
    rate_of_change,
    rsi,
    volume_weighted_average_price,
)
from .trading_intelligence import (
    V2_TIMEFRAMES,
    FeatureGroup,
    FeatureHealth,
    MarketRegimeV2,
    MarketStateV2,
)


def _returns(candles: list[Candle], period: int) -> list[float]:
    window = candles[-(period + 1) :]
    return [
        float(current.close / previous.close - Decimal("1"))
        for previous, current in zip(window, window[1:], strict=False)
        if previous.close > 0
    ]


def _realized_volatility(candles: list[Candle], period: int) -> Decimal:
    values = _returns(candles, period)
    if len(values) < 2:
        raise ValueError("not enough returns")
    mean = sum(values) / len(values)
    variance = sum((item - mean) ** 2 for item in values) / (len(values) - 1)
    return Decimal(str(sqrt(max(variance, 0))))


def _correlation(left: list[float], right: list[float]) -> Decimal | None:
    size = min(len(left), len(right))
    if size < 3:
        return None
    lhs = left[-size:]
    rhs = right[-size:]
    left_mean = sum(lhs) / size
    right_mean = sum(rhs) / size
    covariance = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(lhs, rhs, strict=True)
    )
    left_variance = sum((item - left_mean) ** 2 for item in lhs)
    right_variance = sum((item - right_mean) ** 2 for item in rhs)
    denominator = sqrt(left_variance * right_variance)
    return Decimal(str(covariance / denominator)) if denominator else None


def _safe(function, *args):
    try:
        return function(*args)
    except (ValueError, ArithmeticError, IndexError):
        return None


def _timeframe_features(candles: list[Candle]) -> tuple[dict[str, Any], list[str]]:
    if len(candles) < 2:
        return {}, ["fewer than two closed candles"]
    closes = [item.close for item in candles]
    values: dict[str, Any] = {
        "close": closes[-1],
        "return_1": closes[-1] / closes[-2] - Decimal("1") if closes[-2] else None,
    }
    missing: list[str] = []
    for period in (20, 50, 200):
        series = _safe(ema, closes, period)
        if series:
            values[f"ema_{period}"] = series[-1]
            values[f"ema_{period}_slope"] = (
                series[-1] / series[-2] - Decimal("1") if len(series) > 1 and series[-2] else None
            )
        else:
            missing.append(f"ema_{period}")
    indicator_calls = {
        "adx_14": (average_directional_index, candles, 14),
        "rsi_14": (rsi, closes, 14),
        "roc_12": (rate_of_change, closes, 12),
        "atr_14": (atr, candles, 14),
        "vwap_20": (volume_weighted_average_price, candles, 20),
        "realized_volatility_20": (_realized_volatility, candles, 20),
        "realized_volatility_100": (_realized_volatility, candles, 100),
    }
    for name, call in indicator_calls.items():
        result = _safe(call[0], *call[1:])
        if result is None:
            missing.append(name)
        else:
            values[name] = result
    macd_values = _safe(macd, closes)
    if macd_values:
        values.update({f"macd_{key}": value for key, value in macd_values.items()})
    else:
        missing.append("macd")
    current_roc = values.get("roc_12")
    previous_roc = _safe(rate_of_change, closes[:-1], 12)
    if current_roc is not None and previous_roc is not None:
        values["momentum_acceleration"] = current_roc - previous_roc
    current_rsi = values.get("rsi_14")
    prior_rsi = _safe(rsi, closes[:-5], 14)
    if current_rsi is not None and prior_rsi is not None and len(closes) >= 20:
        values["bullish_momentum_divergence"] = (
            closes[-1] < min(closes[-10:-1]) and current_rsi > prior_rsi
        )
        values["bearish_momentum_divergence"] = (
            closes[-1] > max(closes[-10:-1]) and current_rsi < prior_rsi
        )
    vol20 = values.get("realized_volatility_20")
    vol100 = values.get("realized_volatility_100")
    if vol20 is not None and vol100:
        ratio = vol20 / vol100
        values["volatility_ratio_20_100"] = ratio
        values["volatility_compression"] = ratio <= Decimal("0.65")
        values["volatility_expansion"] = ratio >= Decimal("1.5")
        observations = [
            sample
            for end in range(21, len(candles) + 1)
            if (sample := _safe(_realized_volatility, candles[:end], 20)) is not None
        ][-100:]
        if observations:
            values["realized_volatility_percentile"] = Decimal(
                sum(item <= vol20 for item in observations)
            ) / Decimal(len(observations))
    volume_window = candles[-20:]
    if len(volume_window) >= 2:
        baseline = sum((item.volume for item in volume_window[:-1]), Decimal("0")) / Decimal(
            len(volume_window) - 1
        )
        values["relative_volume_20"] = volume_window[-1].volume / baseline if baseline else None
        prior_window = volume_window[:-1]
        prior_baseline = sum((item.volume for item in prior_window[:-1]), Decimal("0")) / Decimal(
            max(len(prior_window) - 1, 1)
        )
        prior_ratio = prior_window[-1].volume / prior_baseline if prior_baseline else None
        if prior_ratio is not None and values["relative_volume_20"] is not None:
            values["volume_acceleration"] = values["relative_volume_20"] - prior_ratio
    else:
        missing.append("volume ratios")
    return {key: value for key, value in values.items() if value is not None}, missing


def _structure(candles: list[Candle]) -> tuple[dict[str, Any], list[str]]:
    if len(candles) < 20:
        return {}, ["structure requires at least 20 closed candles"]
    recent = candles[-20:]
    prior = candles[-40:-20] if len(candles) >= 40 else candles[:-20]
    recent_high = max(item.high for item in recent)
    recent_low = min(item.low for item in recent)
    prior_high = max((item.high for item in prior), default=recent_high)
    prior_low = min((item.low for item in prior), default=recent_low)
    higher_high = recent_high > prior_high
    higher_low = recent_low > prior_low
    lower_high = recent_high < prior_high
    lower_low = recent_low < prior_low
    close = candles[-1].close
    return (
        {
            "higher_high": higher_high,
            "higher_low": higher_low,
            "lower_high": lower_high,
            "lower_low": lower_low,
            "support": recent_low,
            "resistance": recent_high,
            "breakout_up": close > prior_high,
            "breakout_down": close < prior_low,
            "retest_support": recent_low <= close <= recent_low * Decimal("1.003"),
            "retest_resistance": recent_high * Decimal("0.997") <= close <= recent_high,
            "structure_invalidated": (
                (higher_high and close < prior_low)
                or (lower_low and close > prior_high)
            ),
            "in_range": recent_low <= close <= recent_high,
            "structure": (
                "HH_HL"
                if higher_high and higher_low
                else "LH_LL"
                if lower_high and lower_low
                else "MIXED"
            ),
        },
        [],
    )


def _regime(features: dict[str, Any], structure: dict[str, Any]) -> MarketRegimeV2:
    close = features.get("close")
    ema20 = features.get("ema_20")
    ema50 = features.get("ema_50")
    ema200 = features.get("ema_200")
    adx = features.get("adx_14")
    vol20 = features.get("realized_volatility_20")
    vol100 = features.get("realized_volatility_100")
    if None in {close, ema20, ema50, adx}:
        return MarketRegimeV2.TRANSITION
    volatility_ratio = vol20 / vol100 if vol20 is not None and vol100 else Decimal("1")
    one_return = abs(features.get("return_1", Decimal("0")))
    if one_return >= Decimal("0.04") and volatility_ratio >= Decimal("1.8"):
        return MarketRegimeV2.PANIC
    if volatility_ratio >= Decimal("1.5"):
        return MarketRegimeV2.VOLATILITY_EXPANSION
    if volatility_ratio <= Decimal("0.65") and adx < Decimal("20"):
        return MarketRegimeV2.COMPRESSION
    trend_up = close > ema20 > ema50 and (ema200 is None or ema50 > ema200)
    trend_down = close < ema20 < ema50 and (ema200 is None or ema50 < ema200)
    if adx >= Decimal("30") and trend_up:
        return MarketRegimeV2.STRONG_TREND_UP
    if adx >= Decimal("30") and trend_down:
        return MarketRegimeV2.STRONG_TREND_DOWN
    if trend_up:
        return MarketRegimeV2.TREND_UP
    if trend_down:
        return MarketRegimeV2.TREND_DOWN
    if adx < Decimal("20"):
        return MarketRegimeV2.RANGE
    if structure.get("structure") in {"HH_HL", "LH_LL"}:
        return MarketRegimeV2.REVERSAL
    return MarketRegimeV2.TRANSITION


def _group(values: dict[str, Any], missing: list[str]) -> FeatureGroup:
    if not values:
        return FeatureGroup(
            health=FeatureHealth.UNAVAILABLE,
            reasons=missing or ["no source data"],
        )
    return FeatureGroup(
        health=FeatureHealth.DEGRADED if missing else FeatureHealth.HEALTHY,
        values=values,
        reasons=missing,
    )


def build_market_state_v2(
    *,
    market: MarketSnapshot,
    candles_by_timeframe: dict[str, list[Candle]],
    quality_by_timeframe: dict[str, dict[str, Any]] | None = None,
    microstructure: dict[str, Any] | None = None,
    cross_market: dict[str, Any] | None = None,
    normalized_events: list[dict[str, Any]] | None = None,
    received_at: datetime | None = None,
) -> MarketStateV2:
    """Build a hashable state from causal inputs; absent features remain absent."""

    received = received_at or datetime.now(UTC)
    timeframe_features: dict[str, dict[str, Any]] = {}
    timeframe_missing: dict[str, list[str]] = {}
    for timeframe in V2_TIMEFRAMES:
        candles = candles_by_timeframe.get(timeframe, [])
        values, missing = _timeframe_features(candles)
        if values:
            timeframe_features[timeframe] = values
        if missing:
            timeframe_missing[timeframe] = missing

    reference_timeframe = "1h" if "1h" in timeframe_features else "5m"
    reference = timeframe_features.get(reference_timeframe, {})
    structure_values, structure_missing = _structure(
        candles_by_timeframe.get(reference_timeframe, [])
    )
    quality = quality_by_timeframe or {}
    quality_issues = [
        f"{timeframe}:{payload.get('status', 'unknown')}"
        for timeframe, payload in quality.items()
        if payload.get("status") != "healthy"
    ]
    absent_timeframes = [item for item in V2_TIMEFRAMES if item not in timeframe_features]
    market_missing = [*quality_issues]
    if absent_timeframes:
        market_missing.append(f"missing timeframes: {','.join(absent_timeframes)}")
    latency_ms = max(int((received - market.timestamp).total_seconds() * 1000), 0)
    market_values: dict[str, Any] = {
        "bid": market.bid,
        "ask": market.ask,
        "last": market.last,
        "spread_bps": market.spread_bps,
        "latency_ms": latency_ms,
        "source": market.source,
    }
    trend_keys = (
        "ema_20",
        "ema_20_slope",
        "ema_50",
        "ema_50_slope",
        "ema_200",
        "ema_200_slope",
        "adx_14",
        "vwap_20",
    )
    momentum_keys = (
        "rsi_14",
        "roc_12",
        "macd_line",
        "macd_signal",
        "macd_histogram",
        "momentum_acceleration",
        "bullish_momentum_divergence",
        "bearish_momentum_divergence",
    )
    volatility_keys = (
        "atr_14",
        "realized_volatility_20",
        "realized_volatility_100",
        "volatility_ratio_20_100",
        "realized_volatility_percentile",
        "volatility_compression",
        "volatility_expansion",
    )
    volume_keys = ("relative_volume_20", "volume_acceleration")
    micro = microstructure or {}
    micro_values = {
        key: micro[key]
        for key in (
            "order_book_imbalance",
            "trade_flow_imbalance",
            "trade_intensity",
            "bid_depth_quote",
            "ask_depth_quote",
            "estimated_slippage_bps",
            "active_buy_quote",
            "active_sell_quote",
            "liquidity_quote",
        )
        if micro.get(key) is not None
    }
    derivative_source = {
        "funding_rate": market.funding_rate,
        "open_interest": market.open_interest,
        "open_interest_value": market.open_interest_value,
        "open_interest_change_24h": market.open_interest_change_24h,
        "basis_rate": market.basis_rate,
        "long_short_ratio": market.long_short_ratio,
        "liquidation_flow_imbalance": market.liquidation_flow_imbalance,
    }
    derivative_values = {
        key: value for key, value in derivative_source.items() if value is not None
    }
    event_items = normalized_events or []
    event_values: dict[str, Any] = {
        "active_risk_event_count": len(event_items),
        "new_risk_blocked": any(bool(item.get("blocks_new_risk")) for item in event_items),
    }
    if event_items:
        event_values["active_event_types"] = ",".join(
            sorted({str(item.get("event_type")) for item in event_items})
        )
    regime = _regime(reference, structure_values)
    return MarketStateV2(
        symbol=market.symbol,
        data_as_of=market.timestamp,
        regime=regime,
        market=_group(market_values, market_missing),
        structure=_group(structure_values, structure_missing),
        trend=_group(
            {key: reference[key] for key in trend_keys if key in reference},
            [
                item
                for item in timeframe_missing.get(reference_timeframe, [])
                if item.startswith(("ema", "adx", "vwap"))
            ],
        ),
        momentum=_group(
            {key: reference[key] for key in momentum_keys if key in reference},
            [
                item
                for item in timeframe_missing.get(reference_timeframe, [])
                if item.startswith(("rsi", "roc", "macd"))
            ],
        ),
        volatility=_group(
            {key: reference[key] for key in volatility_keys if key in reference},
            [
                item
                for item in timeframe_missing.get(reference_timeframe, [])
                if item.startswith(("atr", "realized"))
            ],
        ),
        volume=_group(
            {key: reference[key] for key in volume_keys if key in reference},
            [
                item
                for item in timeframe_missing.get(reference_timeframe, [])
                if item == "volume ratios"
            ],
        ),
        microstructure=_group(
            micro_values,
            []
            if len(micro_values) >= 6
            else ["partial or unavailable order-book/trade-flow inputs"],
        ),
        derivatives=_group(
            derivative_values,
            []
            if len(derivative_values) == len(derivative_source)
            else ["partial derivatives context"],
        ),
        cross_market=_group(
            cross_market or {}, [] if cross_market else ["cross-market inputs unavailable"]
        ),
        events=_group(
            event_values,
            ["ETF flow unavailable: no reliable official free real-time source"],
        ),
        timeframe_features=timeframe_features,
    )


def build_cross_market_context(
    candles_by_symbol: dict[str, list[Candle]],
) -> dict[str, Any]:
    """Create BTC regime, relative pairs, correlation and breadth from closed 1h bars."""

    btc_symbol = "BTC/USDT:USDT"
    btc = candles_by_symbol.get(btc_symbol, [])
    if len(btc) < 3:
        return {}
    btc_features, _ = _timeframe_features(btc)
    btc_structure, _ = _structure(btc)
    values: dict[str, Any] = {"btc_regime": str(_regime(btc_features, btc_structure))}
    btc_returns = _returns(btc, 24)
    positive = 0
    observed = 0
    for symbol, key in (
        ("ETH/USDT:USDT", "eth"),
        ("SOL/USDT:USDT", "sol"),
    ):
        candles = candles_by_symbol.get(symbol, [])
        if len(candles) < 3 or candles[-1].close <= 0:
            continue
        values[f"{key}_btc_ratio"] = candles[-1].close / btc[-1].close
        asset_returns = _returns(candles, 24)
        correlation = _correlation(asset_returns, btc_returns)
        if correlation is not None:
            values[f"{key}_btc_correlation_24h"] = correlation
        if len(candles) >= 25 and candles[-25].close > 0 and btc[-25].close > 0:
            asset_change = candles[-1].close / candles[-25].close - Decimal("1")
            btc_change = btc[-1].close / btc[-25].close - Decimal("1")
            values[f"{key}_relative_strength_24h"] = asset_change - btc_change
    for candles in candles_by_symbol.values():
        if len(candles) >= 25 and candles[-25].close > 0:
            observed += 1
            positive += int(candles[-1].close > candles[-25].close)
    if observed:
        values["market_breadth_positive_24h"] = Decimal(positive) / Decimal(observed)
    return values
