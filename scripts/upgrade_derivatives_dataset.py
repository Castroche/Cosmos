from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.archive_data import fetch_okx_funding_archive_history  # noqa: E402
from cosmos_quant.domain import (  # noqa: E402
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter  # noqa: E402
from cosmos_quant.research import (  # noqa: E402
    ResearchDataset,
    build_research_dataset,
    load_external_source_evidence,
    verify_research_dataset,
)


async def upgrade(
    dataset_id: str,
    *,
    adapter: CcxtExchangeAdapter | None = None,
    fetch_positioning: bool = True,
    fetch_okx_funding_archives: bool = True,
    archive_destination: Path | None = None,
) -> dict:
    payload = api.store.load_research_dataset(dataset_id)
    if not payload:
        raise LookupError(f"research dataset not found: {dataset_id}")
    source = ResearchDataset.model_validate(payload)
    if source.market_type != "swap":
        raise ValueError("derivatives upgrade requires a swap dataset")
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
    existing_positioning = [
        PositioningPoint.model_validate(item)
        for item in api.store.load_research_dataset_positioning(source.id)
    ]
    existing_basis = [
        BasisPoint.model_validate(item) for item in api.store.load_research_dataset_basis(source.id)
    ]
    order_flow = [
        OrderFlowBar.model_validate(item)
        for item in api.store.load_research_dataset_order_flow(source.id)
    ]
    liquidation_flow = [
        LiquidationFlowPoint.model_validate(item)
        for item in api.store.load_research_dataset_liquidation_flow(source.id)
    ]
    existing_external_evidence = load_external_source_evidence(source)
    configuration = api.configuration_service.current_configuration()
    owns_adapter = adapter is None
    if adapter is None:
        adapter = CcxtExchangeAdapter(
            source.exchange,
            {},
            sandbox=False,
            live_enabled=False,
            public_only=True,
            market_type="swap",
            proxy_url=configuration.network_proxy_url,
        )
        adapter.public_fetch_attempts = 3
    warnings: list[dict[str, str]] = []
    archive_evidence: list[dict[str, str | int]] = []
    funding_archive_conflict_count = 0
    candle_span_seconds = max((source.data_end - source.data_start).total_seconds(), 1)
    funding_span_seconds = (
        (funding[-1].timestamp - funding[0].timestamp).total_seconds()
        if len(funding) >= 2
        else 0
    )
    original_funding_coverage = Decimal(str(funding_span_seconds / candle_span_seconds))
    funding_upgrade_requested = bool(
        fetch_okx_funding_archives
        and source.exchange == "okx"
        and source.market_type == "swap"
        and candle_span_seconds >= 28 * 86_400
        and original_funding_coverage < Decimal("0.95")
    )
    positioning = existing_positioning
    basis = existing_basis
    try:
        if funding_upgrade_requested:
            try:
                archive_root = archive_destination or (
                    ROOT
                    / "data"
                    / "archives"
                    / "okx"
                    / "funding"
                    / source.symbol.replace("/", "-").split(":", 1)[0]
                )
                fetched_funding, evidence = await asyncio.to_thread(
                    fetch_okx_funding_archive_history,
                    source.symbol,
                    source.data_start,
                    source.data_end,
                    destination=archive_root,
                    proxy_url=configuration.network_proxy_url,
                )
                funding_by_time = {point.timestamp: point for point in funding}
                for point in fetched_funding:
                    existing = funding_by_time.get(point.timestamp)
                    if existing is not None and existing != point:
                        funding_archive_conflict_count += 1
                    # The completed monthly archive is the authoritative
                    # settled series. Keep REST-only points solely outside
                    # the published archive horizon.
                    funding_by_time[point.timestamp] = point
                funding = [funding_by_time[timestamp] for timestamp in sorted(funding_by_time)]
                archive_evidence = [item.manifest() for item in evidence]
            except Exception as exc:
                warnings.append(
                    {
                        "feature": "funding_archive",
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
                )
        if not existing_positioning and fetch_positioning:
            try:
                positioning = await adapter.fetch_positioning_history(
                    source.symbol,
                    source.timeframe,
                    source.data_start,
                    source.data_end,
                )
            except Exception as exc:
                warnings.append(
                    {
                        "feature": "positioning",
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
                )
        if source.basis_coverage_ratio < Decimal("0.99"):
            try:
                fetched_basis = await adapter.fetch_basis_history(
                    source.symbol,
                    source.timeframe,
                    source.data_start,
                    source.data_end,
                )
                basis_by_time = {point.timestamp: point for point in existing_basis}
                basis_by_time.update({point.timestamp: point for point in fetched_basis})
                basis = [basis_by_time[timestamp] for timestamp in sorted(basis_by_time)]
            except Exception as exc:
                warnings.append(
                    {
                        "feature": "basis",
                        "error_type": type(exc).__name__,
                        "message": str(exc)[:500],
                    }
                )
    finally:
        if owns_adapter:
            await adapter.close()
    quality = SimpleNamespace(
        status=source.quality_status,
        source=source.source,
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
        external_source_evidence=[
            *existing_external_evidence,
            *archive_evidence,
        ]
        or None,
    )
    if archive_evidence:
        dataset = dataset.model_copy(
            update={
                "acquisition_manifest": {
                    **dataset.acquisition_manifest,
                    "external_source_evidence_kind": "okx_official_monthly_funding_v1",
                    "funding_archive_precedence": "official_settled_archive_over_rest",
                    "funding_archive_conflict_count": funding_archive_conflict_count,
                }
            }
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
    basis_upgrade_requested = source.basis_coverage_ratio < Decimal("0.99")
    basis_upgrade_complete = (
        not basis_upgrade_requested or dataset.basis_coverage_ratio >= Decimal("0.99")
    )
    upgraded_funding_span_seconds = (
        (funding[-1].timestamp - funding[0].timestamp).total_seconds()
        if len(funding) >= 2
        else 0
    )
    funding_coverage_ratio = min(
        Decimal(str(upgraded_funding_span_seconds / candle_span_seconds)),
        Decimal("1"),
    )
    funding_upgrade_complete = (
        not funding_upgrade_requested or funding_coverage_ratio >= Decimal("0.95")
    )
    return {
        "source_dataset_id": source.id,
        "dataset_id": dataset.id,
        "exchange": dataset.exchange,
        "symbol": dataset.symbol,
        "timeframe": dataset.timeframe,
        "funding_count": len(funding),
        "funding_coverage_ratio": str(funding_coverage_ratio.quantize(Decimal("0.0001"))),
        "funding_upgrade_requested": funding_upgrade_requested,
        "funding_upgrade_complete": funding_upgrade_complete,
        "funding_archive_count": len(archive_evidence),
        "funding_archive_conflict_count": funding_archive_conflict_count,
        "funding_archive_evidence": archive_evidence,
        "positioning_count": dataset.positioning_count,
        "positioning_coverage_ratio": str(dataset.positioning_coverage_ratio),
        "basis_count": dataset.basis_count,
        "basis_coverage_ratio": str(dataset.basis_coverage_ratio),
        "basis_upgrade_requested": basis_upgrade_requested,
        "basis_upgrade_complete": basis_upgrade_complete,
        "order_flow_count": dataset.order_flow_count,
        "warnings": warnings,
        "verified": verification["verified"],
        "upgrade_complete": bool(
            verification["verified"]
            and basis_upgrade_complete
            and funding_upgrade_complete
        ),
        "created_at": datetime.now(UTC).isoformat(),
    }


async def upgrade_many(
    dataset_ids: list[str],
    *,
    fetch_positioning: bool = True,
    fetch_okx_funding_archives: bool = True,
) -> list[dict]:
    configuration = api.configuration_service.current_configuration()
    adapters: dict[str, CcxtExchangeAdapter] = {}
    reports = []
    try:
        for dataset_id in dataset_ids:
            payload = api.store.load_research_dataset(dataset_id)
            if not payload:
                reports.append(
                    {
                        "source_dataset_id": dataset_id,
                        "dataset_id": None,
                        "exchange": "unknown",
                        "symbol": "unknown",
                        "timeframe": "unknown",
                        "warnings": [],
                        "verified": False,
                        "upgrade_complete": False,
                        "error": {
                            "error_type": "LookupError",
                            "message": f"research dataset not found: {dataset_id}",
                        },
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
                continue
            exchange = str(payload["exchange"])
            if exchange not in adapters:
                adapter = CcxtExchangeAdapter(
                    exchange,
                    {},
                    sandbox=False,
                    live_enabled=False,
                    public_only=True,
                    market_type="swap",
                    proxy_url=configuration.network_proxy_url,
                )
                adapter.public_fetch_attempts = 3
                adapters[exchange] = adapter
            try:
                reports.append(
                    await upgrade(
                        dataset_id,
                        adapter=adapters[exchange],
                        fetch_positioning=fetch_positioning,
                        fetch_okx_funding_archives=fetch_okx_funding_archives,
                    )
                )
            except Exception as exc:
                reports.append(
                    {
                        "source_dataset_id": dataset_id,
                        "dataset_id": None,
                        "exchange": exchange,
                        "symbol": str(payload.get("symbol", "unknown")),
                        "timeframe": str(payload.get("timeframe", "unknown")),
                        "warnings": [],
                        "verified": False,
                        "upgrade_complete": False,
                        "error": {
                            "error_type": type(exc).__name__,
                            "message": str(exc)[:500],
                        },
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
    finally:
        await asyncio.gather(
            *(adapter.close() for adapter in adapters.values()),
            return_exceptions=True,
        )
    return reports


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_ids", nargs="+")
    parser.add_argument(
        "--skip-positioning",
        action="store_true",
        help="preserve existing positioning data and upgrade other features only",
    )
    parser.add_argument(
        "--skip-okx-funding-archives",
        action="store_true",
        help="do not supplement OKX funding from the official monthly history portal",
    )
    args = parser.parse_args()
    reports = asyncio.run(
        upgrade_many(
            args.dataset_ids,
            fetch_positioning=not args.skip_positioning,
            fetch_okx_funding_archives=not args.skip_okx_funding_archives,
        )
    )
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    path = output / "derivatives-upgrade-latest.json"
    path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    summaries = [
        {
            key: report.get(key)
            for key in (
                "source_dataset_id",
                "dataset_id",
                "exchange",
                "symbol",
                "timeframe",
                "funding_count",
                "funding_coverage_ratio",
                "funding_archive_count",
                "funding_archive_conflict_count",
                "verified",
                "upgrade_complete",
                "error",
            )
            if key in report
        }
        for report in reports
    ]
    print(json.dumps({"reports": summaries, "manifest": str(path)}, ensure_ascii=False))
    return 0 if all(item["upgrade_complete"] for item in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
