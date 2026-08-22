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

from cosmos_quant import api  # noqa: E402
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
from cosmos_quant.timeframes import (  # noqa: E402
    TIMEFRAME_SECONDS,
    resample_closed_candles,
)


def _load(dataset_id: str, method_name: str, model):
    return [model.model_validate(item) for item in getattr(api.store, method_name)(dataset_id)]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Causally resample one immutable research dataset and every feature series."
    )
    parser.add_argument("source_dataset_id")
    parser.add_argument("--target-timeframe", default="4h")
    args = parser.parse_args()
    payload = api.store.load_research_dataset(args.source_dataset_id)
    if not payload:
        raise SystemExit(f"research dataset not found: {args.source_dataset_id}")
    source = ResearchDataset.model_validate(payload)
    if args.target_timeframe not in TIMEFRAME_SECONDS:
        raise SystemExit(f"unsupported target timeframe: {args.target_timeframe}")
    if TIMEFRAME_SECONDS[source.timeframe] >= TIMEFRAME_SECONDS[args.target_timeframe]:
        raise SystemExit("target timeframe must be greater than source timeframe")

    source_candles = _load(source.id, "load_research_dataset_candles", Candle)
    candles = resample_closed_candles(
        source_candles,
        source.timeframe,
        args.target_timeframe,
    )
    if not candles:
        raise SystemExit("source dataset produced no complete target candles")
    start, end = candles[0].timestamp, candles[-1].timestamp

    def bounded_downsample(method_name: str, model):
        points = [
            item
            for item in _load(source.id, method_name, model)
            if start <= item.timestamp <= end
        ]
        return downsample_latest_observation(points, timeframe=args.target_timeframe)

    funding = bounded_downsample("load_research_dataset_funding", FundingRatePoint)
    open_interest = bounded_downsample(
        "load_research_dataset_open_interest",
        OpenInterestPoint,
    )
    positioning = bounded_downsample(
        "load_research_dataset_positioning",
        PositioningPoint,
    )
    basis = bounded_downsample("load_research_dataset_basis", BasisPoint)
    source_flow = _load(source.id, "load_research_dataset_order_flow", OrderFlowBar)
    order_flow = resample_order_flow(
        source_flow,
        timeframe=args.target_timeframe,
        start=start,
        end=end,
    )
    # Liquidation context is an independently closed hourly evidence stream.
    # Keep every causal hourly observation rather than collapsing it to the
    # candle timeframe; the backtester forms its own trailing 24-hour prefix.
    liquidation_flow = [
        item
        for item in _load(
            source.id,
            "load_research_dataset_liquidation_flow",
            LiquidationFlowPoint,
        )
        if start <= item.timestamp <= end
    ]
    external_source_evidence = load_external_source_evidence(source)
    quality = SimpleNamespace(
        status=source.quality_status,
        source=f"{source.source}|causal-resample-{source.timeframe}-to-{args.target_timeframe}",
        gap_count=0,
        duplicate_count=0,
        invalid_count=0,
    )
    dataset = build_research_dataset(
        candles,
        exchange=source.exchange,
        market_type=source.market_type,
        symbol=source.symbol,
        timeframe=args.target_timeframe,
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
        "source_timeframe": source.timeframe,
        "target_timeframe": dataset.timeframe,
        "candle_count": dataset.candle_count,
        "funding_count": dataset.funding_count,
        "open_interest_count": dataset.open_interest_count,
        "positioning_count": dataset.positioning_count,
        "basis_count": dataset.basis_count,
        "order_flow_count": dataset.order_flow_count,
        "liquidation_flow_count": dataset.liquidation_flow_count,
        "data_start": dataset.data_start.isoformat(),
        "data_end": dataset.data_end.isoformat(),
        "verified": verification["verified"],
        "created_at": datetime.now(UTC).isoformat(),
    }
    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / f"resampled-research-{dataset.id}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({**manifest, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if verification["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
