from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.domain import (  # noqa: E402
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from cosmos_quant.liquidation_archive import (  # noqa: E402
    CoinMLiquidationArchiveManifest,
    coinm_bars_to_research_points,
    load_verified_coinm_liquidation_flow,
)
from cosmos_quant.research import (  # noqa: E402
    ResearchDataset,
    build_research_dataset,
    verify_research_dataset,
)


def _load(dataset_id: str, method_name: str, model):
    return [
        model.model_validate(item)
        for item in getattr(api.store, method_name)(dataset_id)
    ]


def _bounded(points, start, end):
    return [point for point in points if start <= point.timestamp <= end]


def _source_external_evidence(source: ResearchDataset) -> list[dict]:
    raw = source.acquisition_manifest.get("external_source_evidence_json", "")
    if not raw:
        return []
    try:
        payload = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"source dataset {source.id} has invalid external evidence") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"source dataset {source.id} external evidence is not a list")
    return payload


def _load_source(source_id: str):
    payload = api.store.load_research_dataset(source_id)
    if not payload:
        raise ValueError(f"research dataset not found: {source_id}")
    source = ResearchDataset.model_validate(payload)
    series = {
        "candles": _load(source.id, "load_research_dataset_candles", Candle),
        "funding": _load(source.id, "load_research_dataset_funding", FundingRatePoint),
        "open_interest": _load(
            source.id,
            "load_research_dataset_open_interest",
            OpenInterestPoint,
        ),
        "positioning": _load(
            source.id,
            "load_research_dataset_positioning",
            PositioningPoint,
        ),
        "basis": _load(source.id, "load_research_dataset_basis", BasisPoint),
        "order_flow": _load(
            source.id,
            "load_research_dataset_order_flow",
            OrderFlowBar,
        ),
        "liquidation_flow": _load(
            source.id,
            "load_research_dataset_liquidation_flow",
            LiquidationFlowPoint,
        ),
    }
    verification = verify_research_dataset(
        source,
        series["candles"],
        series["funding"],
        series["open_interest"],
        series["positioning"],
        series["basis"],
        series["order_flow"],
        series["liquidation_flow"],
    )
    if not verification["verified"]:
        raise ValueError(f"source dataset failed immutable verification: {source.id}")
    return source, series


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bind verified Binance COIN-M hourly liquidation context to immutable "
            "USDT-perpetual research datasets without claiming contract equivalence."
        )
    )
    parser.add_argument("source_dataset_ids", nargs="+")
    parser.add_argument(
        "--archive-manifest",
        default=str(
            ROOT / "output" / "data-ingestion" / "binance-coinm-liquidations-latest.json"
        ),
    )
    args = parser.parse_args()

    archive_path = Path(args.archive_manifest).resolve()
    archive_payload = json.loads(archive_path.read_text(encoding="utf-8"))
    manifests = {
        item.research_symbol: item
        for item in (
            CoinMLiquidationArchiveManifest.model_validate(payload)
            for payload in archive_payload.get("manifests", [])
        )
    }
    if set(manifests) != {"BTC/USDT", "ETH/USDT", "SOL/USDT"}:
        raise SystemExit("archive manifest must contain BTC, ETH and SOL evidence")
    bars_by_symbol = {}
    for symbol, manifest in manifests.items():
        try:
            bars_by_symbol[symbol] = load_verified_coinm_liquidation_flow(
                manifest,
                workspace_root=ROOT,
            )
        except ValueError as exc:
            raise SystemExit(
                f"liquidation archive failed verification: {manifest.id}"
            ) from exc

    results = []
    for source_id in dict.fromkeys(args.source_dataset_ids):
        try:
            source, series = _load_source(source_id)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if (
            source.research_role != "general"
            or source.timeframe != "1h"
            or source.market_type not in {"swap", "perpetual"}
            or source.symbol not in manifests
        ):
            raise SystemExit(
                f"{source.id} must be a general 1h BTC/ETH/SOL perpetual dataset"
            )
        if series["liquidation_flow"]:
            raise SystemExit(f"{source.id} already contains liquidation flow")

        manifest = manifests[source.symbol]
        start = max(source.data_start, manifest.coverage_start + timedelta(hours=1))
        end = min(source.data_end, manifest.coverage_end)
        candles = _bounded(series["candles"], start, end)
        if len(candles) < 90 * 24:
            raise SystemExit(f"{source.id} has less than 90 days of overlapping candles")
        if any(
            right.timestamp - left.timestamp != timedelta(hours=1)
            for left, right in zip(candles, candles[1:], strict=False)
        ):
            raise SystemExit(f"{source.id} has a gap inside the liquidation window")
        start, end = candles[0].timestamp, candles[-1].timestamp
        bars = bars_by_symbol[source.symbol]
        liquidation_flow = coinm_bars_to_research_points(
            bars,
            research_symbol=source.symbol,
            start=start,
            end=end,
        )
        if len(liquidation_flow) != len(candles):
            raise SystemExit(f"{source.id} liquidation context is not hourly-complete")

        funding = _bounded(series["funding"], start, end)
        open_interest = _bounded(series["open_interest"], start, end)
        positioning = _bounded(series["positioning"], start, end)
        basis = _bounded(series["basis"], start, end)
        order_flow = _bounded(series["order_flow"], start, end)
        liquidation_evidence = {
            "archive_day_count": manifest.archive_day_count,
            "archive_manifest_id": manifest.id,
            "evidence_role": manifest.evidence_role,
            "flow_sha256": manifest.flow_sha256,
            "source_instrument_id": manifest.source_instrument_id,
            "source_listing_sha256": manifest.source_listing_sha256,
            "source_market_type": manifest.source_market_type,
            "source_dataset_fingerprint": source.data_fingerprint,
            "source_dataset_id": source.id,
        }
        external_evidence = [
            *_source_external_evidence(source),
            liquidation_evidence,
        ]
        quality = SimpleNamespace(
            status=source.quality_status,
            source=f"{source.source}|verified-coinm-liquidation-context-v1",
            gap_count=0,
            duplicate_count=0,
            invalid_count=0,
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
            source_dataset_id=source.id,
            external_source_evidence=external_evidence,
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
        if not verification["verified"]:
            raise SystemExit(f"derived dataset failed verification: {dataset.id}")
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
        results.append(
            {
                "source_dataset_id": source.id,
                "dataset_id": dataset.id,
                "exchange": dataset.exchange,
                "symbol": dataset.symbol,
                "candle_count": dataset.candle_count,
                "liquidation_flow_count": dataset.liquidation_flow_count,
                "liquidation_flow_coverage_ratio": str(
                    dataset.liquidation_flow_coverage_ratio
                ),
                "archive_manifest_id": manifest.id,
                "data_start": dataset.data_start.isoformat(),
                "data_end": dataset.data_end.isoformat(),
                "verified": verification["verified"],
            }
        )

    output = ROOT / "output" / "data-ingestion"
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / "liquidation-context-research-datasets-latest.json"
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "archive_manifest": str(archive_path),
                "datasets": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"manifest": str(result_path), "datasets": results},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
