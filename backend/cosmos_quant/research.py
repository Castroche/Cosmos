from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, Field

from .backtest import BacktestResult
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .timeframes import TIMEFRAME_SECONDS


class ResearchJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BacktestJob(BaseModel):
    id: str
    plan_id: str
    status: ResearchJobStatus = ResearchJobStatus.QUEUED
    result: BacktestResult | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ResearchDataset(BaseModel):
    schema_version: int = 8
    id: str
    exchange: str
    market_type: str
    symbol: str
    timeframe: str
    candle_count: int = Field(ge=1)
    data_start: datetime
    data_end: datetime
    data_fingerprint: str
    quality_status: str = "unknown"
    source: str = "unknown"
    gap_count: int = 0
    duplicate_count: int = 0
    invalid_count: int = 0
    funding_count: int = 0
    funding_status: str = "unavailable"
    open_interest_count: int = 0
    open_interest_status: str = "unavailable"
    open_interest_coverage_ratio: Decimal = Decimal("0")
    positioning_count: int = 0
    positioning_status: str = "unavailable"
    positioning_coverage_ratio: Decimal = Decimal("0")
    basis_count: int = 0
    basis_status: str = "unavailable"
    basis_coverage_ratio: Decimal = Decimal("0")
    order_flow_count: int = 0
    order_flow_status: str = "unavailable"
    order_flow_coverage_ratio: Decimal = Decimal("0")
    liquidation_flow_count: int = 0
    liquidation_flow_status: str = "unavailable"
    liquidation_flow_coverage_ratio: Decimal = Decimal("0")
    timeframe_seconds: int = 0
    expected_candle_count: int = 0
    coverage_ratio: Decimal = Decimal("0")
    closed_bars_only: bool = True
    research_role: Literal["general", "development", "blind"] = "general"
    source_dataset_id: str | None = None
    blind_set_id: str | None = None
    split_boundary: datetime | None = None
    embargo_bars: int = Field(default=0, ge=0)
    blind_sealed: bool = False
    acquisition_manifest: dict[str, str | int | bool] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def load_external_source_evidence(dataset: ResearchDataset) -> list[dict]:
    """Return verified external evidence for a derived immutable dataset."""

    raw = dataset.acquisition_manifest.get("external_source_evidence_json", "")
    declared = dataset.acquisition_manifest.get("external_source_evidence_sha256", "")
    if not raw and not declared:
        return []
    if not isinstance(raw, str) or not isinstance(declared, str):
        raise ValueError("external source evidence metadata has an invalid type")
    if len(declared) != 64 or sha256(raw.encode("utf-8")).hexdigest() != declared:
        raise ValueError("external source evidence hash mismatch")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("external source evidence is not valid JSON") from exc
    if not isinstance(payload, list) or not payload or not all(
        isinstance(item, dict) for item in payload
    ):
        raise ValueError("external source evidence must be a non-empty list")
    return payload


def candle_data_fingerprint(
    candles: list[Candle],
    *,
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    funding_rates: list[FundingRatePoint] | None = None,
    open_interest: list[OpenInterestPoint] | None = None,
    positioning: list[PositioningPoint] | None = None,
    basis: list[BasisPoint] | None = None,
    order_flow: list[OrderFlowBar] | None = None,
    liquidation_flow: list[LiquidationFlowPoint] | None = None,
    fingerprint_provenance: dict[str, str | int | bool] | None = None,
) -> str:
    identity = {
        "exchange": exchange,
        "market_type": market_type,
        "symbol": symbol,
        "timeframe": timeframe,
    }
    if fingerprint_provenance:
        identity["fingerprint_provenance"] = fingerprint_provenance
    digest = sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    for candle in candles:
        digest.update(b"\n")
        digest.update(candle.model_dump_json(exclude_none=True).encode("utf-8"))
    for point in funding_rates or []:
        digest.update(b"\nfunding:")
        digest.update(point.model_dump_json(exclude_none=True).encode("utf-8"))
    for point in open_interest or []:
        digest.update(b"\nopen-interest:")
        digest.update(point.model_dump_json(exclude_none=True).encode("utf-8"))
    for point in positioning or []:
        digest.update(b"\npositioning:")
        digest.update(point.model_dump_json(exclude_none=True).encode("utf-8"))
    for point in basis or []:
        digest.update(b"\nbasis:")
        digest.update(point.model_dump_json(exclude_none=True).encode("utf-8"))
    for point in order_flow or []:
        digest.update(b"\norder-flow:")
        digest.update(point.model_dump_json(exclude_none=True).encode("utf-8"))
    for point in liquidation_flow or []:
        digest.update(b"\nliquidation-flow:")
        digest.update(point.model_dump_json(exclude_none=True).encode("utf-8"))
    return digest.hexdigest()[:24]


