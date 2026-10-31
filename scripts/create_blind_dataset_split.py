from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.application_services import ApplicationServices  # noqa: E402
api = ApplicationServices()
from cosmos_quant.domain import (  # noqa: E402
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from cosmos_quant.research import (  # noqa: E402
    ResearchDataset,
    build_research_dataset,
    verify_research_dataset,
)
from cosmos_quant.timeframes import TIMEFRAME_SECONDS  # noqa: E402


def _load_points(dataset_id: str, method_name: str, model):
    payloads = getattr(api.store, method_name)(dataset_id)
    return [model.model_validate(item) for item in payloads]


def _bounded(points, start: datetime, end: datetime):
    return [point for point in points if start <= point.timestamp <= end]


def _external_evidence(dataset: ResearchDataset) -> list[dict]:
    raw = dataset.acquisition_manifest.get("external_source_evidence_json", "")
    if not raw:
        return []
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{dataset.id} has invalid external source evidence") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{dataset.id} external source evidence is not a list")
    return payload


def _canonical_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()[:20]


def _parse_utc_boundary(value: str, *, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{option} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{option} must include a UTC offset")
    return parsed.astimezone(UTC)


def _bound_candle_windows(
    datasets: dict[str, list[Candle]],
    *,
    window_start: datetime | None = None,
    window_end_exclusive: datetime | None = None,
) -> dict[str, list[Candle]]:
    if window_start and window_end_exclusive and window_start >= window_end_exclusive:
        raise ValueError("window start must precede the exclusive end")
    bounded = {
        dataset_id: [
            candle
            for candle in candles
            if (window_start is None or candle.timestamp >= window_start)
            and (
                window_end_exclusive is None
                or candle.timestamp < window_end_exclusive
            )
        ]
        for dataset_id, candles in datasets.items()
    }
    empty = [dataset_id for dataset_id, candles in bounded.items() if not candles]
    if empty:
        raise ValueError(
            "requested research window leaves empty datasets: " + ", ".join(sorted(empty))
        )
    return bounded


def _align_common_candle_windows(
    datasets: dict[str, list[Candle]],
    *,
    window_start: datetime | None = None,
    window_end_exclusive: datetime | None = None,
) -> tuple[dict[str, list[Candle]], datetime, datetime]:
    if not datasets or any(not candles for candles in datasets.values()):
        raise ValueError("common-window alignment requires non-empty candle datasets")
    datasets = _bound_candle_windows(
        datasets,
        window_start=window_start,
        window_end_exclusive=window_end_exclusive,
    )
    common_start = max(candles[0].timestamp for candles in datasets.values())
    common_end = min(candles[-1].timestamp for candles in datasets.values())
    if common_start >= common_end:
        raise ValueError("research datasets do not share a usable time window")
    aligned = {
        dataset_id: [
            candle for candle in candles if common_start <= candle.timestamp <= common_end
        ]
        for dataset_id, candles in datasets.items()
    }
    reference_timestamps: list[datetime] | None = None
    for dataset_id, candles in aligned.items():
        timestamps = [candle.timestamp for candle in candles]
        if reference_timestamps is None:
            reference_timestamps = timestamps
        elif timestamps != reference_timestamps:
            raise ValueError(
                f"common-window timestamps are not identical for dataset {dataset_id}"
            )
    assert reference_timestamps
    return aligned, common_start, common_end


def _create_split_dataset(
    *,
    source: ResearchDataset,
    subset: list[Candle],
    funding: list[FundingRatePoint],
    open_interest: list[OpenInterestPoint],
    positioning: list[PositioningPoint],
    basis: list[BasisPoint],
    order_flow: list[OrderFlowBar],
    liquidation_flow: list[LiquidationFlowPoint],
    external_source_evidence: list[dict],
    quality,
    role: str,
    blind_set_id: str,
    split_boundary: datetime,
    embargo_bars: int,
    sealed: bool,
):
    start, end = subset[0].timestamp, subset[-1].timestamp
    subset_funding = _bounded(funding, start, end)
    subset_open_interest = _bounded(open_interest, start, end)
    subset_positioning = _bounded(positioning, start, end)
    subset_basis = _bounded(basis, start, end)
    subset_order_flow = _bounded(order_flow, start, end)
    subset_liquidation_flow = _bounded(liquidation_flow, start, end)
    dataset = build_research_dataset(
        subset,
        exchange=source.exchange,
        market_type=source.market_type,
        symbol=source.symbol,
        timeframe=source.timeframe,
        quality=quality,
        funding_rates=subset_funding,
        open_interest=subset_open_interest,
        positioning=subset_positioning,
        basis=subset_basis,
        order_flow=subset_order_flow,
        liquidation_flow=subset_liquidation_flow,
        research_role=role,
        source_dataset_id=source.id,
        blind_set_id=blind_set_id,
        split_boundary=split_boundary,
        embargo_bars=embargo_bars,
        blind_sealed=sealed,
        external_source_evidence=external_source_evidence or None,
    )
    api.store.save_research_dataset(
        dataset,
        subset,
        subset_funding,
        subset_open_interest,
        subset_positioning,
        subset_basis,
        subset_order_flow,
        subset_liquidation_flow,
    )
    verification = verify_research_dataset(
        dataset,
        subset,
        subset_funding,
        subset_open_interest,
        subset_positioning,
        subset_basis,
        subset_order_flow,
        subset_liquidation_flow,
    )
    if not verification["verified"]:
        raise RuntimeError(f"split dataset verification failed: {dataset.id}")
    return dataset, verification


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create fingerprint-bound development and sealed blind datasets using "
            "a chronological split with a purge/embargo gap."
        )
    )
    parser.add_argument("dataset_ids", nargs="+")
    parser.add_argument("--development-ratio", type=float, default=0.80)
    parser.add_argument("--embargo-days", type=float, default=7.0)
    parser.add_argument("--minimum-development-bars", type=int, default=500)
    parser.add_argument("--minimum-blind-bars", type=int, default=300)
    parser.add_argument(
        "--window-start",
        help="inclusive ISO-8601 UTC boundary applied before alignment and splitting",
    )
    parser.add_argument(
        "--window-end-exclusive",
        help="exclusive ISO-8601 UTC boundary applied before alignment and splitting",
    )
    parser.add_argument(
        "--common-window",
        action="store_true",
        help=(
            "align every source to the exact common timestamp window before applying "
            "one shared chronological split"
        ),
    )
    args = parser.parse_args()
    if not 0.55 <= args.development_ratio <= 0.90:
        raise SystemExit("--development-ratio must be between 0.55 and 0.90")
    if args.embargo_days < 0:
        raise SystemExit("--embargo-days must be non-negative")
    try:
        window_start = (
            _parse_utc_boundary(args.window_start, option="--window-start")
            if args.window_start
            else None
        )
        window_end_exclusive = (
            _parse_utc_boundary(
                args.window_end_exclusive,
                option="--window-end-exclusive",
            )
            if args.window_end_exclusive
            else None
        )
        if window_start and window_end_exclusive and window_start >= window_end_exclusive:
            raise ValueError("window start must precede the exclusive end")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    unique_source_ids = list(dict.fromkeys(args.dataset_ids))
    sources: dict[str, ResearchDataset] = {}
    candles_by_source: dict[str, list[Candle]] = {}
    for source_id in unique_source_ids:
        payload = api.store.load_research_dataset(source_id)
        if not payload:
            raise SystemExit(f"research dataset not found: {source_id}")
        sources[source_id] = ResearchDataset.model_validate(payload)
        candles_by_source[source_id] = _load_points(
            source_id,
            "load_research_dataset_candles",
            Candle,
        )
    common_start = None
    common_end = None
    if args.common_window:
        timeframes = {source.timeframe for source in sources.values()}
        if len(timeframes) != 1:
            raise SystemExit("--common-window requires one shared timeframe")
        try:
            candles_by_source, common_start, common_end = _align_common_candle_windows(
                candles_by_source,
                window_start=window_start,
                window_end_exclusive=window_end_exclusive,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif window_start or window_end_exclusive:
        try:
            candles_by_source = _bound_candle_windows(
                candles_by_source,
                window_start=window_start,
                window_end_exclusive=window_end_exclusive,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    split_policy = (
        "chronological-purged-bounded-holdout-v2"
        if window_start or window_end_exclusive
        else "chronological-purged-holdout-v1"
    )
    set_contract = {
        "source_dataset_ids": sorted(unique_source_ids),
        "development_ratio": format(args.development_ratio, ".8f"),
        "embargo_days": format(args.embargo_days, ".8f"),
        "policy": split_policy,
        "common_window": args.common_window,
        "common_window_start": common_start.isoformat() if common_start else None,
        "common_window_end": common_end.isoformat() if common_end else None,
        "requested_window_start": window_start.isoformat() if window_start else None,
        "requested_window_end_exclusive": (
            window_end_exclusive.isoformat() if window_end_exclusive else None
        ),
    }
    blind_set_id = f"blindset_{_canonical_hash(set_contract)}"
    results = []
    for source_id in unique_source_ids:
        source = sources[source_id]
        candles = candles_by_source[source_id]
        source_candles = _load_points(
            source_id,
            "load_research_dataset_candles",
            Candle,
        )
        funding = _load_points(
            source_id,
            "load_research_dataset_funding",
            FundingRatePoint,
        )
        open_interest = _load_points(
            source_id,
            "load_research_dataset_open_interest",
            OpenInterestPoint,
        )
        positioning = _load_points(
            source_id,
            "load_research_dataset_positioning",
            PositioningPoint,
        )
        basis = _load_points(
            source_id,
            "load_research_dataset_basis",
            BasisPoint,
        )
        order_flow = _load_points(
            source_id,
            "load_research_dataset_order_flow",
            OrderFlowBar,
        )
        liquidation_flow = _load_points(
            source_id,
            "load_research_dataset_liquidation_flow",
            LiquidationFlowPoint,
        )
        source_verification = verify_research_dataset(
            source,
            source_candles,
            funding,
            open_interest,
            positioning,
            basis,
            order_flow,
            liquidation_flow,
        )
        if not source_verification["verified"]:
            raise SystemExit(f"source dataset failed immutable verification: {source.id}")
        try:
            external_source_evidence = _external_evidence(source)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        interval_seconds = TIMEFRAME_SECONDS.get(source.timeframe)
        if not interval_seconds:
            raise SystemExit(f"unsupported timeframe for split: {source.timeframe}")
        split_index = int(len(candles) * args.development_ratio)
        embargo_bars = math.ceil(args.embargo_days * 86_400 / interval_seconds)
        development_end_index = split_index - embargo_bars
        if development_end_index < args.minimum_development_bars:
            raise SystemExit(
                f"{source_id} has only {development_end_index} development bars after embargo"
            )
        if len(candles) - split_index < args.minimum_blind_bars:
            raise SystemExit(f"{source_id} has only {len(candles) - split_index} blind bars")
        development_candles = candles[:development_end_index]
        blind_candles = candles[split_index:]
        split_boundary = blind_candles[0].timestamp
        quality = SimpleNamespace(
            status=source.quality_status,
            source=f"{source.source}|{split_policy}",
            gap_count=source.gap_count,
            duplicate_count=source.duplicate_count,
            invalid_count=source.invalid_count,
        )

        common = {
            "source": source,
            "funding": funding,
            "open_interest": open_interest,
            "positioning": positioning,
            "basis": basis,
            "order_flow": order_flow,
            "liquidation_flow": liquidation_flow,
            "external_source_evidence": external_source_evidence,
            "quality": quality,
            "blind_set_id": blind_set_id,
            "split_boundary": split_boundary,
            "embargo_bars": embargo_bars,
        }
        development, development_verification = _create_split_dataset(
            **common,
            role="development",
            subset=development_candles,
            sealed=False,
        )
        blind, blind_verification = _create_split_dataset(
            **common,
            role="blind",
            subset=blind_candles,
            sealed=True,
        )
        results.append(
            {
                "source_dataset_id": source.id,
                "source_fingerprint": source.data_fingerprint,
                "exchange": source.exchange,
                "symbol": source.symbol,
                "timeframe": source.timeframe,
                "split_boundary": split_boundary.isoformat(),
                "embargo_bars": embargo_bars,
                "development_dataset_id": development.id,
                "development_bars": development.candle_count,
                "development_verified": development_verification["verified"],
                "blind_dataset_id": blind.id,
                "blind_bars": blind.candle_count,
                "blind_verified": blind_verification["verified"],
            }
        )
    manifest = {
        "blind_set_id": blind_set_id,
        "contract": set_contract,
        "datasets": results,
        "created_at": datetime.now(UTC).isoformat(),
    }
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / f"{blind_set_id}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
