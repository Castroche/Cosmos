from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np

from .alpha_factory import (
    AlphaInferenceResultV1,
    AlphaModelArtifactV1,
    predict_alpha_from_feature_history,
)
from .domain import Candle, MarketRegime, MarketSnapshot, SignalDirection, StrategySignal
from .strategy_dsl import CanonicalStrategyIR

_EXTERNAL_OPERATOR_ALIASES = {
    "funding": "funding_rate",
    "funding_rate": "funding_rate",
    "open_interest": "open_interest_change",
    "open_interest_change": "open_interest_change",
    "basis": "basis_rate",
    "spread": "basis_rate",
    "basis_rate": "basis_rate",
    "orderflow_imbalance": "order_flow_imbalance",
    "trade_imbalance": "order_flow_imbalance",
    "order_flow_imbalance": "order_flow_imbalance",
    "long_short_ratio": "long_short_ratio",
    "liquidation_imbalance": "liquidation_imbalance",
}


def _as_utc(value: datetime | str) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _available_time(payload: dict, *, order_flow: bool = False) -> datetime:
    explicit = payload.get("available_time")
    if explicit:
        return _as_utc(explicit)
    timestamp = _as_utc(payload["timestamp"])
    return timestamp + timedelta(minutes=5) if order_flow else timestamp


def _causal_series(
    decision_ns: np.ndarray,
    payloads: list[dict],
    value,
    *,
    order_flow: bool = False,
) -> np.ndarray:
    result = np.full(len(decision_ns), np.nan, dtype=np.float64)
    observations: list[tuple[int, float]] = []
    for payload in payloads:
        try:
            measured = float(value(payload))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if np.isfinite(measured):
            observations.append(
                (int(_available_time(payload, order_flow=order_flow).timestamp() * 1e9), measured)
            )
    if not observations:
        return result
    observations.sort(key=lambda item: item[0])
    available = np.asarray([item[0] for item in observations], dtype=np.int64)
    values = np.asarray([item[1] for item in observations], dtype=np.float64)
    positions = np.searchsorted(available, decision_ns, side="right") - 1
    present = positions >= 0
    result[present] = values[positions[present]]
    return result


def _snapshot_age(
    snapshot: MarketSnapshot,
    timestamp: datetime | None,
    maximum_age: timedelta,
    name: str,
) -> None:
    if timestamp is None:
        raise ValueError(f"current {name} available_time is missing")
    observed = _as_utc(timestamp)
    decision = _as_utc(snapshot.timestamp)
    if observed > decision or decision - observed > maximum_age:
        raise ValueError(f"current {name} is stale or future-dated")


def required_external_alpha_features(ir: CanonicalStrategyIR) -> set[str]:
    required = {
        normalized
        for name in ir.required_features
        if (normalized := _EXTERNAL_OPERATOR_ALIASES.get(name)) is not None
    }
    required.update(
        normalized
        for node in ir.nodes
        if (normalized := _EXTERNAL_OPERATOR_ALIASES.get(node.operator)) is not None
    )
    return required


