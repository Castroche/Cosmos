from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

from pydantic import BaseModel, Field, model_validator

from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .research import (
    ResearchDataset,
    build_research_dataset,
    load_external_source_evidence,
    verify_research_dataset,
)
from .research_universe import ResearchUniverseSnapshot, validate_universe_snapshot
from .timeframes import TIMEFRAME_SECONDS


class SealedHoldoutRequest(BaseModel):
    dataset_ids: list[str] = Field(min_length=1, max_length=100)
    universe_snapshot_id: str
    development_ratio: float = Field(default=0.80, ge=0.55, le=0.90)
    embargo_days: float = Field(default=7.0, ge=0, le=90)
    minimum_development_bars: int = Field(default=500, ge=100)
    minimum_blind_bars: int = Field(default=300, ge=100)
    common_window: bool = True
    window_start: datetime | None = None
    window_end_exclusive: datetime | None = None

    @model_validator(mode="after")
    def validate_window(self):
        for value in (self.window_start, self.window_end_exclusive):
            if value is not None and value.tzinfo is None:
                raise ValueError("holdout window boundaries must be timezone-aware")
        if (
            self.window_start is not None
            and self.window_end_exclusive is not None
            and self.window_start >= self.window_end_exclusive
        ):
            raise ValueError("holdout window start must precede its exclusive end")
        if len(self.dataset_ids) != len(set(self.dataset_ids)):
            raise ValueError("holdout source dataset identifiers must be unique")
        return self


class SealedHoldoutDataset(BaseModel):
    source_dataset_id: str
    source_fingerprint: str
    exchange: str
    symbol: str
    timeframe: str
    split_boundary: datetime
    embargo_bars: int
    development_dataset_id: str
    development_bars: int
    blind_dataset_id: str
    blind_bars: int


class SealedHoldoutReport(BaseModel):
    blind_set_id: str
    universe_snapshot_id: str
    contract: dict[str, object]
    datasets: list[SealedHoldoutDataset]
    development_dataset_ids: list[str]
    blind_dataset_ids: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _canonical_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:20]


def _load_points(store, dataset_id: str, method_name: str, model):
    return [
        model.model_validate(item)
        for item in getattr(store, method_name)(dataset_id)
    ]


def _bound_points(points, start: datetime, end: datetime):
    return [point for point in points if start <= point.timestamp <= end]


def _bound_candles(
    datasets: dict[str, list[Candle]],
    *,
    start: datetime | None,
    end_exclusive: datetime | None,
) -> dict[str, list[Candle]]:
    bounded = {
        dataset_id: [
            candle
            for candle in candles
            if (start is None or candle.timestamp >= start)
            and (end_exclusive is None or candle.timestamp < end_exclusive)
        ]
        for dataset_id, candles in datasets.items()
    }
    empty = sorted(dataset_id for dataset_id, candles in bounded.items() if not candles)
    if empty:
        raise ValueError("holdout window leaves empty datasets: " + ", ".join(empty))
    return bounded


def _align_common_window(
    datasets: dict[str, list[Candle]],
) -> tuple[dict[str, list[Candle]], datetime, datetime]:
    common_start = max(candles[0].timestamp for candles in datasets.values())
    common_end = min(candles[-1].timestamp for candles in datasets.values())
    if common_start >= common_end:
        raise ValueError("holdout sources do not share a usable time window")
    aligned = {
        dataset_id: [
            candle for candle in candles if common_start <= candle.timestamp <= common_end
        ]
        for dataset_id, candles in datasets.items()
    }
    reference: list[datetime] | None = None
    for dataset_id, candles in aligned.items():
        timestamps = [candle.timestamp for candle in candles]
        if reference is None:
            reference = timestamps
        elif timestamps != reference:
            raise ValueError(
                f"holdout common timestamps differ for source dataset {dataset_id}"
            )
    if not reference:
        raise ValueError("holdout common window is empty")
    return aligned, common_start, common_end


