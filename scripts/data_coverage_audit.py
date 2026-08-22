from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cosmos_quant import api  # noqa: E402
from cosmos_quant.data_coverage import (  # noqa: E402
    CORE_HISTORICAL_DATA_CONTRACT,
    DataCoverageReport,
    EvidenceCoverageBasis,
    FeatureCoverageEvidence,
    HistoricalFeature,
    evaluate_data_coverage,
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
from cosmos_quant.liquidation_archive import (  # noqa: E402
    CoinMLiquidationArchiveManifest,
    verify_binance_coinm_liquidation_manifest,
)
from cosmos_quant.liquidation_stream import (  # noqa: E402
    BINANCE_LIQUIDATION_STREAM,
)
from cosmos_quant.market_data_capture import (  # noqa: E402
    MarketDataCaptureSession,
    calculate_capture_coverage,
)
from cosmos_quant.research import ResearchDataset, verify_research_dataset  # noqa: E402


def _maximum_gap_hours(points) -> Decimal | None:
    ordered = sorted(points, key=lambda item: item.timestamp)
    if len(ordered) < 2:
        return None
    maximum_seconds = max(
        (current.timestamp - previous.timestamp).total_seconds()
        for previous, current in zip(ordered, ordered[1:], strict=False)
    )
    return (Decimal(str(maximum_seconds)) / Decimal("3600")).quantize(Decimal("0.001"))


def _feature_evidence(
    dataset: ResearchDataset,
    feature: HistoricalFeature,
    points,
    *,
    verified: bool,
) -> FeatureCoverageEvidence | None:
    if len(points) < 2:
        return None
    ordered = sorted(points, key=lambda item: item.timestamp)
    return FeatureCoverageEvidence(
        feature=feature,
        exchange=dataset.exchange,
        symbol=dataset.symbol,
        timeframe=dataset.timeframe,
        data_start=ordered[0].timestamp,
        data_end=ordered[-1].timestamp,
        point_count=len(ordered),
        maximum_gap_hours=_maximum_gap_hours(ordered),
        integrity_verified=verified,
        dataset_id=dataset.id,
        source=dataset.source,
    )


def gather_evidence() -> list[FeatureCoverageEvidence]:
    contract = CORE_HISTORICAL_DATA_CONTRACT
    datasets = [
        ResearchDataset.model_validate(payload)
        for payload in api.store.list_research_datasets(1000)
        if payload.get("research_role", "general") == "general"
        and payload.get("symbol") in contract.symbols
        and payload.get("timeframe") in contract.timeframes
    ]
    evidence: list[FeatureCoverageEvidence] = []
    for dataset in datasets:
        candles = [
            Candle.model_validate(item)
            for item in api.store.load_research_dataset_candles(dataset.id)
        ]
        funding = [
            FundingRatePoint.model_validate(item)
            for item in api.store.load_research_dataset_funding(dataset.id)
        ]
        open_interest = [
            OpenInterestPoint.model_validate(item)
            for item in api.store.load_research_dataset_open_interest(dataset.id)
        ]
        positioning = [
            PositioningPoint.model_validate(item)
            for item in api.store.load_research_dataset_positioning(dataset.id)
        ]
        basis = [
            BasisPoint.model_validate(item)
            for item in api.store.load_research_dataset_basis(dataset.id)
        ]
        order_flow = [
            OrderFlowBar.model_validate(item)
            for item in api.store.load_research_dataset_order_flow(dataset.id)
        ]
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item)
            for item in api.store.load_research_dataset_liquidation_flow(dataset.id)
        ]
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
        verified = bool(verification["verified"])
        candle_evidence = _feature_evidence(
            dataset,
            HistoricalFeature.CANDLES,
            candles,
            verified=(
                verified
                and dataset.gap_count == 0
                and dataset.invalid_count == 0
                and dataset.coverage_ratio == Decimal("1")
            ),
        )
        if candle_evidence:
            evidence.append(candle_evidence)
        for feature, points in (
            (HistoricalFeature.FUNDING, funding),
            (HistoricalFeature.OPEN_INTEREST, open_interest),
            (HistoricalFeature.POSITIONING, positioning),
            (HistoricalFeature.BASIS, basis),
            (HistoricalFeature.ORDER_FLOW, order_flow),
        ):
            item = _feature_evidence(dataset, feature, points, verified=verified)
            if item:
                evidence.append(item)
    capture_sessions = [
        MarketDataCaptureSession.model_validate(payload)
        for payload in api.store.list_market_data_capture_sessions(
            source="binance",
            stream=BINANCE_LIQUIDATION_STREAM,
        )
    ]
    liquidation_summaries = api.store.summarize_liquidation_events()
    audit_now = datetime.now(UTC)
    for summary in liquidation_summaries:
        symbol = summary["symbol"]
        venue = summary["exchange"]
        if symbol not in contract.symbols:
            continue
        if venue == "binance":
            coverage = calculate_capture_coverage(
                capture_sessions,
                source=venue,
                stream=BINANCE_LIQUIDATION_STREAM,
                symbol=symbol,
                now=audit_now,
            )
        else:
            coverage = None
        if coverage and coverage.earliest_capture_at and coverage.latest_capture_at:
            evidence.append(
                FeatureCoverageEvidence(
                    feature=HistoricalFeature.LIQUIDATIONS,
                    exchange=venue,
                    symbol=symbol,
                    timeframe="event",
                    data_start=coverage.earliest_capture_at,
                    data_end=coverage.latest_capture_at,
                    point_count=int(summary["point_count"]),
                    maximum_gap_hours=(
                        coverage.maximum_gap_seconds / Decimal("3600")
                    ).quantize(Decimal("0.000001")),
                    integrity_verified=coverage.passed,
                    source=f"{venue}:{BINANCE_LIQUIDATION_STREAM}",
                    coverage_basis=EvidenceCoverageBasis.CONTINUOUS_CAPTURE_SESSIONS,
                    capture_coverage_ratio=coverage.coverage_ratio,
                    capture_session_count=coverage.session_count,
                )
            )
        else:
            # Legacy events remain queryable, but their first/last timestamps are
            # explicitly insufficient evidence of uninterrupted collection.
            evidence.append(
                FeatureCoverageEvidence(
                    feature=HistoricalFeature.LIQUIDATIONS,
                    exchange=venue,
                    symbol=symbol,
                    timeframe="event",
                    data_start=datetime.fromisoformat(summary["first_event_at"]),
                    data_end=datetime.fromisoformat(summary["last_event_at"]),
                    point_count=int(summary["point_count"]),
                    maximum_gap_hours=None,
                    integrity_verified=False,
                    source=f"{venue}:legacy-event-records-without-capture-sessions",
                    coverage_basis=EvidenceCoverageBasis.EVENT_TIMESTAMPS,
                )
            )
    archive_manifest = (
        ROOT / "output" / "data-ingestion" / "binance-coinm-liquidations-latest.json"
    )
    if archive_manifest.is_file():
        try:
            archive_payload = json.loads(archive_manifest.read_text(encoding="utf-8"))
            archive_items = [
                CoinMLiquidationArchiveManifest.model_validate(item)
                for item in archive_payload.get("manifests", [])
            ]
        except (ValueError, OSError, json.JSONDecodeError):
            archive_items = []
        for manifest in archive_items:
            if manifest.research_symbol not in contract.symbols:
                continue
            verification = verify_binance_coinm_liquidation_manifest(
                manifest,
                workspace_root=ROOT,
            )
            evidence.append(
                FeatureCoverageEvidence(
                    feature=HistoricalFeature.LIQUIDATIONS,
                    exchange="binance_coin_m",
                    symbol=manifest.research_symbol,
                    timeframe="event",
                    data_start=manifest.coverage_start,
                    data_end=manifest.coverage_end,
                    point_count=manifest.unique_event_count,
                    maximum_gap_hours=Decimal("0"),
                    integrity_verified=bool(verification["verified"]),
                    dataset_id=manifest.id,
                    source=(
                        "binance:data-vision:coin-m-liquidationSnapshot:"
                        f"{manifest.source_instrument_id}:cross-contract-context"
                    ),
                    source_market_type=manifest.source_market_type,
                    source_instrument_id=manifest.source_instrument_id,
                    evidence_role=manifest.evidence_role,
                    coverage_basis=EvidenceCoverageBasis.OFFICIAL_ARCHIVE,
                )
            )
    return evidence


