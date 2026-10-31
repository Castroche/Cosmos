from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant.application_services import ApplicationServices  # noqa: E402
api = ApplicationServices()
from cosmos_quant.archive_data import (  # noqa: E402
    downsample_latest_observation,
    resample_order_flow,
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
from cosmos_quant.timeframes import TIMEFRAME_SECONDS  # noqa: E402


def _load(dataset_id: str, method_name: str, model):
    return [model.model_validate(item) for item in getattr(api.store, method_name)(dataset_id)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_flow_dataset_id")
    parser.add_argument("target_dataset_id")
    args = parser.parse_args()
    source_payload = api.store.load_research_dataset(args.source_flow_dataset_id)
    target_payload = api.store.load_research_dataset(args.target_dataset_id)
    if not source_payload or not target_payload:
        raise SystemExit("source or target research dataset not found")
    source = ResearchDataset.model_validate(source_payload)
    target = ResearchDataset.model_validate(target_payload)
    if (source.exchange, source.market_type, source.symbol) != (
        target.exchange,
        target.market_type,
        target.symbol,
    ):
        raise SystemExit("source and target must describe the same exchange market")
    if TIMEFRAME_SECONDS[source.timeframe] >= TIMEFRAME_SECONDS[target.timeframe]:
        raise SystemExit("source order-flow timeframe must be lower than target timeframe")

    candles = _load(target.id, "load_research_dataset_candles", Candle)
    funding = _load(target.id, "load_research_dataset_funding", FundingRatePoint)
    open_interest = _load(
        target.id,
        "load_research_dataset_open_interest",
        OpenInterestPoint,
    )
    positioning = _load(
        target.id,
        "load_research_dataset_positioning",
        PositioningPoint,
    )
    basis = _load(target.id, "load_research_dataset_basis", BasisPoint)
    existing_flow = _load(
        target.id,
        "load_research_dataset_order_flow",
        OrderFlowBar,
    )
    liquidation_flow = _load(
        target.id,
        "load_research_dataset_liquidation_flow",
        LiquidationFlowPoint,
    )
    external_source_evidence = load_external_source_evidence(target)
    source_flow = _load(
        source.id,
        "load_research_dataset_order_flow",
        OrderFlowBar,
    )
    source_open_interest = _load(
        source.id,
        "load_research_dataset_open_interest",
        OpenInterestPoint,
    )
    source_positioning = _load(
        source.id,
        "load_research_dataset_positioning",
        PositioningPoint,
    )
    open_interest_by_time = {item.timestamp: item for item in open_interest}
    open_interest_by_time.update(
        (item.timestamp, item)
        for item in source_open_interest
        if target.data_start <= item.timestamp <= target.data_end
    )
    open_interest = [open_interest_by_time[key] for key in sorted(open_interest_by_time)]
    open_interest = downsample_latest_observation(
        open_interest,
        timeframe=target.timeframe,
    )
    positioning_by_time = {item.timestamp: item for item in positioning}
    positioning_by_time.update(
        (item.timestamp, item)
        for item in source_positioning
        if target.data_start <= item.timestamp <= target.data_end
    )
    positioning = [positioning_by_time[key] for key in sorted(positioning_by_time)]
    positioning = downsample_latest_observation(
        positioning,
        timeframe=target.timeframe,
    )
    resampled = resample_order_flow(
        source_flow,
        timeframe=target.timeframe,
        start=target.data_start,
        end=target.data_end,
    )
    flow_by_timestamp = {item.timestamp: item for item in existing_flow}
    flow_by_timestamp.update((item.timestamp, item) for item in resampled)
    order_flow = [flow_by_timestamp[key] for key in sorted(flow_by_timestamp)]
    quality = SimpleNamespace(
        status=target.quality_status,
        source=f"{target.source}|resampled-verified-aggregate-trades",
        gap_count=target.gap_count,
        duplicate_count=target.duplicate_count,
        invalid_count=target.invalid_count,
    )
    dataset = build_research_dataset(
        candles,
        exchange=target.exchange,
        market_type=target.market_type,
        symbol=target.symbol,
        timeframe=target.timeframe,
        quality=quality,
        funding_rates=funding,
        open_interest=open_interest,
        positioning=positioning,
        basis=basis,
        order_flow=order_flow,
        liquidation_flow=liquidation_flow,
        research_role=target.research_role,
        source_dataset_id=(
            target.source_dataset_id
            if target.research_role != "general"
            else target.id
        ),
        blind_set_id=target.blind_set_id,
        split_boundary=target.split_boundary,
        embargo_bars=target.embargo_bars,
        blind_sealed=target.blind_sealed,
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
        "source_flow_dataset_id": source.id,
        "target_dataset_id": target.id,
        "dataset_id": dataset.id,
        "source_order_flow_count": source.order_flow_count,
        "source_open_interest_count": source.open_interest_count,
        "source_positioning_count": source.positioning_count,
        "resampled_order_flow_count": len(resampled),
        "merged_order_flow_count": dataset.order_flow_count,
        "merged_open_interest_count": dataset.open_interest_count,
        "merged_positioning_count": dataset.positioning_count,
        "order_flow_coverage_ratio": str(dataset.order_flow_coverage_ratio),
        "verified": verification["verified"],
        "created_at": datetime.now(UTC).isoformat(),
    }
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / f"resampled-order-flow-{dataset.id}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if verification["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
