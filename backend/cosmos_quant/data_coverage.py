from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class HistoricalFeature(StrEnum):
    CANDLES = "candles"
    FUNDING = "funding"
    OPEN_INTEREST = "open_interest"
    POSITIONING = "positioning"
    BASIS = "basis"
    ORDER_FLOW = "order_flow"
    LIQUIDATIONS = "liquidations"


class EvidenceCoverageBasis(StrEnum):
    EVENT_TIMESTAMPS = "event_timestamps"
    CONTINUOUS_CAPTURE_SESSIONS = "continuous_capture_sessions"
    LICENSED_ARCHIVE = "licensed_archive"
    OFFICIAL_ARCHIVE = "official_archive"


class DataCoverageRequirement(BaseModel):
    feature: HistoricalFeature
    minimum_history_days: Decimal = Field(gt=0)
    minimum_venues: int = Field(ge=1)
    minimum_points: int = Field(ge=2)
    maximum_gap_hours: Decimal | None = Field(default=None, gt=0)
    weight: int = Field(ge=1, le=100)
    historical_source_policy: str


class DataCoverageContract(BaseModel):
    version: int = 2
    symbols: list[str]
    timeframes: list[str]
    requirements: list[DataCoverageRequirement]
    no_synthetic_backfill: bool = True
    missing_feature_policy: str = (
        "A strategy that requires a missing historical feature is ineligible for that "
        "dataset; current values may never be copied backward."
    )

    @model_validator(mode="after")
    def weights_total_one_hundred(self):
        if sum(item.weight for item in self.requirements) != 100:
            raise ValueError("data coverage requirement weights must total 100")
        return self


class FeatureCoverageEvidence(BaseModel):
    feature: HistoricalFeature
    exchange: str
    symbol: str
    timeframe: str
    data_start: datetime
    data_end: datetime
    point_count: int = Field(ge=0)
    maximum_gap_hours: Decimal | None = Field(default=None, ge=0)
    integrity_verified: bool
    dataset_id: str | None = None
    source: str = "unknown"
    source_market_type: str | None = None
    source_instrument_id: str | None = None
    evidence_role: str = "exact_market_feature"
    coverage_basis: EvidenceCoverageBasis = EvidenceCoverageBasis.EVENT_TIMESTAMPS
    capture_coverage_ratio: Decimal | None = Field(default=None, ge=0, le=1)
    capture_session_count: int = Field(default=0, ge=0)

    @property
    def history_days(self) -> Decimal:
        seconds = max((self.data_end - self.data_start).total_seconds(), 0)
        return Decimal(str(seconds)) / Decimal("86400")


class DataCoverageCell(BaseModel):
    feature: HistoricalFeature
    symbol: str
    timeframe: str
    required_history_days: Decimal
    required_venues: int
    qualifying_venues: list[str] = Field(default_factory=list)
    best_history_days: Decimal = Decimal("0")
    best_point_count: int = 0
    best_maximum_gap_hours: Decimal | None = None
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    evidence_dataset_ids: list[str] = Field(default_factory=list)


class DataCoverageReport(BaseModel):
    contract_version: int
    score: Decimal = Field(ge=0, le=100)
    passed: bool
    cells: list[DataCoverageCell]
    feature_scores: dict[str, Decimal]
    blockers: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


CORE_HISTORICAL_DATA_CONTRACT = DataCoverageContract(
    symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    timeframes=["1h", "4h"],
    requirements=[
        DataCoverageRequirement(
            feature=HistoricalFeature.CANDLES,
            minimum_history_days=Decimal("1095"),
            minimum_venues=2,
            minimum_points=6570,
            maximum_gap_hours=Decimal("8"),
            weight=30,
            historical_source_policy=(
                "Exchange-native OHLCV from two independent venues; closed bars only."
            ),
        ),
        DataCoverageRequirement(
            feature=HistoricalFeature.FUNDING,
            minimum_history_days=Decimal("730"),
            minimum_venues=1,
            minimum_points=1500,
            maximum_gap_hours=Decimal("48"),
            weight=15,
            historical_source_policy=(
                "Exchange-published perpetual funding history; never backfill from current funding."
            ),
        ),
        DataCoverageRequirement(
            feature=HistoricalFeature.OPEN_INTEREST,
            minimum_history_days=Decimal("365"),
            minimum_venues=1,
            minimum_points=365,
            maximum_gap_hours=Decimal("48"),
            weight=15,
            historical_source_policy=(
                "Native or licensed point-in-time OI history with timestamped provenance."
            ),
        ),
        DataCoverageRequirement(
            feature=HistoricalFeature.POSITIONING,
            minimum_history_days=Decimal("365"),
            minimum_venues=1,
            minimum_points=365,
            maximum_gap_hours=Decimal("48"),
            weight=10,
            historical_source_policy=(
                "Exchange-published long/short positioning series; absent history "
                "disables the factor."
            ),
        ),
        DataCoverageRequirement(
            feature=HistoricalFeature.BASIS,
            minimum_history_days=Decimal("365"),
            minimum_venues=1,
            minimum_points=365,
            maximum_gap_hours=Decimal("48"),
            weight=10,
            historical_source_policy=(
                "Point-in-time mark/index basis derived only from contemporaneous exchange records."
            ),
        ),
        DataCoverageRequirement(
            feature=HistoricalFeature.ORDER_FLOW,
            minimum_history_days=Decimal("180"),
            minimum_venues=1,
            minimum_points=1080,
            maximum_gap_hours=Decimal("8"),
            weight=15,
            historical_source_policy=(
                "Checksum-verified exchange futures klines with published taker-buy volume, "
                "or aggregate trades converted to closed-bar taker flow."
            ),
        ),
        DataCoverageRequirement(
            feature=HistoricalFeature.LIQUIDATIONS,
            minimum_history_days=Decimal("90"),
            minimum_venues=1,
            minimum_points=100,
            maximum_gap_hours=Decimal("0.034"),
            weight=5,
            historical_source_policy=(
                "Truthful live collection or licensed history; no fabricated "
                "historical liquidation events. Live evidence must bind durable "
                "connection sessions with at least 99.9% coverage and no gap over 120 seconds."
            ),
        ),
    ],
)