def _markdown(report: DataCoverageReport) -> str:
    lines = [
        "# COSMOS 历史数据覆盖合同审计",
        "",
        f"- 生成时间：{report.generated_at.isoformat()}",
        f"- 合同版本：{report.contract_version}",
        f"- 严格得分：{report.score}/100",
        f"- 全部通过：{'是' if report.passed else '否'}",
        "- 禁止伪回填：是",
        "",
        "| 特征 | 市场 | 周期 | 最佳历史天数 | 合格来源 | 结果 | 原因 |",
        "|---|---|---|---:|---|---|---|",
    ]
    for cell in report.cells:
        lines.append(
            "| "
            f"{cell.feature} | {cell.symbol} | {cell.timeframe} | "
            f"{cell.best_history_days} | {', '.join(cell.qualifying_venues) or '-'} | "
            f"{'PASS' if cell.passed else 'HOLD'} | "
            f"{'; '.join(cell.reasons) or 'meets contract'} |"
        )
    lines.extend(["", "## 特征得分", ""])
    lines.extend(f"- {feature}: {score}" for feature, score in report.feature_scores.items())
    lines.extend(["", "## 阻塞项", ""])
    lines.extend(f"- {blocker}" for blocker in report.blockers)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "output" / "acceptance",
    )
    args = parser.parse_args()
    evidence = gather_evidence()
    report = evaluate_data_coverage(evidence)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / "data-coverage.json"
    markdown_path = args.output_dir / "data-coverage.md"
    contract_path = args.output_dir / "data-coverage-contract.json"
    evidence_path = args.output_dir / "data-coverage-evidence.json"
    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    contract_path.write_text(
        CORE_HISTORICAL_DATA_CONTRACT.model_dump_json(indent=2),
        encoding="utf-8",
    )
    evidence_path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in evidence],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "score": str(report.score),
                "passed": report.passed,
                "evidence_records": len(evidence),
                "blockers": len(report.blockers),
                "report": str(report_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
