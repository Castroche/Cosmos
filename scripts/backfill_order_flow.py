from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.application_services import ApplicationServices  # noqa: E402
api = ApplicationServices()
from cosmos_quant.archive_data import (  # noqa: E402
    aggregate_order_flow,
    binance_daily_aggtrade_url,
    binance_daily_kline_url,
    binance_monthly_aggtrade_url,
    binance_monthly_kline_url,
    download_verified_archive,
    downsample_latest_observation,
    iter_binance_aggregate_trades,
    iter_binance_kline_order_flow,
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


def _months(start: date, end: date):
    current = start.replace(day=1)
    while current <= end:
        yield current
        current = (
            current.replace(year=current.year + 1, month=1)
            if current.month == 12
            else current.replace(month=current.month + 1)
        )


def _next_month(month: date) -> date:
    return (
        month.replace(year=month.year + 1, month=1)
        if month.month == 12
        else month.replace(month=month.month + 1)
    )


def build_archive_requests(
    start: date,
    end: date,
    archive_period: str,
    *,
    current_date: date | None = None,
) -> list[tuple[str, date]]:
    """Use daily files for an unclosed tail month with no monthly archive yet."""

    if archive_period == "daily":
        return [("daily", day) for day in _days(start, end)]
    current_month = (current_date or datetime.now(UTC).date()).replace(day=1)
    requests: list[tuple[str, date]] = []
    for month in _months(start, end):
        if month < current_month:
            requests.append(("monthly", month))
            continue
        first_day = max(start, month)
        last_day = min(end, _next_month(month) - timedelta(days=1))
        requests.extend(("daily", day) for day in _days(first_day, last_day))
    return requests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_id")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--max-days", type=int, default=31)
    parser.add_argument(
        "--archive-period",
        choices=("daily", "monthly"),
        default="daily",
        help="Use monthly archives for long ranges to reduce request overhead.",
    )
    parser.add_argument(
        "--source-type",
        choices=("aggTrades", "klines"),
        default="aggTrades",
        help=(
            "Use official closed futures klines for efficient taker flow, or "
            "aggTrades when per-aggregate-trade fields are required."
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "data" / "archives" / "binance",
    )
    args = parser.parse_args()
    requested_days = list(_days(args.start, args.end))
    if not requested_days or len(requested_days) > args.max_days:
        raise SystemExit(f"requested {len(requested_days)} days; --max-days is {args.max_days}")
    archive_requests = build_archive_requests(
        args.start,
        args.end,
        args.archive_period,
    )
    payload = api.store.load_research_dataset(args.dataset_id)
    if not payload:
        raise SystemExit(f"research dataset not found: {args.dataset_id}")
    source = ResearchDataset.model_validate(payload)
    if source.exchange != "binance" or source.market_type != "swap":
        raise SystemExit("official order-flow backfill requires Binance UM futures")
    candles = [
        Candle.model_validate(item) for item in api.store.load_research_dataset_candles(source.id)
    ]
    funding = [
        FundingRatePoint.model_validate(item)
        for item in api.store.load_research_dataset_funding(source.id)
    ]
    open_interest = [
        OpenInterestPoint.model_validate(item)
        for item in api.store.load_research_dataset_open_interest(source.id)
    ]
    positioning = [
        PositioningPoint.model_validate(item)
        for item in api.store.load_research_dataset_positioning(source.id)
    ]
    open_interest = downsample_latest_observation(
        open_interest,
        timeframe=source.timeframe,
    )
    positioning = downsample_latest_observation(
        positioning,
        timeframe=source.timeframe,
    )
    basis = [
        BasisPoint.model_validate(item) for item in api.store.load_research_dataset_basis(source.id)
    ]
    existing_flow = [
        OrderFlowBar.model_validate(item)
        for item in api.store.load_research_dataset_order_flow(source.id)
    ]
    liquidation_flow = [
        LiquidationFlowPoint.model_validate(item)
        for item in api.store.load_research_dataset_liquidation_flow(source.id)
    ]
    external_source_evidence = load_external_source_evidence(source)
    proxy_url = api.configuration_service.current_configuration().network_proxy_url
    symbol_directory = source.symbol.replace("/", "").split(":", 1)[0]
    archive_destination = args.cache_dir / args.source_type / symbol_directory
    if args.source_type == "klines":
        archive_destination /= source.timeframe
    flow_by_timestamp = {item.timestamp: item for item in existing_flow}
    archives = []
    requested_start = datetime.combine(args.start, datetime.min.time(), tzinfo=UTC)
    requested_end = datetime.combine(
        args.end + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    ) - timedelta(microseconds=1)
    total_archives = len(archive_requests)
    for completed_archives, (period_kind, period) in enumerate(archive_requests, start=1):
        if args.source_type == "klines":
            url = (
                binance_daily_kline_url(source.symbol, source.timeframe, period)
                if period_kind == "daily"
                else binance_monthly_kline_url(source.symbol, source.timeframe, period)
            )
        else:
            url = (
                binance_daily_aggtrade_url(source.symbol, period)
                if period_kind == "daily"
                else binance_monthly_aggtrade_url(source.symbol, period)
            )
        archive, checksum = download_verified_archive(
            url,
            archive_destination,
            proxy_url=proxy_url,
            timeout_seconds=240,
        )
        period_start = datetime.combine(period, datetime.min.time(), tzinfo=UTC)
        period_end = (
            period_start + timedelta(days=1)
            if period_kind == "daily"
            else datetime.combine(_next_month(period), datetime.min.time(), tzinfo=UTC)
        )
        bounded_start = max(period_start, source.data_start, requested_start)
        bounded_end = min(
            period_end - timedelta(microseconds=1),
            source.data_end,
            requested_end,
        )
        bars = (
            list(
                iter_binance_kline_order_flow(
                    archive,
                    start=bounded_start,
                    end=bounded_end,
                )
            )
            if args.source_type == "klines"
            else aggregate_order_flow(
                iter_binance_aggregate_trades(archive),
                timeframe=source.timeframe,
                start=bounded_start,
                end=bounded_end,
            )
        )
        flow_by_timestamp.update((item.timestamp, item) for item in bars)
        archives.append(
            {
                "url": url,
                "sha256": checksum,
                "bytes": archive.stat().st_size,
                "bars": len(bars),
            }
        )
        print(
            json.dumps(
                {
                    "event": "order_flow_backfill_progress",
                    "dataset_id": source.id,
                    "symbol": source.symbol,
                    "archive_period": period_kind,
                    "source_type": args.source_type,
                    "period": period.isoformat(),
                    "completed_archives": completed_archives,
                    "total_archives": total_archives,
                    "archive_bytes": archive.stat().st_size,
                    "bars": len(bars),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    order_flow = [flow_by_timestamp[key] for key in sorted(flow_by_timestamp)]
    quality = SimpleNamespace(
        status=source.quality_status,
        source=f"{source.source}|binance-data-vision-{args.source_type}-sha256-v1",
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
            source.source_dataset_id if source.research_role != "general" else source.id
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
        "archive_period": args.archive_period,
        "archive_periods_used": sorted({kind for kind, _ in archive_requests}),
        "source_type": args.source_type,
        "archive_count": len(archives),
        "archives": archives,
        "order_flow_count": dataset.order_flow_count,
        "order_flow_coverage_ratio": str(dataset.order_flow_coverage_ratio),
        "verified": verification["verified"],
        "created_at": datetime.now(UTC).isoformat(),
    }
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / f"order-flow-{dataset.id}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if verification["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