def evaluate_data_coverage(
    evidence: list[FeatureCoverageEvidence],
    contract: DataCoverageContract = CORE_HISTORICAL_DATA_CONTRACT,
) -> DataCoverageReport:
    cells: list[DataCoverageCell] = []
    feature_scores: dict[str, Decimal] = {}
    blockers: list[str] = []
    for requirement in contract.requirements:
        targets = (
            [(symbol, "event") for symbol in contract.symbols]
            if requirement.feature == HistoricalFeature.LIQUIDATIONS
            else [
                (symbol, timeframe)
                for symbol in contract.symbols
                for timeframe in contract.timeframes
            ]
        )
        passed_targets = 0
        for symbol, timeframe in targets:
            candidates = [
                item
                for item in evidence
                if item.feature == requirement.feature
                and item.symbol == symbol
                and item.timeframe == timeframe
            ]
            qualifying = [
                item
                for item in candidates
                if item.integrity_verified
                and item.history_days >= requirement.minimum_history_days
                and item.point_count >= requirement.minimum_points
                and (
                    requirement.feature != HistoricalFeature.LIQUIDATIONS
                    or item.coverage_basis
                    in {
                        EvidenceCoverageBasis.LICENSED_ARCHIVE,
                        EvidenceCoverageBasis.OFFICIAL_ARCHIVE,
                    }
                    or (
                        item.coverage_basis
                        == EvidenceCoverageBasis.CONTINUOUS_CAPTURE_SESSIONS
                        and item.capture_session_count > 0
                        and item.capture_coverage_ratio is not None
                        and item.capture_coverage_ratio >= Decimal("0.999")
                    )
                )
                and (
                    requirement.maximum_gap_hours is None
                    or (
                        item.maximum_gap_hours is not None
                        and item.maximum_gap_hours <= requirement.maximum_gap_hours
                    )
                )
            ]
            qualifying_venues = sorted({item.exchange for item in qualifying})
            passed = len(qualifying_venues) >= requirement.minimum_venues
            if passed:
                passed_targets += 1
            best = max(candidates, key=lambda item: item.history_days, default=None)
            reasons: list[str] = []
            if not candidates:
                reasons.append("no immutable historical evidence")
            elif not passed:
                reasons.append(
                    f"qualifying venues {len(qualifying_venues)}/{requirement.minimum_venues}"
                )
                if best and best.history_days < requirement.minimum_history_days:
                    reasons.append(
                        f"best history {best.history_days.quantize(Decimal('0.1'))}/"
                        f"{requirement.minimum_history_days} days"
                    )
                if best and best.point_count < requirement.minimum_points:
                    reasons.append(f"best points {best.point_count}/{requirement.minimum_points}")
                if best and not best.integrity_verified:
                    reasons.append("best evidence failed immutable integrity verification")
                if (
                    best
                    and requirement.feature == HistoricalFeature.LIQUIDATIONS
                    and best.coverage_basis == EvidenceCoverageBasis.EVENT_TIMESTAMPS
                ):
                    reasons.append(
                        "event timestamps do not prove continuous stream capture"
                    )
                if (
                    best
                    and requirement.feature == HistoricalFeature.LIQUIDATIONS
                    and best.coverage_basis
                    == EvidenceCoverageBasis.CONTINUOUS_CAPTURE_SESSIONS
                    and (
                        best.capture_coverage_ratio is None
                        or best.capture_coverage_ratio < Decimal("0.999")
                    )
                ):
                    reasons.append(
                        f"capture coverage {best.capture_coverage_ratio or Decimal('0')}/0.999"
                    )
                if (
                    best
                    and requirement.maximum_gap_hours is not None
                    and (
                        best.maximum_gap_hours is None
                        or best.maximum_gap_hours > requirement.maximum_gap_hours
                    )
                ):
                    reasons.append(
                        f"maximum gap {best.maximum_gap_hours}/"
                        f"{requirement.maximum_gap_hours} hours"
                    )
            cell = DataCoverageCell(
                feature=requirement.feature,
                symbol=symbol,
                timeframe=timeframe,
                required_history_days=requirement.minimum_history_days,
                required_venues=requirement.minimum_venues,
                qualifying_venues=qualifying_venues,
                best_history_days=(
                    best.history_days.quantize(Decimal("0.001")) if best else Decimal("0")
                ),
                best_point_count=best.point_count if best else 0,
                best_maximum_gap_hours=best.maximum_gap_hours if best else None,
                passed=passed,
                reasons=reasons,
                evidence_dataset_ids=sorted(
                    {item.dataset_id for item in candidates if item.dataset_id}
                ),
            )
            cells.append(cell)
            if not passed:
                blockers.append(
                    f"{requirement.feature}:{symbol}:{timeframe}: " + "; ".join(reasons)
                )
        ratio = Decimal(passed_targets) / Decimal(max(len(targets), 1))
        feature_scores[str(requirement.feature)] = (Decimal(requirement.weight) * ratio).quantize(
            Decimal("0.01")
        )
    score = sum(feature_scores.values(), Decimal("0")).quantize(Decimal("0.01"))
    return DataCoverageReport(
        contract_version=contract.version,
        score=score,
        passed=all(cell.passed for cell in cells),
        cells=cells,
        feature_scores=feature_scores,
        blockers=blockers,
    )
