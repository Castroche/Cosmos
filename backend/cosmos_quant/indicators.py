from __future__ import annotations

from decimal import Decimal
from math import sqrt

from .domain import Candle


def ema(values: list[Decimal], period: int) -> list[Decimal]:
    if period <= 0 or len(values) < period:
        raise ValueError("not enough values for EMA period")
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    multiplier = Decimal("2") / Decimal(period + 1)
    result = [seed]
    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def atr(candles: list[Candle], period: int = 14) -> Decimal:
    if len(candles) < period + 1:
        raise ValueError("not enough candles for ATR")
    # Only the latest ``period`` true ranges affect this simple ATR. Bounding
    # the input keeps per-bar market sensing O(period) during long backtests
    # without changing the result.
    candles = candles[-(period + 1) :]
    true_ranges: list[Decimal] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return sum(true_ranges[-period:], Decimal("0")) / Decimal(period)


def sma(values: list[Decimal], period: int) -> Decimal:
    if period <= 0 or len(values) < period:
        raise ValueError("not enough values for SMA period")
    return sum(values[-period:], Decimal("0")) / Decimal(period)


def standard_deviation(values: list[Decimal], period: int) -> Decimal:
    window = values[-period:]
    mean = sma(values, period)
    variance = sum((value - mean) ** 2 for value in window) / Decimal(period)
    return Decimal(str(sqrt(float(variance))))


def rsi(values: list[Decimal], period: int = 14) -> Decimal:
    if len(values) < period + 1:
        raise ValueError("not enough values for RSI period")
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    gains = sum((max(change, Decimal("0")) for change in changes[-period:]), Decimal("0"))
    losses = sum((abs(min(change, Decimal("0"))) for change in changes[-period:]), Decimal("0"))
    if losses == 0:
        return Decimal("100")
    relative_strength = gains / losses
    return Decimal("100") - Decimal("100") / (Decimal("1") + relative_strength)


def rate_of_change(values: list[Decimal], period: int = 12) -> Decimal:
    if period <= 0 or len(values) < period + 1:
        raise ValueError("not enough values for ROC period")
    baseline = values[-period - 1]
    if baseline == 0:
        return Decimal("0")
    return (values[-1] / baseline - Decimal("1")) * Decimal("100")


def bollinger_bands(
    values: list[Decimal],
    period: int = 20,
    deviations: Decimal = Decimal("2"),
) -> dict[str, Decimal]:
    if deviations <= 0:
        raise ValueError("Bollinger deviation multiplier must be positive")
    middle = sma(values, period)
    width = standard_deviation(values, period) * deviations
    upper = middle + width
    lower = middle - width
    bandwidth = (upper - lower) / middle if middle else Decimal("0")
    percent_b = (values[-1] - lower) / (upper - lower) if upper != lower else Decimal("0.5")
    return {
        "lower": lower,
        "middle": middle,
        "upper": upper,
        "bandwidth": bandwidth,
        "percent_b": percent_b,
    }


