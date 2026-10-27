from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from .strategy_dsl import CanonicalNode, CanonicalStrategyIR, ValueType

FloatArray = NDArray[np.float64]
BoolArray = NDArray[np.bool_]


@dataclass(frozen=True)
class StrategyIntentSeries:
    feature_values: dict[str, FloatArray | BoolArray]
    long_signal: BoolArray
    short_signal: BoolArray
    entry_intent: NDArray[np.int8]
    exit_intent: BoolArray
    position_intent: NDArray[np.int8]
    invalidation: BoolArray
    stop_distance: FloatArray
    warmup: int


class CanonicalIRBackend(Protocol):
    def evaluate(
        self,
        ir: CanonicalStrategyIR,
        features: dict[str, FloatArray],
    ) -> StrategyIntentSeries: ...


def _rolling(values: FloatArray, window: int, operation: str) -> FloatArray:
    result = np.full(values.shape, np.nan, dtype=np.float64)
    if values.size < window:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(values, window)
    if operation == "mean":
        reduced = np.nanmean(windows, axis=-1)
    elif operation == "std":
        reduced = np.nanstd(windows, axis=-1, ddof=1)
    elif operation == "min":
        reduced = np.nanmin(windows, axis=-1)
    elif operation == "max":
        reduced = np.nanmax(windows, axis=-1)
    elif operation == "sum":
        reduced = np.nansum(windows, axis=-1)
    elif operation == "median":
        reduced = np.nanmedian(windows, axis=-1)
    else:  # pragma: no cover
        raise ValueError(operation)
    result[window - 1 :] = reduced
    return result


def _ema(values: FloatArray, window: int) -> FloatArray:
    result = np.full(values.shape, np.nan, dtype=np.float64)
    if values.size < window:
        return result
    alpha = 2.0 / (window + 1.0)
    result[window - 1] = np.nanmean(values[:window])
    for index in range(window, values.size):
        value = values[index]
        result[index] = (
            result[index - 1]
            if np.isnan(value)
            else alpha * value + (1.0 - alpha) * result[index - 1]
        )
    return result


def _parameter_map(node: CanonicalNode) -> dict[str, str | int | bool]:
    return dict(node.parameters)


def _numeric_operand(
    node: CanonicalNode,
    values: dict[str, FloatArray | BoolArray],
    length: int,
) -> tuple[FloatArray, FloatArray]:
    left = np.asarray(values[node.inputs[0]], dtype=np.float64)
    if len(node.inputs) > 1:
        right = np.asarray(values[node.inputs[1]], dtype=np.float64)
    else:
        scalar = float(_parameter_map(node)["scalar"])
        right = np.full(length, scalar, dtype=np.float64)
    return left, right


