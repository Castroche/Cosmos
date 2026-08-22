from __future__ import annotations

from enum import StrEnum

import numpy as np
from pydantic import BaseModel

from .strategy_backends import FloatArray, NumpyFastVectorBackend, StrategyIntentSeries
from .strategy_dsl import CanonicalStrategyIR


class IntegrityGateName(StrEnum):
    STATIC_LOOKAHEAD = "static_lookahead"
    DIFFERENTIAL_LOOKAHEAD = "differential_lookahead"
    RECURSIVE_STABILITY = "recursive_stability"
    WARMUP_STABILITY = "warmup_stability"
    TIME_AVAILABILITY = "time_availability"
    HISTORICAL_SIGNAL_IMMUTABILITY = "historical_signal_immutability"


class IntegrityGateResult(BaseModel, frozen=True):
    name: IntegrityGateName
    passed: bool
    reason_codes: tuple[str, ...] = ()


class StrategyIntegrityReport(BaseModel, frozen=True):
    gates: tuple[IntegrityGateResult, ...]

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)

    @property
    def reason_codes(self) -> list[str]:
        return [code for gate in self.gates for code in gate.reason_codes]


def _equal_prefix(left: StrategyIntentSeries, right: StrategyIntentSeries, length: int) -> bool:
    names = (
        "long_signal",
        "short_signal",
        "entry_intent",
        "exit_intent",
        "position_intent",
        "invalidation",
    )
    return all(
        np.array_equal(getattr(left, name)[:length], getattr(right, name)[:length])
        for name in names
    )


def run_integrity_gates(
    ir: CanonicalStrategyIR,
    features: dict[str, FloatArray],
    *,
    event_time_ns: np.ndarray | None = None,
    available_time_ns: np.ndarray | None = None,
    decision_time_ns: np.ndarray | None = None,
) -> StrategyIntegrityReport:
    """Run all pre-return semantic checks; no PnL or return series is accepted here."""
    backend = NumpyFastVectorBackend()
    length = len(next(iter(features.values())))
    static_reasons: list[str] = []
    for node in ir.nodes:
        if node.operator == "lag" and dict(node.parameters).get("periods", 1) < 0:
            static_reasons.append("negative_lag")
        if node.point_in_time_policy not in {"closed_bar", "asof", "static"}:
            static_reasons.append("unknown_pit_policy")
    static = IntegrityGateResult(
        name=IntegrityGateName.STATIC_LOOKAHEAD,
        passed=not static_reasons,
        reason_codes=tuple(sorted(set(static_reasons))),
    )
    full = backend.evaluate(ir, features)
    split = max(full.warmup + 2, length * 2 // 3)
    split = min(split, length - 1)
    prefix_features = {name: values[:split].copy() for name, values in features.items()}
    prefix = backend.evaluate(ir, prefix_features)

    changed_future = {name: values.copy() for name, values in features.items()}
    for values in changed_future.values():
        values[split:] = values[split:] * 13.0 + 7.0
    perturbed = backend.evaluate(ir, changed_future)
    differential_passed = _equal_prefix(full, perturbed, split)
    differential = IntegrityGateResult(
        name=IntegrityGateName.DIFFERENTIAL_LOOKAHEAD,
        passed=differential_passed,
        reason_codes=() if differential_passed else ("future_perturbation_changed_history",),
    )

    recursive_passed = _equal_prefix(prefix, full, split)
    recursive = IntegrityGateResult(
        name=IntegrityGateName.RECURSIVE_STABILITY,
        passed=recursive_passed,
        reason_codes=() if recursive_passed else ("prefix_recompute_mismatch",),
    )

    warmup_end = min(length, full.warmup + 2)
    warmup_passed = (
        not (
            full.long_signal[: full.warmup].any()
            or full.short_signal[: full.warmup].any()
            or full.position_intent[: full.warmup].any()
        )
        and warmup_end > full.warmup
    )
    warmup = IntegrityGateResult(
        name=IntegrityGateName.WARMUP_STABILITY,
        passed=warmup_passed,
        reason_codes=() if warmup_passed else ("warmup_signal_leak",),
    )

    if event_time_ns is None or available_time_ns is None or decision_time_ns is None:
        availability_passed = False
        availability_reasons = ("availability_metadata_missing",)
    else:
        availability_passed = (
            len(event_time_ns) == length
            and len(available_time_ns) == length
            and len(decision_time_ns) == length
            and bool(np.all(available_time_ns >= event_time_ns))
            and bool(np.all(available_time_ns <= decision_time_ns))
        )
        availability_reasons = () if availability_passed else ("invalid_availability_timeline",)
    availability = IntegrityGateResult(
        name=IntegrityGateName.TIME_AVAILABILITY,
        passed=availability_passed,
        reason_codes=availability_reasons,
    )

    append_count = min(8, length)
    appended_features: dict[str, FloatArray] = {}
    for name, values in features.items():
        tail = np.repeat(values[-1:, ...], append_count, axis=0).astype(np.float64)
        appended_features[name] = np.concatenate([values, tail])
    appended = backend.evaluate(ir, appended_features)
    immutable_passed = _equal_prefix(full, appended, length)
    immutable = IntegrityGateResult(
        name=IntegrityGateName.HISTORICAL_SIGNAL_IMMUTABILITY,
        passed=immutable_passed,
        reason_codes=() if immutable_passed else ("future_append_changed_history",),
    )
    return StrategyIntegrityReport(
        gates=(static, differential, recursive, warmup, availability, immutable)
    )