def _verify_source(store, source: ResearchDataset) -> tuple[list[Candle], dict[str, list]]:
    candles = _load_points(store, source.id, "load_research_dataset_candles", Candle)
    features = {
        "funding": _load_points(
            store,
            source.id,
            "load_research_dataset_funding",
            FundingRatePoint,
        ),
        "open_interest": _load_points(
            store,
            source.id,
            "load_research_dataset_open_interest",
            OpenInterestPoint,
        ),
        "positioning": _load_points(
            store,
            source.id,
            "load_research_dataset_positioning",
            PositioningPoint,
        ),
        "basis": _load_points(
            store,
            source.id,
            "load_research_dataset_basis",
            BasisPoint,
        ),
        "order_flow": _load_points(
            store,
            source.id,
            "load_research_dataset_order_flow",
            OrderFlowBar,
        ),
        "liquidation_flow": _load_points(
            store,
            source.id,
            "load_research_dataset_liquidation_flow",
            LiquidationFlowPoint,
        ),
    }
    verification = verify_research_dataset(
        source,
        candles,
        features["funding"],
        features["open_interest"],
        features["positioning"],
        features["basis"],
        features["order_flow"],
        features["liquidation_flow"],
    )
    if not verification["verified"]:
        raise ValueError(f"source dataset failed immutable verification: {source.id}")
    return candles, features


def _create_subset(
    store,
    *,
    source: ResearchDataset,
    candles: list[Candle],
    features: dict[str, list],
    role: str,
    blind_set_id: str,
    split_boundary: datetime,
    embargo_bars: int,
    source_label: str,
) -> ResearchDataset:
    start, end = candles[0].timestamp, candles[-1].timestamp
    bounded = {
        name: _bound_points(points, start, end) for name, points in features.items()
    }
    quality = SimpleNamespace(
        status=source.quality_status,
        source=f"{source.source}|{source_label}",
        gap_count=source.gap_count,
        duplicate_count=source.duplicate_count,
        invalid_count=source.invalid_count,
    )
    dataset = build_research_dataset(
        candles,
        exchange=source.exchange,
        market_type=source.market_type,
        symbol=source.symbol,
        timeframe=source.timeframe,
        quality=quality,
        funding_rates=bounded["funding"],
        open_interest=bounded["open_interest"],
        positioning=bounded["positioning"],
        basis=bounded["basis"],
        order_flow=bounded["order_flow"],
        liquidation_flow=bounded["liquidation_flow"],
        research_role=role,
        source_dataset_id=source.id,
        blind_set_id=blind_set_id,
        split_boundary=split_boundary,
        embargo_bars=embargo_bars,
        blind_sealed=role == "blind",
        external_source_evidence=load_external_source_evidence(source) or None,
    )
    store.save_research_dataset(
        dataset,
        candles,
        bounded["funding"],
        bounded["open_interest"],
        bounded["positioning"],
        bounded["basis"],
        bounded["order_flow"],
        bounded["liquidation_flow"],
    )
    verification = verify_research_dataset(
        dataset,
        candles,
        bounded["funding"],
        bounded["open_interest"],
        bounded["positioning"],
        bounded["basis"],
        bounded["order_flow"],
        bounded["liquidation_flow"],
    )
    if not verification["verified"]:
        raise RuntimeError(f"sealed holdout verification failed: {dataset.id}")
    return dataset