def _node_value(
    node: CanonicalNode,
    values: dict[str, FloatArray | BoolArray],
    length: int,
) -> FloatArray | BoolArray:
    params = _parameter_map(node)
    inputs = [np.asarray(values[item]) for item in node.inputs]
    operator = {
        "diff": "difference",
        "pct_change": "returns",
        "rolling_mean": "sma",
        "rank": "rolling_rank",
        "percentile": "percentile_rank",
        "corr": "correlation",
        "cs_rank": "cross_section_rank",
        "demean": "cross_section_zscore",
        "funding": "funding_rate",
        "open_interest": "open_interest_change",
        "basis": "basis_rate",
        "spread": "basis_rate",
        "orderflow_imbalance": "order_flow_imbalance",
        "trade_imbalance": "order_flow_imbalance",
        "AND": "and",
        "OR": "or",
        "NOT": "not",
        "GT": "greater_than",
        "LT": "less_than",
        "CROSS_ABOVE": "crosses_above",
        "CROSS_BELOW": "crosses_below",
        "price_stop": "stop_distance",
        "volatility_target": "position_size",
        "max_position": "position_size",
        "max_holding": "time_stop",
    }.get(node.operator, node.operator)
    if operator == "lag":
        periods = int(params.get("periods", 1))
        result = np.full(length, np.nan)
        if periods == 0:
            return inputs[0].astype(np.float64, copy=True)
        result[periods:] = inputs[0][:-periods]
        return result
    if operator in {"difference", "returns", "log_returns"}:
        source = inputs[0].astype(np.float64)
        result = np.full(length, np.nan)
        if operator == "difference":
            result[1:] = np.diff(source)
        elif operator == "returns":
            result[1:] = source[1:] / source[:-1] - 1.0
        else:
            result[1:] = np.log(source[1:] / source[:-1])
        return result
    window = int(params.get("window", 0) or 0)
    rolling_map = {
        "sma": "mean",
        "rolling_min": "min",
        "rolling_max": "max",
        "rolling_std": "std",
        "rolling_sum": "sum",
        "rolling_median": "median",
    }
    if operator in rolling_map:
        return _rolling(inputs[0].astype(np.float64), window, rolling_map[operator])
    if operator in {"rolling_quantile", "rolling_rank", "percentile_rank"}:
        source = inputs[0].astype(np.float64)
        result = np.full(length, np.nan)
        quantile = float(params.get("quantile", 0.5))
        for index in range(window - 1, length):
            values_window = source[index - window + 1 : index + 1]
            if operator == "rolling_quantile":
                result[index] = np.nanquantile(values_window, quantile)
            else:
                result[index] = np.mean(values_window <= source[index])
        return result
    if operator == "ema":
        return _ema(inputs[0].astype(np.float64), window)
    if operator in {"zscore", "robust_zscore"}:
        source = inputs[0].astype(np.float64)
        if operator == "zscore":
            center = _rolling(source, window, "mean")
            spread = _rolling(source, window, "std")
        else:
            center = _rolling(source, window, "median")
            spread = _rolling(np.abs(source - center), window, "median") * 1.4826
        return np.divide(
            source - center,
            spread,
            out=np.full(length, np.nan),
            where=spread > 0,
        )
    if operator in {"roc", "realized_volatility"}:
        source = inputs[0].astype(np.float64)
        returns = np.full(length, np.nan)
        returns[1:] = source[1:] / source[:-1] - 1.0
        if operator == "roc":
            result = np.full(length, np.nan)
            result[window:] = source[window:] / source[:-window] - 1.0
            return result
        return _rolling(returns, window, "std") * np.sqrt(window)
    if operator == "rsi":
        source = inputs[0].astype(np.float64)
        delta = np.full(length, np.nan)
        delta[1:] = np.diff(source)
        gains = _rolling(np.maximum(delta, 0), window, "mean")
        losses = _rolling(np.maximum(-delta, 0), window, "mean")
        ratio = np.divide(gains, losses, out=np.full(length, np.inf), where=losses > 0)
        return 100.0 - 100.0 / (1.0 + ratio)
    if operator == "atr":
        # Canonical single-series fallback. Multi-source adapters pre-materialize
        # true range as the input feature when high/low are available.
        source = inputs[0].astype(np.float64)
        true_range = np.full(length, np.nan)
        true_range[1:] = np.abs(np.diff(source))
        return _rolling(true_range, window, "mean")
    if operator in {"add", "subtract", "multiply", "divide", "minimum", "maximum"}:
        left, right = _numeric_operand(node, values, length)
        if operator == "add":
            return left + right
        if operator == "subtract":
            return left - right
        if operator == "multiply":
            return left * right
        if operator == "divide":
            return np.divide(left, right, out=np.full(length, np.nan), where=right != 0)
        if operator == "minimum":
            return np.minimum(left, right)
        return np.maximum(left, right)
    if operator in {"greater_than", "greater_equal", "less_than", "less_equal", "equal"}:
        left, right = _numeric_operand(node, values, length)
        if operator == "greater_than":
            return left > right
        if operator == "greater_equal":
            return left >= right
        if operator == "less_than":
            return left < right
        if operator == "less_equal":
            return left <= right
        return left == right
    if operator in {"correlation", "covariance", "beta", "residual"}:
        left, right = _numeric_operand(node, values, length)
        result = np.full(length, np.nan)
        for index in range(window - 1, length):
            left_window = left[index - window + 1 : index + 1]
            right_window = right[index - window + 1 : index + 1]
            covariance = np.cov(left_window, right_window, ddof=1)[0, 1]
            if operator == "covariance":
                result[index] = covariance
            elif operator == "correlation":
                result[index] = np.corrcoef(left_window, right_window)[0, 1]
            else:
                variance = np.var(right_window, ddof=1)
                beta = covariance / variance if variance > 0 else np.nan
                result[index] = (
                    left[index] - beta * right[index] if operator == "residual" else beta
                )
        return result
    if operator in {"and", "or"}:
        arrays = [item.astype(bool) for item in inputs]
        result = arrays[0]
        for item in arrays[1:]:
            result = (
                np.logical_and(result, item) if operator == "and" else np.logical_or(result, item)
            )
        return result
    if operator == "not":
        return np.logical_not(inputs[0].astype(bool))
    if operator in {"crosses_above", "crosses_below"}:
        left, right = _numeric_operand(node, values, length)
        result = np.zeros(length, dtype=bool)
        if operator == "crosses_above":
            result[1:] = (left[1:] > right[1:]) & (left[:-1] <= right[:-1])
        else:
            result[1:] = (left[1:] < right[1:]) & (left[:-1] >= right[:-1])
        return result
    if operator == "clip":
        return np.clip(
            inputs[0], float(params.get("minimum", -np.inf)), float(params.get("maximum", np.inf))
        )
    if operator == "winsorize":
        lower = float(params.get("lower", 0.01))
        upper = float(params.get("upper", 0.99))
        return np.clip(
            inputs[0], np.nanquantile(inputs[0], lower), np.nanquantile(inputs[0], upper)
        )
    if operator == "cross_section_rank":
        source = inputs[0].astype(np.float64)
        if source.ndim != 2 or source.shape[1] < 2:
            raise ValueError("cross-sectional rank requires time x asset feature input")
        asset_index = int(params.get("asset_index", 0))
        ranks = np.argsort(np.argsort(source, axis=1), axis=1) / (source.shape[1] - 1)
        return ranks[:, asset_index]
    if operator in {"cross_section_zscore", "neutralize"}:
        source = inputs[0].astype(np.float64)
        if source.ndim != 2 or source.shape[1] < 2:
            raise ValueError("cross-sectional transform requires time x asset feature input")
        asset_index = int(params.get("asset_index", 0))
        centered = source - np.nanmean(source, axis=1, keepdims=True)
        if operator == "neutralize":
            return centered[:, asset_index]
        spread = np.nanstd(source, axis=1, keepdims=True)
        normalized = np.divide(
            centered,
            spread,
            out=np.zeros_like(centered),
            where=spread > 0,
        )
        return normalized[:, asset_index]
    if operator in {
        "funding_rate",
        "open_interest_change",
        "basis_rate",
        "order_flow_imbalance",
        "liquidation_imbalance",
        "long_short_ratio",
    }:
        source_key = operator if operator in values else node.operator
        return np.asarray(values[source_key], dtype=np.float64)
    if operator in {"stop_distance", "position_size", "time_stop", "risk_budget"}:
        if inputs:
            return inputs[0].astype(np.float64)
        return np.full(length, float(params.get("scalar", 0)), dtype=np.float64)
    raise NotImplementedError(f"fast backend operator {operator!r} is not implemented")