def build_research_dataset(
    candles: list[Candle],
    *,
    exchange: str,
    market_type: str,
    symbol: str,
    timeframe: str,
    quality=None,
    funding_rates: list[FundingRatePoint] | None = None,
    open_interest: list[OpenInterestPoint] | None = None,
    positioning: list[PositioningPoint] | None = None,
    basis: list[BasisPoint] | None = None,
    order_flow: list[OrderFlowBar] | None = None,
    liquidation_flow: list[LiquidationFlowPoint] | None = None,
    research_role: Literal["general", "development", "blind"] = "general",
    source_dataset_id: str | None = None,
    blind_set_id: str | None = None,
    split_boundary: datetime | None = None,
    embargo_bars: int = 0,
    blind_sealed: bool = False,
    external_source_evidence: list[dict[str, str | int | bool]] | None = None,
) -> ResearchDataset:
    if not candles:
        raise ValueError("research dataset requires at least one candle")
    fingerprint_provenance: dict[str, str | int | bool] = {}
    if research_role != "general" or source_dataset_id is not None:
        fingerprint_provenance = {
            "research_role": research_role,
            "source_dataset_id": source_dataset_id or "",
            "blind_set_id": blind_set_id or "",
            "split_boundary": split_boundary.isoformat() if split_boundary else "",
            "embargo_bars": embargo_bars,
            "blind_sealed": blind_sealed,
        }
    external_evidence_json = ""
    external_evidence_sha256 = ""
    if external_source_evidence:
        external_evidence_json = json.dumps(
            external_source_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        external_evidence_sha256 = sha256(
            external_evidence_json.encode("utf-8")
        ).hexdigest()
        fingerprint_provenance["external_source_evidence_sha256"] = (
            external_evidence_sha256
        )
    fingerprint = candle_data_fingerprint(
        candles,
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        fingerprint_provenance=fingerprint_provenance or None,
    )
    interval_seconds = TIMEFRAME_SECONDS.get(timeframe, 0)
    expected_count = (
        int((candles[-1].timestamp - candles[0].timestamp).total_seconds()) // interval_seconds + 1
        if interval_seconds
        else len(candles)
    )
    coverage_ratio = min(
        Decimal(len(candles)) / Decimal(max(expected_count, 1)),
        Decimal("1"),
    )
    open_interest_points = sorted(
        open_interest or [],
        key=lambda item: item.timestamp,
    )
    open_interest_coverage = Decimal("0")
    if len(open_interest_points) >= 2 and candles[-1].timestamp > candles[0].timestamp:
        covered_start = max(
            open_interest_points[0].timestamp,
            candles[0].timestamp,
        )
        covered_end = min(
            open_interest_points[-1].timestamp,
            candles[-1].timestamp,
        )
        if covered_end > covered_start:
            open_interest_coverage = min(
                Decimal(str((covered_end - covered_start).total_seconds()))
                / Decimal(str((candles[-1].timestamp - candles[0].timestamp).total_seconds())),
                Decimal("1"),
            )
    positioning_points = sorted(
        positioning or [],
        key=lambda item: item.timestamp,
    )
    positioning_coverage = Decimal("0")
    if len(positioning_points) >= 2 and candles[-1].timestamp > candles[0].timestamp:
        covered_start = max(
            positioning_points[0].timestamp,
            candles[0].timestamp,
        )
        covered_end = min(
            positioning_points[-1].timestamp,
            candles[-1].timestamp,
        )
        if covered_end > covered_start:
            positioning_coverage = min(
                Decimal(str((covered_end - covered_start).total_seconds()))
                / Decimal(str((candles[-1].timestamp - candles[0].timestamp).total_seconds())),
                Decimal("1"),
            )
    basis_points = sorted(basis or [], key=lambda item: item.timestamp)
    basis_coverage = Decimal("0")
    if len(basis_points) >= 2 and candles[-1].timestamp > candles[0].timestamp:
        covered_start = max(basis_points[0].timestamp, candles[0].timestamp)
        covered_end = min(basis_points[-1].timestamp, candles[-1].timestamp)
        if covered_end > covered_start:
            basis_coverage = min(
                Decimal(str((covered_end - covered_start).total_seconds()))
                / Decimal(str((candles[-1].timestamp - candles[0].timestamp).total_seconds())),
                Decimal("1"),
            )
    order_flow_points = sorted(order_flow or [], key=lambda item: item.timestamp)
    order_flow_coverage = Decimal("0")
    if len(order_flow_points) >= 2 and candles[-1].timestamp > candles[0].timestamp:
        covered_start = max(order_flow_points[0].timestamp, candles[0].timestamp)
        covered_end = min(order_flow_points[-1].timestamp, candles[-1].timestamp)
        if covered_end > covered_start:
            order_flow_coverage = min(
                Decimal(str((covered_end - covered_start).total_seconds()))
                / Decimal(str((candles[-1].timestamp - candles[0].timestamp).total_seconds())),
                Decimal("1"),
            )
    liquidation_flow_points = sorted(
        liquidation_flow or [],
        key=lambda item: item.timestamp,
    )
    liquidation_flow_coverage = Decimal("0")
    if len(liquidation_flow_points) >= 2 and candles[-1].timestamp > candles[0].timestamp:
        covered_start = max(liquidation_flow_points[0].timestamp, candles[0].timestamp)
        covered_end = min(liquidation_flow_points[-1].timestamp, candles[-1].timestamp)
        if covered_end > covered_start:
            liquidation_flow_coverage = min(
                Decimal(str((covered_end - covered_start).total_seconds()))
                / Decimal(str((candles[-1].timestamp - candles[0].timestamp).total_seconds())),
                Decimal("1"),
            )
    return ResearchDataset(
        id=f"ds_{fingerprint}",
        exchange=exchange,
        market_type=market_type,
        symbol=symbol,
        timeframe=timeframe,
        candle_count=len(candles),
        data_start=candles[0].timestamp,
        data_end=candles[-1].timestamp,
        data_fingerprint=fingerprint,
        quality_status=getattr(quality, "status", "unknown"),
        source=getattr(quality, "source", "unknown"),
        gap_count=getattr(quality, "gap_count", 0),
        duplicate_count=getattr(quality, "duplicate_count", 0),
        invalid_count=getattr(quality, "invalid_count", 0),
        funding_count=len(funding_rates or []),
        funding_status="recorded" if funding_rates else "unavailable",
        open_interest_count=len(open_interest_points),
        open_interest_status=("recorded" if open_interest_points else "unavailable"),
        open_interest_coverage_ratio=open_interest_coverage.quantize(Decimal("0.0001")),
        positioning_count=len(positioning_points),
        positioning_status=("recorded" if positioning_points else "unavailable"),
        positioning_coverage_ratio=positioning_coverage.quantize(Decimal("0.0001")),
        basis_count=len(basis_points),
        basis_status=("recorded" if basis_points else "unavailable"),
        basis_coverage_ratio=basis_coverage.quantize(Decimal("0.0001")),
        order_flow_count=len(order_flow_points),
        order_flow_status=("recorded" if order_flow_points else "unavailable"),
        order_flow_coverage_ratio=order_flow_coverage.quantize(Decimal("0.0001")),
        liquidation_flow_count=len(liquidation_flow_points),
        liquidation_flow_status=(
            "recorded" if liquidation_flow_points else "unavailable"
        ),
        liquidation_flow_coverage_ratio=liquidation_flow_coverage.quantize(
            Decimal("0.0001")
        ),
        timeframe_seconds=interval_seconds,
        expected_candle_count=expected_count,
        coverage_ratio=coverage_ratio.quantize(Decimal("0.0001")),
        research_role=research_role,
        source_dataset_id=source_dataset_id,
        blind_set_id=blind_set_id,
        split_boundary=split_boundary,
        embargo_bars=embargo_bars,
        blind_sealed=blind_sealed,
        acquisition_manifest={
            "fingerprint_algorithm": "sha256-canonical-json-v1",
            "deduplicated": True,
            "sorted_ascending": True,
            "closed_bars_only": True,
            "funding_bound_to_fingerprint": bool(funding_rates),
            "open_interest_bound_to_fingerprint": bool(open_interest_points),
            "positioning_bound_to_fingerprint": bool(positioning_points),
            "basis_bound_to_fingerprint": bool(basis_points),
            "order_flow_bound_to_fingerprint": bool(order_flow_points),
            "liquidation_flow_bound_to_fingerprint": bool(liquidation_flow_points),
            "external_evidence_bound_to_fingerprint": bool(external_source_evidence),
            "external_source_evidence_json": external_evidence_json,
            "external_source_evidence_sha256": external_evidence_sha256,
            "fingerprint_provenance_json": (
                json.dumps(
                    fingerprint_provenance,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if fingerprint_provenance
                else ""
            ),
            "source": getattr(quality, "source", "unknown"),
        },
    )


def verify_research_dataset(
    dataset: ResearchDataset,
    candles: list[Candle],
    funding_rates: list[FundingRatePoint] | None = None,
    open_interest: list[OpenInterestPoint] | None = None,
    positioning: list[PositioningPoint] | None = None,
    basis: list[BasisPoint] | None = None,
    order_flow: list[OrderFlowBar] | None = None,
    liquidation_flow: list[LiquidationFlowPoint] | None = None,
) -> dict[str, bool | int | str]:
    """Recompute immutable provenance before research or execution uses a dataset."""

    raw_provenance = dataset.acquisition_manifest.get("fingerprint_provenance_json", "")
    try:
        fingerprint_provenance = (
            json.loads(raw_provenance) if isinstance(raw_provenance, str) and raw_provenance else {}
        )
    except json.JSONDecodeError:
        fingerprint_provenance = {}
    raw_external_evidence = dataset.acquisition_manifest.get(
        "external_source_evidence_json",
        "",
    )
    declared_external_evidence_hash = dataset.acquisition_manifest.get(
        "external_source_evidence_sha256",
        "",
    )
    external_source_evidence_matches = True
    if raw_external_evidence or declared_external_evidence_hash:
        try:
            decoded_external_evidence = json.loads(str(raw_external_evidence))
            external_source_evidence_matches = bool(
                isinstance(decoded_external_evidence, list)
                and decoded_external_evidence
                and isinstance(declared_external_evidence_hash, str)
                and len(declared_external_evidence_hash) == 64
                and sha256(str(raw_external_evidence).encode("utf-8")).hexdigest()
                == declared_external_evidence_hash
            )
        except json.JSONDecodeError:
            external_source_evidence_matches = False
    expected_provenance: dict[str, str | int | bool] = {}
    if dataset.research_role != "general" or dataset.source_dataset_id is not None:
        expected_provenance = {
            "research_role": dataset.research_role,
            "source_dataset_id": dataset.source_dataset_id or "",
            "blind_set_id": dataset.blind_set_id or "",
            "split_boundary": (
                dataset.split_boundary.isoformat() if dataset.split_boundary else ""
            ),
            "embargo_bars": dataset.embargo_bars,
            "blind_sealed": dataset.blind_sealed,
        }
    if dataset.acquisition_manifest.get("external_evidence_bound_to_fingerprint") is True:
        expected_provenance["external_source_evidence_sha256"] = str(
            declared_external_evidence_hash
        )
    provenance_matches = fingerprint_provenance == expected_provenance
    fingerprint = candle_data_fingerprint(
        candles,
        exchange=dataset.exchange,
        market_type=dataset.market_type,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        funding_rates=funding_rates,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        fingerprint_provenance=fingerprint_provenance or None,
    )
    ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(candles, candles[1:], strict=False)
    )
    count_matches = len(candles) == dataset.candle_count
    bounds_match = bool(
        candles
        and candles[0].timestamp == dataset.data_start
        and candles[-1].timestamp == dataset.data_end
    )
    fingerprint_matches = fingerprint == dataset.data_fingerprint
    funding_points = funding_rates or []
    funding_count_matches = len(funding_points) == dataset.funding_count
    funding_ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(
            funding_points,
            funding_points[1:],
            strict=False,
        )
    )
    funding_causal = all(
        dataset.data_start <= point.timestamp <= dataset.data_end for point in funding_points
    )
    open_interest_points = open_interest or []
    open_interest_count_matches = len(open_interest_points) == dataset.open_interest_count
    open_interest_ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(
            open_interest_points,
            open_interest_points[1:],
            strict=False,
        )
    )
    open_interest_causal = all(
        dataset.data_start <= point.timestamp <= dataset.data_end for point in open_interest_points
    )
    positioning_points = positioning or []
    positioning_count_matches = len(positioning_points) == dataset.positioning_count
    positioning_ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(
            positioning_points,
            positioning_points[1:],
            strict=False,
        )
    )
    positioning_causal = all(
        dataset.data_start <= point.timestamp <= dataset.data_end for point in positioning_points
    )
    basis_points = basis or []
    basis_count_matches = len(basis_points) == dataset.basis_count
    basis_ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(basis_points, basis_points[1:], strict=False)
    )
    basis_causal = all(
        dataset.data_start <= point.timestamp <= dataset.data_end for point in basis_points
    )
    order_flow_points = order_flow or []
    order_flow_count_matches = len(order_flow_points) == dataset.order_flow_count
    order_flow_ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(
            order_flow_points,
            order_flow_points[1:],
            strict=False,
        )
    )
    order_flow_causal = all(
        dataset.data_start <= point.timestamp <= dataset.data_end for point in order_flow_points
    )
    liquidation_flow_points = liquidation_flow or []
    liquidation_flow_count_matches = (
        len(liquidation_flow_points) == dataset.liquidation_flow_count
    )
    liquidation_flow_ordered = all(
        previous.timestamp < current.timestamp
        for previous, current in zip(
            liquidation_flow_points,
            liquidation_flow_points[1:],
            strict=False,
        )
    )
    liquidation_flow_causal = all(
        dataset.data_start <= point.timestamp <= dataset.data_end
        and point.timestamp == point.interval_start + timedelta(hours=1)
        and point.source_market_type == "coin_m_inverse_perpetual"
        and point.evidence_role == "cross_contract_underlying_liquidation_context"
        for point in liquidation_flow_points
    )
    return {
        "verified": bool(
            fingerprint_matches
            and count_matches
            and bounds_match
            and ordered
            and funding_count_matches
            and funding_ordered
            and funding_causal
            and open_interest_count_matches
            and open_interest_ordered
            and open_interest_causal
            and positioning_count_matches
            and positioning_ordered
            and positioning_causal
            and basis_count_matches
            and basis_ordered
            and basis_causal
            and order_flow_count_matches
            and order_flow_ordered
            and order_flow_causal
            and liquidation_flow_count_matches
            and liquidation_flow_ordered
            and liquidation_flow_causal
            and provenance_matches
            and external_source_evidence_matches
        ),
        "fingerprint_matches": fingerprint_matches,
        "count_matches": count_matches,
        "bounds_match": bounds_match,
        "strictly_ordered_unique": ordered,
        "funding_count_matches": funding_count_matches,
        "funding_strictly_ordered_unique": funding_ordered,
        "funding_causal": funding_causal,
        "open_interest_count_matches": open_interest_count_matches,
        "open_interest_strictly_ordered_unique": open_interest_ordered,
        "open_interest_causal": open_interest_causal,
        "positioning_count_matches": positioning_count_matches,
        "positioning_strictly_ordered_unique": positioning_ordered,
        "positioning_causal": positioning_causal,
        "basis_count_matches": basis_count_matches,
        "basis_strictly_ordered_unique": basis_ordered,
        "basis_causal": basis_causal,
        "order_flow_count_matches": order_flow_count_matches,
        "order_flow_strictly_ordered_unique": order_flow_ordered,
        "order_flow_causal": order_flow_causal,
        "liquidation_flow_count_matches": liquidation_flow_count_matches,
        "liquidation_flow_strictly_ordered_unique": liquidation_flow_ordered,
        "liquidation_flow_causal": liquidation_flow_causal,
        "fingerprint_provenance_matches": provenance_matches,
        "external_source_evidence_matches": external_source_evidence_matches,
        "actual_candle_count": len(candles),
        "actual_fingerprint": fingerprint,
    }
