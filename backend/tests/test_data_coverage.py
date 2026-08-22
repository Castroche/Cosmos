from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.data_coverage import (
    CORE_HISTORICAL_DATA_CONTRACT,
    DataCoverageContract,
    DataCoverageRequirement,
    EvidenceCoverageBasis,
    FeatureCoverageEvidence,
    HistoricalFeature,
    evaluate_data_coverage,
)


def _evidence(
    *,
    feature: HistoricalFeature,
    exchange: str,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    days: int = 400,
    points: int = 1000,
    gap: str | None = "1",
    verified: bool = True,
) -> FeatureCoverageEvidence:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return FeatureCoverageEvidence(
        feature=feature,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        data_start=start,
        data_end=start + timedelta(days=days),
        point_count=points,
        maximum_gap_hours=Decimal(gap) if gap is not None else None,
        integrity_verified=verified,
        dataset_id=f"{exchange}-{feature}",
        source="test",
    )


def test_contract_rejects_weights_that_do_not_total_one_hundred():
    try:
        DataCoverageContract(
            symbols=["BTC/USDT"],
            timeframes=["1h"],
            requirements=[
                DataCoverageRequirement(
                    feature=HistoricalFeature.CANDLES,
                    minimum_history_days=Decimal("1"),
                    minimum_venues=1,
                    minimum_points=2,
                    weight=99,
                    historical_source_policy="test",
                )
            ],
        )
    except ValueError as exc:
        assert "weights must total 100" in str(exc)
    else:
        raise AssertionError("invalid contract weights were accepted")


def test_coverage_requires_independent_venues_history_integrity_and_continuity():
    contract = DataCoverageContract(
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        requirements=[
            DataCoverageRequirement(
                feature=HistoricalFeature.CANDLES,
                minimum_history_days=Decimal("365"),
                minimum_venues=2,
                minimum_points=100,
                maximum_gap_hours=Decimal("2"),
                weight=100,
                historical_source_policy="test",
            )
        ],
    )
    evidence = [
        _evidence(feature=HistoricalFeature.CANDLES, exchange="binance"),
        _evidence(
            feature=HistoricalFeature.CANDLES,
            exchange="okx",
            verified=False,
        ),
    ]

    failed = evaluate_data_coverage(evidence, contract)
    assert failed.score == 0
    assert failed.passed is False
    evidence[1] = evidence[1].model_copy(update={"integrity_verified": True})
    passed = evaluate_data_coverage(evidence, contract)
    assert passed.score == 100
    assert passed.passed is True


def test_liquidation_contract_requires_stream_uptime_not_fake_event_bar_gaps():
    contract = DataCoverageContract(
        symbols=["BTC/USDT"],
        timeframes=["1h", "4h"],
        requirements=[
            DataCoverageRequirement(
                feature=HistoricalFeature.LIQUIDATIONS,
                minimum_history_days=Decimal("90"),
                minimum_venues=1,
                minimum_points=100,
                maximum_gap_hours=Decimal("0.034"),
                weight=100,
                historical_source_policy="live-only",
            )
        ],
    )
    evidence = [
        _evidence(
            feature=HistoricalFeature.LIQUIDATIONS,
            exchange="binance",
            timeframe="event",
            days=91,
            points=100,
            gap="0.02",
        )
    ]

    event_only = evaluate_data_coverage(evidence, contract)
    assert event_only.passed is False
    assert "do not prove continuous stream capture" in event_only.blockers[0]

    evidence[0] = evidence[0].model_copy(
        update={
            "coverage_basis": EvidenceCoverageBasis.CONTINUOUS_CAPTURE_SESSIONS,
            "capture_coverage_ratio": Decimal("0.9995"),
            "capture_session_count": 4,
        }
    )
    report = evaluate_data_coverage(evidence, contract)
    assert report.passed is True
    assert len(report.cells) == 1


def test_core_contract_is_strict_and_prohibits_synthetic_backfill():
    assert CORE_HISTORICAL_DATA_CONTRACT.no_synthetic_backfill is True
    assert sum(item.weight for item in CORE_HISTORICAL_DATA_CONTRACT.requirements) == 100
    assert {
        requirement.feature for requirement in CORE_HISTORICAL_DATA_CONTRACT.requirements
    } == set(HistoricalFeature)