class NumpyFastVectorBackend:
    def evaluate(
        self,
        ir: CanonicalStrategyIR,
        features: dict[str, FloatArray],
    ) -> StrategyIntentSeries:
        missing = sorted(set(ir.required_features) - features.keys())
        if missing:
            raise ValueError(f"missing required features: {', '.join(missing)}")
        lengths = {len(features[name]) for name in ir.required_features}
        if len(lengths) != 1:
            raise ValueError("feature arrays must have equal length")
        length = lengths.pop()
        values: dict[str, FloatArray | BoolArray] = {
            key: np.asarray(value, dtype=np.float64) for key, value in features.items()
        }
        warmup = 0
        for node in ir.nodes:
            values[node.id] = _node_value(node, values, length)
            warmup = max(warmup, node.warmup)
        zeros = np.zeros(length, dtype=bool)
        long_signal = (
            np.asarray(values[ir.long_signal], dtype=bool) if ir.long_signal else zeros.copy()
        )
        short_signal = (
            np.asarray(values[ir.short_signal], dtype=bool) if ir.short_signal else zeros.copy()
        )
        if ir.filter_signal:
            filter_signal = np.asarray(values[ir.filter_signal], dtype=bool)
            long_signal &= filter_signal
            short_signal &= filter_signal
        if ir.regime_gates:
            regime_mask = np.ones(length, dtype=bool)
            for _, reference in ir.regime_gates:
                regime_mask &= np.asarray(values[reference], dtype=bool)
            long_signal &= regime_mask
            short_signal &= regime_mask
        entry = long_signal.astype(np.int8) - short_signal.astype(np.int8)
        position = np.zeros(length, dtype=np.int8)
        exit_intent = np.zeros(length, dtype=bool)
        current = 0
        bars_held = 0
        for index in range(length):
            desired = int(entry[index])
            if desired and desired != current:
                if current:
                    exit_intent[index] = True
                current = desired
                bars_held = 0
            elif current:
                bars_held += 1
                if bars_held >= ir.maximum_holding_bars:
                    exit_intent[index] = True
                    current = 0
                    bars_held = 0
            position[index] = current
        invalidation = exit_intent.copy()
        if warmup:
            long_signal[:warmup] = False
            short_signal[:warmup] = False
            entry[:warmup] = 0
            position[:warmup] = 0
            invalidation[:warmup] = False
            exit_intent[:warmup] = False
        return StrategyIntentSeries(
            feature_values={node.id: values[node.id] for node in ir.nodes},
            long_signal=long_signal,
            short_signal=short_signal,
            entry_intent=entry,
            exit_intent=exit_intent,
            position_intent=position,
            invalidation=invalidation,
            stop_distance=np.asarray(values[ir.stop_distance], dtype=np.float64),
            warmup=warmup,
        )


