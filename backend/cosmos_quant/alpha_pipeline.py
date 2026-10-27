from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal

import numpy as np

from .alpha_factory import (
    ALPHA_HORIZONS,
    AlphaTrainingResultV1,
    frozen_net_return_labels,
    split_closed_bars_on_gaps,
    train_alpha_model_pair,
)
from .alpha_runtime import build_causal_alpha_feature_history
from .domain import Candle
from .strategy_backends import NumpyFastVectorBackend
from .strategy_dsl import CanonicalStrategyIR


@dataclass(frozen=True)
class FrozenAlphaFeatureCube:
    feature_names: tuple[str, ...]
    matrix: np.ndarray
    labels: dict[int, np.ndarray]
    timestamps: tuple
    fingerprint: str
    label_fingerprints: dict[int, str]


def _array_fingerprint(*parts: np.ndarray | bytes | str) -> str:
    digest = sha256()
    for part in parts:
        if isinstance(part, str):
            digest.update(part.encode())
        elif isinstance(part, bytes):
            digest.update(part)
        else:
            contiguous = np.ascontiguousarray(part)
            digest.update(str(contiguous.dtype).encode())
            digest.update(str(contiguous.shape).encode())
            digest.update(contiguous.tobytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build_frozen_alpha_feature_cube(
    candles: list[Candle],
    feature_history: dict[str, list[dict]],
    ir: CanonicalStrategyIR,
    *,
    feature_names: tuple[str, ...],
    dataset_fingerprint: str,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    impact_bps: Decimal = Decimal("5"),
) -> FrozenAlphaFeatureCube:
    """Compile one immutable market cube and all three cost-after labels."""

    ordered = sorted(candles, key=lambda item: item.timestamp)
    raw = build_causal_alpha_feature_history(
        ordered,
        feature_history,
        None,
        ir,
        enforce_current_snapshot=False,
    )
    intent = NumpyFastVectorBackend().evaluate(ir, raw)
    available = {**raw, **intent.feature_values}
    missing = sorted(set(feature_names) - available.keys())
    if missing:
        raise ValueError(f"feature cube contains unregistered features: {missing}")
    matrix = np.column_stack(
        [np.asarray(available[name], dtype=np.float64) for name in feature_names]
    )
    close = np.asarray([float(item.close) for item in ordered], dtype=np.float64)
    timestamps_ns = np.asarray(
        [int(item.timestamp.timestamp() * 1e9) for item in ordered],
        dtype=np.int64,
    )
    segments = split_closed_bars_on_gaps(timestamps_ns)
    entry_exit_fraction = float((fee_bps + slippage_bps) / Decimal("10000"))
    impact_fraction = float(impact_bps / Decimal("10000"))
    labels = {
        horizon: np.full(len(ordered), np.nan, dtype=np.float64)
        for horizon in ALPHA_HORIZONS
    }
    offset = 0
    for segment in segments:
        segment_length = len(segment)
        segment_labels = frozen_net_return_labels(
            close[offset : offset + segment_length],
            entry_cost_fraction=entry_exit_fraction,
            exit_cost_fraction=entry_exit_fraction,
            impact_fraction=impact_fraction * 2,
        )
        for horizon in ALPHA_HORIZONS:
            labels[horizon][offset : offset + segment_length] = segment_labels[horizon]
        offset += segment_length
    timestamps = tuple(item.timestamp + timedelta(minutes=5) for item in ordered)
    cube_fingerprint = _array_fingerprint(
        dataset_fingerprint,
        ir.fingerprint,
        "|".join(feature_names),
        matrix,
        timestamps_ns,
    )
    label_fingerprints = {
        horizon: _array_fingerprint(
            cube_fingerprint,
            str(horizon),
            labels[horizon],
            str(fee_bps),
            str(slippage_bps),
            str(impact_bps),
        )
        for horizon in ALPHA_HORIZONS
    }
    return FrozenAlphaFeatureCube(
        feature_names=feature_names,
        matrix=matrix,
        labels=labels,
        timestamps=timestamps,
        fingerprint=cube_fingerprint,
        label_fingerprints=label_fingerprints,
    )


def train_frozen_alpha_horizons(
    cube: FrozenAlphaFeatureCube,
    ir: CanonicalStrategyIR,
    *,
    cost_model_fingerprint: str,
    task: Literal["regression", "classification"] = "regression",
    random_seed: int = 17,
) -> dict[int, AlphaTrainingResultV1]:
    """Train 3/6/12-bar native model trios without touching the live path."""

    results: dict[int, AlphaTrainingResultV1] = {}
    for horizon in ALPHA_HORIZONS:
        horizon_ir = ir.model_copy(
            update={
                "holding_horizon": f"{horizon} bars",
                "maximum_holding_bars": horizon,
            }
        )
        labels = cube.labels[horizon]
        mask = np.isfinite(labels) & np.all(np.isfinite(cube.matrix), axis=1)
        if np.count_nonzero(mask) < 3_200:
            raise ValueError(
                f"horizon {horizon} has fewer than 3,200 causally complete training rows"
            )
        results[horizon] = train_alpha_model_pair(
            cube.matrix[mask],
            labels[mask],
            feature_names=cube.feature_names,
            horizon_bars=horizon,
            feature_cube_fingerprint=cube.fingerprint,
            label_fingerprint=cube.label_fingerprints[horizon],
            cost_model_fingerprint=cost_model_fingerprint,
            ir_fingerprint=horizon_ir.fingerprint,
            canonical_ir=horizon_ir,
            timestamps=tuple(
                timestamp for timestamp, keep in zip(cube.timestamps, mask, strict=True) if keep
            ),
            task=task,
            random_seed=random_seed,
        )
    return results