def seal_research_holdout(store, request: SealedHoldoutRequest) -> SealedHoldoutReport:
    snapshot_payload = store.load_research_universe_snapshot(request.universe_snapshot_id)
    if snapshot_payload is None:
        raise ValueError("frozen research universe snapshot was not found")
    snapshot = ResearchUniverseSnapshot.model_validate(snapshot_payload)
    validate_universe_snapshot(snapshot)

    sources: dict[str, ResearchDataset] = {}
    candles_by_source: dict[str, list[Candle]] = {}
    features_by_source: dict[str, dict[str, list]] = {}
    for dataset_id in request.dataset_ids:
        payload = store.load_research_dataset(dataset_id)
        if payload is None:
            raise ValueError(f"research source dataset was not found: {dataset_id}")
        source = ResearchDataset.model_validate(payload)
        if source.research_role != "general" or source.blind_set_id:
            raise ValueError("holdout sources must be unsplit general research datasets")
        candles, features = _verify_source(store, source)
        sources[dataset_id] = source
        candles_by_source[dataset_id] = candles
        features_by_source[dataset_id] = features

    expected_keys = {
        (
            item.entry.exchange,
            item.entry.symbol,
            timeframe,
            item.entry.market_type,
        )
        for item in snapshot.instruments
        if item.selected
        for timeframe in snapshot.policy.timeframes
    }
    actual_keys = {
        (source.exchange, source.symbol, source.timeframe, source.market_type)
        for source in sources.values()
    }
    if actual_keys != expected_keys or len(sources) != len(expected_keys):
        raise ValueError("holdout sources must exactly cover the frozen universe")

    requested_window_start = (
        request.window_start.astimezone(UTC) if request.window_start else None
    )
    if requested_window_start is not None and requested_window_start < snapshot.as_of:
        raise ValueError("holdout window start cannot predate the frozen universe")
    window_start = requested_window_start or snapshot.as_of
    window_end = (
        request.window_end_exclusive.astimezone(UTC)
        if request.window_end_exclusive
        else None
    )
    candles_by_source = _bound_candles(
        candles_by_source,
        start=window_start,
        end_exclusive=window_end,
    )
    if request.common_window:
        candles_by_source, common_start, common_end = _align_common_window(
            candles_by_source
        )
    else:
        common_start = common_end = None
    actual_start = min(candles[0].timestamp for candles in candles_by_source.values())
    if actual_start < snapshot.as_of:
        raise ValueError("holdout evidence must start at or after the frozen universe time")

    contract: dict[str, object] = {
        "source_dataset_ids": sorted(request.dataset_ids),
        "source_fingerprints": sorted(
            source.data_fingerprint for source in sources.values()
        ),
        "universe_snapshot_id": snapshot.id,
        "universe_snapshot_fingerprint": snapshot.fingerprint,
        "development_ratio": format(request.development_ratio, ".8f"),
        "embargo_days": format(request.embargo_days, ".8f"),
        "policy": "chronological-purged-bounded-holdout-v2",
        "common_window": request.common_window,
        "common_window_start": common_start.isoformat() if common_start else None,
        "common_window_end": common_end.isoformat() if common_end else None,
        "requested_window_start": (
            requested_window_start.isoformat() if requested_window_start else None
        ),
        "effective_window_start": window_start.isoformat(),
        "requested_window_end_exclusive": window_end.isoformat() if window_end else None,
    }
    blind_set_id = f"blindset_{_canonical_hash(contract)}"
    results: list[SealedHoldoutDataset] = []
    for dataset_id in request.dataset_ids:
        source = sources[dataset_id]
        candles = candles_by_source[dataset_id]
        interval_seconds = TIMEFRAME_SECONDS.get(source.timeframe)
        if not interval_seconds:
            raise ValueError(f"unsupported holdout timeframe: {source.timeframe}")
        split_index = int(len(candles) * request.development_ratio)
        embargo_bars = math.ceil(request.embargo_days * 86_400 / interval_seconds)
        development_end = split_index - embargo_bars
        if development_end < request.minimum_development_bars:
            raise ValueError(f"{dataset_id} has insufficient development bars")
        if len(candles) - split_index < request.minimum_blind_bars:
            raise ValueError(f"{dataset_id} has insufficient blind bars")
        development_candles = candles[:development_end]
        blind_candles = candles[split_index:]
        split_boundary = blind_candles[0].timestamp
        common = {
            "store": store,
            "source": source,
            "features": features_by_source[dataset_id],
            "blind_set_id": blind_set_id,
            "split_boundary": split_boundary,
            "embargo_bars": embargo_bars,
            "source_label": str(contract["policy"]),
        }
        development = _create_subset(
            **common,
            candles=development_candles,
            role="development",
        )
        blind = _create_subset(
            **common,
            candles=blind_candles,
            role="blind",
        )
        results.append(
            SealedHoldoutDataset(
                source_dataset_id=source.id,
                source_fingerprint=source.data_fingerprint,
                exchange=source.exchange,
                symbol=source.symbol,
                timeframe=source.timeframe,
                split_boundary=split_boundary,
                embargo_bars=embargo_bars,
                development_dataset_id=development.id,
                development_bars=development.candle_count,
                blind_dataset_id=blind.id,
                blind_bars=blind.candle_count,
            )
        )
    return SealedHoldoutReport(
        blind_set_id=blind_set_id,
        universe_snapshot_id=snapshot.id,
        contract=contract,
        datasets=results,
        development_dataset_ids=[item.development_dataset_id for item in results],
        blind_dataset_ids=[item.blind_dataset_id for item in results],
    )