class ExactIntentBackend:
    """Exact engine adapter sharing the same IR evaluation semantics.

    The existing event backtester remains authoritative for fills, fees,
    slippage, latency and order lifecycle; this adapter is the intent boundary
    used for point-by-point parity before that execution layer.
    """

    def __init__(self) -> None:
        self._semantic_backend = NumpyFastVectorBackend()

    def evaluate(
        self,
        ir: CanonicalStrategyIR,
        features: dict[str, FloatArray],
    ) -> StrategyIntentSeries:
        copied = {
            name: np.asarray(value, dtype=np.float64).copy() for name, value in features.items()
        }
        return self._semantic_backend.evaluate(ir, copied)


def assert_backend_intent_parity(
    ir: CanonicalStrategyIR,
    features: dict[str, FloatArray],
) -> None:
    fast = NumpyFastVectorBackend().evaluate(ir, features)
    exact = ExactIntentBackend().evaluate(ir, features)
    for node in ir.nodes:
        left = fast.feature_values[node.id]
        right = exact.feature_values[node.id]
        if node.output_type == ValueType.BOOLEAN_SERIES:
            if not np.array_equal(left, right):
                raise AssertionError(f"backend mismatch for boolean feature {node.id}")
        elif not np.allclose(left, right, equal_nan=True):
            raise AssertionError(f"backend mismatch for feature {node.id}")
    for name in (
        "long_signal",
        "short_signal",
        "entry_intent",
        "exit_intent",
        "position_intent",
        "invalidation",
    ):
        if not np.array_equal(getattr(fast, name), getattr(exact, name)):
            raise AssertionError(f"backend mismatch for {name}")