def build_causal_alpha_feature_history(
    candles: list[Candle],
    feature_tail: dict[str, list[dict]],
    snapshot: MarketSnapshot | None,
    ir: CanonicalStrategyIR,
    *,
    enforce_current_snapshot: bool = True,
) -> dict[str, np.ndarray]:
    """Build the current model input only from closed bars and available-time facts."""

    if len(candles) < 2:
        raise ValueError("alpha runtime requires at least two closed 5m candles")
    ordered = sorted(candles, key=lambda item: item.timestamp)
    if any(
        right.timestamp <= left.timestamp
        for left, right in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("closed 5m candles must be strictly ordered and unique")
    decision_ns = np.asarray(
        [
            int((_as_utc(item.timestamp) + timedelta(minutes=5)).timestamp() * 1e9)
            for item in ordered
        ],
        dtype=np.int64,
    )
    raw: dict[str, np.ndarray] = {
        "open": np.asarray([float(item.open) for item in ordered], dtype=np.float64),
        "high": np.asarray([float(item.high) for item in ordered], dtype=np.float64),
        "low": np.asarray([float(item.low) for item in ordered], dtype=np.float64),
        "close": np.asarray([float(item.close) for item in ordered], dtype=np.float64),
        "volume": np.asarray([float(item.volume) for item in ordered], dtype=np.float64),
    }
    funding = _causal_series(
        decision_ns,
        feature_tail.get("funding", []),
        lambda item: item["rate"],
    )
    open_interest_level = _causal_series(
        decision_ns,
        feature_tail.get("open_interest", []),
        lambda item: (
            item["open_interest_amount"]
            if item.get("open_interest_amount") is not None
            else item["open_interest_value"]
        ),
    )
    open_interest_change = np.full(len(ordered), np.nan, dtype=np.float64)
    lookback = 24 * 12
    if len(ordered) > lookback:
        baseline = open_interest_level[:-lookback]
        current = open_interest_level[lookback:]
        open_interest_change[lookback:] = np.divide(
            current - baseline,
            np.abs(baseline),
            out=np.full(len(current), np.nan),
            where=np.isfinite(baseline) & (baseline != 0) & np.isfinite(current),
        )
    positioning = _causal_series(
        decision_ns,
        feature_tail.get("positioning", []),
        lambda item: item["long_short_ratio"],
    )
    basis = _causal_series(
        decision_ns,
        feature_tail.get("basis", []),
        lambda item: item["basis_rate"],
    )
    order_flow = _causal_series(
        decision_ns,
        feature_tail.get("order_flow", []),
        lambda item: float(item["delta_base_volume"]) / float(item["base_volume"]),
        order_flow=True,
    )
    liquidation = _causal_series(
        decision_ns,
        feature_tail.get("liquidation_flow", []),
        lambda item: (
            (float(item["short_liquidated_contracts"]) - float(item["long_liquidated_contracts"]))
            / (float(item["short_liquidated_contracts"]) + float(item["long_liquidated_contracts"]))
        ),
    )
    raw.update(
        {
            "funding_rate": funding,
            "funding": funding,
            "open_interest_level": open_interest_level,
            "open_interest_change": open_interest_change,
            "open_interest": open_interest_change,
            "long_short_ratio": positioning,
            "basis_rate": basis,
            "basis": basis,
            "spread": basis,
            "order_flow_imbalance": order_flow,
            "orderflow_imbalance": order_flow,
            "trade_imbalance": order_flow,
            "liquidation_imbalance": liquidation,
        }
    )

    required_external = required_external_alpha_features(ir)
    if enforce_current_snapshot and snapshot is None:
        raise ValueError("current market snapshot is required for alpha execution")
    if not enforce_current_snapshot:
        snapshot = None
    if snapshot is not None and "funding_rate" in required_external:
        if snapshot.funding_rate is None:
            raise ValueError("current funding_rate is unavailable")
        _snapshot_age(snapshot, snapshot.funding_time, timedelta(hours=9), "funding_rate")
        funding[-1] = float(snapshot.funding_rate)
    if snapshot is not None and "open_interest_change" in required_external:
        if snapshot.open_interest_change_24h is None:
            raise ValueError("current open_interest_change is unavailable")
        _snapshot_age(snapshot, snapshot.open_interest_time, timedelta(minutes=15), "open_interest")
        open_interest_change[-1] = float(snapshot.open_interest_change_24h)
    if snapshot is not None and "basis_rate" in required_external:
        if snapshot.basis_rate is None:
            raise ValueError("current basis_rate is unavailable")
        _snapshot_age(snapshot, snapshot.basis_time, timedelta(minutes=15), "basis")
        basis[-1] = float(snapshot.basis_rate)
    if snapshot is not None and "order_flow_imbalance" in required_external:
        if snapshot.order_flow_imbalance is None:
            raise ValueError("current order_flow_imbalance is unavailable")
        _snapshot_age(snapshot, snapshot.order_flow_time, timedelta(minutes=5), "order_flow")
        order_flow[-1] = float(snapshot.order_flow_imbalance)
    if snapshot is not None and "long_short_ratio" in required_external:
        if snapshot.long_short_ratio is None:
            raise ValueError("current long_short_ratio is unavailable")
        _snapshot_age(snapshot, snapshot.positioning_time, timedelta(hours=2), "positioning")
        positioning[-1] = float(snapshot.long_short_ratio)
    if snapshot is not None and "liquidation_imbalance" in required_external:
        if snapshot.liquidation_flow_imbalance is None:
            raise ValueError("current liquidation_imbalance is unavailable")
        _snapshot_age(
            snapshot,
            snapshot.liquidation_flow_time,
            timedelta(hours=1),
            "liquidation_flow",
        )
        liquidation[-1] = float(snapshot.liquidation_flow_imbalance)

    missing = sorted(set(ir.required_features) - raw.keys())
    if missing:
        raise ValueError(f"registered runtime feature adapters are missing: {missing}")
    return raw


def estimate_alpha_round_trip_cost(
    *,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    spread_bps: Decimal,
    notional: Decimal,
    latest_quote_volume: Decimal,
) -> Decimal:
    participation = notional / latest_quote_volume if latest_quote_volume > 0 else Decimal("1")
    impact_per_side_bps = (
        Decimal(str(np.sqrt(float(max(participation, Decimal("0")))))) * Decimal("5")
    )
    total_bps = (fee_bps + slippage_bps + impact_per_side_bps) * 2 + spread_bps
    return total_bps / Decimal("10000")


def evaluate_certified_alpha_signal(
    linear: AlphaModelArtifactV1,
    gbdt: AlphaModelArtifactV1,
    ensemble: AlphaModelArtifactV1,
    *,
    candles: list[Candle],
    feature_tail: dict[str, list[dict]],
    snapshot: MarketSnapshot,
    estimated_round_trip_cost: Decimal,
) -> tuple[StrategySignal, AlphaInferenceResultV1]:
    if linear.canonical_ir_payload is None:
        raise ValueError("certified alpha artifact has no Canonical IR")
    ir = CanonicalStrategyIR.model_validate(linear.canonical_ir_payload)
    raw = build_causal_alpha_feature_history(candles, feature_tail, snapshot, ir)
    inference = predict_alpha_from_feature_history(
        linear,
        gbdt,
        ensemble,
        raw_features=raw,
        estimated_round_trip_cost=float(estimated_round_trip_cost),
    )
    direction = {
        -1: SignalDirection.SHORT,
        0: SignalDirection.FLAT,
        1: SignalDirection.LONG,
    }[inference.signal]
    cost_floor = max(inference.estimated_round_trip_cost * 2, Decimal("0.00000001"))
    confidence = min(abs(inference.blended_prediction) / cost_floor, Decimal("1"))
    if direction == SignalDirection.FLAT:
        confidence = min(confidence, Decimal("0.49"))
    reasons = [
        "Elastic Net/Logistic 与 LightGBM 仅在方向一致时形成 Alpha 信号",
        "预测净优势必须严格超过两倍当前估算往返成本",
        "Canonical IR 中七类状态门已在本次已收盘 5m 特征上重新执行",
    ]
    if direction == SignalDirection.FLAT:
        reasons.append("方向、成本优势或显式状态门未同时通过，禁止新增风险")
    signal = StrategySignal(
        algorithm="alpha_ensemble_50_50",
        symbol=snapshot.symbol,
        timeframe="5m",
        direction=direction,
        regime=(
            MarketRegime.TREND
            if direction != SignalDirection.FLAT
            else MarketRegime.UNCERTAIN
        ),
        confidence=confidence,
        signal_candle_at=sorted(candles, key=lambda item: item.timestamp)[-1].timestamp,
        data_as_of=snapshot.timestamp,
        reasons=reasons,
        features={
            "horizon_bars": str(inference.horizon_bars),
            "linear_prediction": inference.linear_prediction,
            "gbdt_prediction": inference.gbdt_prediction,
            "blended_prediction": inference.blended_prediction,
            "estimated_round_trip_cost": inference.estimated_round_trip_cost,
            "canonical_ir_direction": str(inference.canonical_ir_direction),
            "regime_gates_passed": inference.regime_gates_passed,
            "ensemble_artifact_fingerprint": inference.ensemble_artifact_fingerprint,
        },
    )
    return signal, inference