def macd(
    values: list[Decimal],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict[str, Decimal]:
    if fast_period >= slow_period:
        raise ValueError("MACD fast period must be below slow period")
    minimum = slow_period + signal_period - 1
    if len(values) < minimum:
        raise ValueError("not enough values for MACD periods")
    fast = ema(values, fast_period)
    slow = ema(values, slow_period)
    aligned_fast = fast[-len(slow) :]
    line_series = [
        fast_value - slow_value for fast_value, slow_value in zip(aligned_fast, slow, strict=True)
    ]
    signal_series = ema(line_series, signal_period)
    line = line_series[-1]
    signal = signal_series[-1]
    return {
        "line": line,
        "signal": signal,
        "histogram": line - signal,
    }


def stochastic_oscillator(
    candles: list[Candle],
    period: int = 14,
    smooth: int = 3,
) -> dict[str, Decimal]:
    if period <= 0 or smooth <= 0 or len(candles) < period + smooth - 1:
        raise ValueError("not enough candles for stochastic oscillator")
    k_values: list[Decimal] = []
    for end in range(len(candles) - smooth + 1, len(candles) + 1):
        window = candles[end - period : end]
        lowest = min(item.low for item in window)
        highest = max(item.high for item in window)
        value = (
            (window[-1].close - lowest) / (highest - lowest) * Decimal("100")
            if highest != lowest
            else Decimal("50")
        )
        k_values.append(value)
    return {
        "k": k_values[-1],
        "d": sum(k_values, Decimal("0")) / Decimal(len(k_values)),
    }


def commodity_channel_index(candles: list[Candle], period: int = 20) -> Decimal:
    if period <= 0 or len(candles) < period:
        raise ValueError("not enough candles for CCI period")
    typical = [(item.high + item.low + item.close) / Decimal("3") for item in candles[-period:]]
    mean = sum(typical, Decimal("0")) / Decimal(period)
    mean_deviation = sum((abs(value - mean) for value in typical), Decimal("0")) / Decimal(period)
    if mean_deviation == 0:
        return Decimal("0")
    return (typical[-1] - mean) / (Decimal("0.015") * mean_deviation)


def williams_r(candles: list[Candle], period: int = 14) -> Decimal:
    if period <= 0 or len(candles) < period:
        raise ValueError("not enough candles for Williams %R period")
    window = candles[-period:]
    highest = max(item.high for item in window)
    lowest = min(item.low for item in window)
    if highest == lowest:
        return Decimal("-50")
    return (highest - window[-1].close) / (highest - lowest) * Decimal("-100")


def on_balance_volume(candles: list[Candle]) -> Decimal:
    if len(candles) < 2:
        raise ValueError("OBV requires at least two candles")
    value = Decimal("0")
    for previous, current in zip(candles, candles[1:], strict=False):
        if current.close > previous.close:
            value += current.volume
        elif current.close < previous.close:
            value -= current.volume
    return value


def volume_weighted_average_price(candles: list[Candle], period: int = 20) -> Decimal:
    if period <= 0 or len(candles) < period:
        raise ValueError("not enough candles for VWAP period")
    window = candles[-period:]
    total_volume = sum((item.volume for item in window), Decimal("0"))
    if total_volume == 0:
        return sum((item.close for item in window), Decimal("0")) / Decimal(period)
    return (
        sum(
            (((item.high + item.low + item.close) / Decimal("3")) * item.volume for item in window),
            Decimal("0"),
        )
        / total_volume
    )


def average_directional_index(candles: list[Candle], period: int = 14) -> Decimal:
    """Return Wilder-style ADX from closed candles only."""

    if period <= 0 or len(candles) < period * 2 + 1:
        raise ValueError("not enough candles for ADX period")
    # The returned value averages the latest ``period`` directional indexes,
    # and each index uses ``period`` true ranges. Older candles cannot affect
    # that result, so discard them before building the rolling windows.
    candles = candles[-(period * 2 + 1) :]
    true_ranges: list[Decimal] = []
    positive_dm: list[Decimal] = []
    negative_dm: list[Decimal] = []
    for previous, current in zip(candles, candles[1:], strict=False):
        up = current.high - previous.high
        down = previous.low - current.low
        positive_dm.append(up if up > down and up > 0 else Decimal("0"))
        negative_dm.append(down if down > up and down > 0 else Decimal("0"))
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    directional_indexes: list[Decimal] = []
    for end in range(period, len(true_ranges) + 1):
        tr = sum(true_ranges[end - period : end], Decimal("0"))
        if tr == 0:
            directional_indexes.append(Decimal("0"))
            continue
        plus = sum(positive_dm[end - period : end], Decimal("0")) / tr * Decimal("100")
        minus = sum(negative_dm[end - period : end], Decimal("0")) / tr * Decimal("100")
        total = plus + minus
        directional_indexes.append(
            abs(plus - minus) / total * Decimal("100") if total else Decimal("0")
        )
    return sum(directional_indexes[-period:], Decimal("0")) / Decimal(period)


INDICATOR_CATALOG: dict[str, dict[str, str]] = {
    "ema": {"category": "trend", "function": "ema"},
    "sma": {"category": "trend", "function": "sma"},
    "macd": {"category": "trend_momentum", "function": "macd"},
    "adx": {"category": "trend_strength", "function": "average_directional_index"},
    "rsi": {"category": "momentum", "function": "rsi"},
    "stochastic": {"category": "momentum", "function": "stochastic_oscillator"},
    "roc": {"category": "momentum", "function": "rate_of_change"},
    "bollinger_bands": {"category": "volatility", "function": "bollinger_bands"},
    "atr": {"category": "volatility_risk", "function": "atr"},
    "standard_deviation": {
        "category": "volatility",
        "function": "standard_deviation",
    },
    "cci": {"category": "mean_reversion", "function": "commodity_channel_index"},
    "williams_r": {"category": "mean_reversion", "function": "williams_r"},
    "obv": {"category": "volume", "function": "on_balance_volume"},
    "vwap": {"category": "price_volume", "function": "volume_weighted_average_price"},
    "donchian_channel": {"category": "breakout", "function": "rolling_high_low"},
    "funding_rate": {"category": "crypto_derivatives", "function": "market_snapshot"},
    "open_interest": {"category": "crypto_derivatives", "function": "market_microstructure"},
    "global_long_short_ratio": {
        "category": "crypto_positioning",
        "function": "positioning_history",
    },
    "mark_index_basis": {
        "category": "crypto_derivatives",
        "function": "basis_history",
    },
    "taker_flow_imbalance": {
        "category": "crypto_order_flow",
        "function": "aggregate_trades",
    },
    "liquidation_flow": {
        "category": "crypto_forced_flow",
        "function": "liquidation_stream",
    },
    "order_book_imbalance": {
        "category": "crypto_microstructure",
        "function": "market_microstructure",
    },
    "trade_flow_imbalance": {
        "category": "crypto_microstructure",
        "function": "market_microstructure",
    },
}

_INDICATOR_SEMANTICS: dict[str, dict[str, str]] = {
    "ema": {
        "role": "direction",
        "regimes": "trend",
        "warning": "与 SMA/MACD 高度相关，避免重复投票",
    },
    "sma": {
        "role": "direction",
        "regimes": "slow_trend",
        "warning": "滞后于快速反转，不与 EMA 堆叠确认",
    },
    "macd": {
        "role": "direction_and_momentum",
        "regimes": "trend",
        "warning": "震荡市场容易反复交叉",
    },
    "adx": {
        "role": "trend_strength_filter",
        "regimes": "trend_or_range_classifier",
        "warning": "只描述强度，不提供方向",
    },
    "rsi": {
        "role": "momentum_or_reversal",
        "regimes": "range",
        "warning": "强趋势中可长期停留在极值区",
    },
    "stochastic": {
        "role": "range_position",
        "regimes": "range",
        "warning": "与 Williams %R 高度相关",
    },
    "roc": {
        "role": "time_series_momentum",
        "regimes": "trend",
        "warning": "观察窗口变化会显著改变信号频率",
    },
    "bollinger_bands": {
        "role": "relative_price_and_volatility",
        "regimes": "range_or_volatility_expansion",
        "warning": "越过带宽既可能回归也可能沿带趋势，必须先分类状态",
    },
    "atr": {
        "role": "risk_and_volatility",
        "regimes": "all",
        "warning": "不提供方向，只用于标准化与风险预算",
    },
    "cci": {
        "role": "cyclical_deviation",
        "regimes": "range",
        "warning": "趋势中极值不等于立即反转",
    },
    "williams_r": {
        "role": "range_position",
        "regimes": "range",
        "warning": "与 stochastic 共享高低区间信息",
    },
    "obv": {
        "role": "volume_confirmation",
        "regimes": "trend_or_breakout",
        "warning": "交易所成交量口径差异会造成跨市场偏差",
    },
    "vwap": {
        "role": "volume_weighted_reference",
        "regimes": "liquid_range",
        "warning": "滚动 VWAP 不等于交易所会话 VWAP",
    },
    "funding_rate": {
        "role": "carry_and_crowding",
        "regimes": "perpetual_swap",
        "warning": "单独使用不可靠，结算间隔和符号方向必须按交易所校准",
    },
    "open_interest": {
        "role": "derivatives_participation",
        "regimes": "perpetual_or_futures",
        "warning": "必须与价格方向联合解释且统一 amount/value 口径",
    },
    "global_long_short_ratio": {
        "role": "positioning_crowding",
        "regimes": "perpetual_swap",
        "warning": (
            "Exchange-reported account ratios are venue-specific and are not "
            "capital-weighted exposure"
        ),
    },
    "mark_index_basis": {
        "role": "futures_carry_and_dislocation",
        "regimes": "perpetual_or_futures",
        "warning": (
            "Basis can persist during structural repricing; extremes are "
            "hypotheses, not automatic reversals"
        ),
    },
    "taker_flow_imbalance": {
        "role": "closed_bar_aggressive_flow_confirmation",
        "regimes": "liquid_microstructure",
        "warning": (
            "A bar is available only after close; using its final flow inside "
            "that bar is look-ahead"
        ),
    },
    "liquidation_flow": {
        "role": "forced_position_unwind_context",
        "regimes": "leveraged_stress",
        "warning": (
            "The live all-market stream is sampled and is not complete historical liquidation truth"
        ),
    },
    "order_book_imbalance": {
        "role": "short_horizon_execution_context",
        "regimes": "liquid_microstructure",
        "warning": "容易撤单和失真，只能作执行上下文，不能证明中期策略",
    },
    "trade_flow_imbalance": {
        "role": "aggressive_flow_context",
        "regimes": "liquid_microstructure",
        "warning": "取样窗口短，不得外推为长期方向",
    },
}
for _name, _semantics in _INDICATOR_SEMANTICS.items():
    if _name in INDICATOR_CATALOG:
        INDICATOR_CATALOG[_name].update(_semantics)


def recommend_indicator_stack(
    primary_regime: str,
) -> list[dict[str, str]]:
    """Return a small non-redundant indicator stack for an Agent hypothesis."""

    if "breakout" in primary_regime or "expansion" in primary_regime:
        names = ["donchian_channel", "atr", "obv", "open_interest"]
    elif "trend" in primary_regime:
        names = ["ema", "adx", "atr", "open_interest"]
    elif "range" in primary_regime:
        names = ["bollinger_bands", "rsi", "atr", "vwap"]
    else:
        names = ["adx", "bollinger_bands", "atr", "trade_flow_imbalance"]
    return [
        {"name": name, **INDICATOR_CATALOG[name]} for name in names if name in INDICATOR_CATALOG
    ]
