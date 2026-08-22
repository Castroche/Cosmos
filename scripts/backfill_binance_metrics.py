from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.archive_data import (  # noqa: E402
    binance_daily_metrics_url,
    download_verified_archive,
    downsample_latest_observation,
    iter_binance_futures_metrics,
    metrics_to_research_points,
)
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
    load_external_source_evidence,
    verify_research_dataset,
)


def _days(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _load(dataset_id: str, method_name: str, model):
    return [model.model_validate(item) for item in getattr(api.store, method_name)(dataset_id)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Enrich an immutable Binance futures dataset with checksum-verified "
            "Data Vision open-interest and positioning history."
        )
    )
    parser.add_argument("dataset_id")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--max-days", type=int, default=400)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "data" / "archives" / "binance" / "metrics",
    )
    args = parser.parse_args()
    requested_days = list(_days(args.start, args.end))
    if not requested_days or len(requested_days) > args.max_days:
        raise SystemExit(f"requested {len(requested_days)} days; --max-days is {args.max_days}")
    if not 1 <= args.workers <= 32:
        raise SystemExit("--workers must be between 1 and 32")
    payload = api.store.load_research_dataset(args.dataset_id)
    if not payload:
        raise SystemExit(f"research dataset not found: {args.dataset_id}")
    source = ResearchDataset.model_validate(payload)
    if source.exchange != "binance" or source.market_type != "swap":
        raise SystemExit("official metrics backfill requires a Binance UM futures dataset")

    candles = _load(source.id, "load_research_dataset_candles", Candle)
    funding = _load(source.id, "load_research_dataset_funding", FundingRatePoint)
    existing_open_interest = _load(
        source.id,
        "load_research_dataset_open_interest",
        OpenInterestPoint,
    )
    existing_positioning = _load(
        source.id,
        "load_research_dataset_positioning",
        PositioningPoint,
    )
    basis = _load(source.id, "load_research_dataset_basis", BasisPoint)
    order_flow = _load(source.id, "load_research_dataset_order_flow", OrderFlowBar)
    liquidation_flow = _load(
        source.id,
        "load_research_dataset_liquidation_flow",
        LiquidationFlowPoint,
    )
    external_source_evidence = load_external_source_evidence(source)
    proxy_url = api.configuration_service.current_configuration().network_proxy_url
    compact_symbol = source.symbol.replace("/", "").split(":", 1)[0]
    destination = args.cache_dir / compact_symbol

    def download(day: date):
        url = binance_daily_metrics_url(source.symbol, day)
        archive, checksum = download_verified_archive(
            url,
            destination,
            proxy_url=proxy_url,
            timeout_seconds=240,
        )
        return day, url, archive, checksum

    downloaded = []
    metrics = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(download, day): day for day in requested_days}
        for completed, future in enumerate(as_completed(futures), start=1):
            day, url, archive, checksum = future.result()
            archive_metrics = list(iter_binance_futures_metrics(archive))
            if any(item.symbol != compact_symbol for item in archive_metrics):
                raise RuntimeError(f"metrics archive symbol mismatch: {archive}")
            metrics.extend(archive_metrics)
            downloaded.append(
                {
                    "day": day.isoformat(),
                    "url": url,
                    "sha256": checksum,
                    "bytes": archive.stat().st_size,
                    "points": len(archive_metrics),
                }
            )
            print(
                json.dumps(
                    {
                        "event": "binance_metrics_backfill_progress",
                        "dataset_id": source.id,
                        "symbol": source.symbol,
                        "day": day.isoformat(),
                        "completed_days": completed,
                        "total_days": len(requested_days),
                        "points": len(archive_metrics),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    archived_open_interest, archived_positioning = metrics_to_research_points(
        metrics,
        start=source.data_start,
        end=source.data_end,
    )
    open_interest_by_time = {item.timestamp: item for item in existing_open_interest}
    open_interest_by_time.update((item.timestamp, item) for item in archived_open_interest)
    positioning_by_time = {item.timestamp: item for item in existing_positioning}
    positioning_by_time.update((item.timestamp, item) for item in archived_positioning)
    open_interest = [open_interest_by_time[key] for key in sorted(open_interest_by_time)]
    positioning = [positioning_by_time[key] for key in sorted(positioning_by_time)]
    open_interest = downsample_latest_observation(
        open_interest,
        timeframe=source.timeframe,
    )
    positioning = downsample_latest_observation(
        positioning,
        timeframe=source.timeframe,
    )
    quality = SimpleNamespace(
        status=source.quality_status,
        source=f"{source.source}|binance-data-vision-metrics-sha256-v1",
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
        funding_rates=funding,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        research_role=source.research_role,
        source_dataset_id=(
            source.source_dataset_id
            if source.research_role != "general"
            else source.id
        ),
        blind_set_id=source.blind_set_id,
        split_boundary=source.split_boundary,
        embargo_bars=source.embargo_bars,
        blind_sealed=source.blind_sealed,
        external_source_evidence=external_source_evidence or None,
    )
    api.store.save_research_dataset(
        dataset,
        candles,
        funding,
        open_interest,
        positioning,
        basis,
        order_flow,
        liquidation_flow,
    )
    verification = verify_research_dataset(
        dataset,
        candles,
        funding,
        open_interest,
        positioning,
        basis,
        order_flow,
        liquidation_flow,
    )
    manifest = {
        "source_dataset_id": source.id,
        "dataset_id": dataset.id,
        "schema_version": dataset.schema_version,
        "requested_start": args.start.isoformat(),
        "requested_end": args.end.isoformat(),
        "archive_count": len(downloaded),
        "archives": sorted(downloaded, key=lambda item: item["day"]),
        "open_interest_count": dataset.open_interest_count,
        "open_interest_coverage_ratio": str(dataset.open_interest_coverage_ratio),
        "positioning_count": dataset.positioning_count,
        "positioning_coverage_ratio": str(dataset.positioning_coverage_ratio),
        "verified": verification["verified"],
        "created_at": datetime.now(UTC).isoformat(),
    }
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / f"binance-metrics-{dataset.id}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if verification["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
