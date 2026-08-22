from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class CaptureSessionStatus(StrEnum):
    CONNECTED = "connected"
    CLOSED = "closed"
    DISCONNECTED = "disconnected"
    INTERRUPTED = "interrupted"


class MarketDataCaptureSession(BaseModel):
    """Durable evidence for one confirmed live-stream connection interval."""

    id: str
    source: str
    stream: str
    symbols: list[str]
    sampling: str
    status: CaptureSessionStatus = CaptureSessionStatus.CONNECTED
    started_at: datetime
    last_heartbeat_at: datetime
    ended_at: datetime | None = None
    error: str = ""

    @model_validator(mode="after")
    def timestamps_are_causal(self):
        if self.last_heartbeat_at < self.started_at:
            raise ValueError("capture heartbeat cannot precede the confirmed connection")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("capture end cannot precede the confirmed connection")
        if self.status == CaptureSessionStatus.CONNECTED and self.ended_at is not None:
            raise ValueError("connected capture session cannot already have an end time")
        if self.status != CaptureSessionStatus.CONNECTED and self.ended_at is None:
            raise ValueError("terminal capture session requires an end time")
        return self


class MarketDataCaptureCoverage(BaseModel):
    source: str
    stream: str
    symbol: str
    required_history_days: Decimal
    required_coverage_ratio: Decimal
    maximum_allowed_gap_seconds: Decimal
    window_start: datetime
    window_end: datetime
    earliest_capture_at: datetime | None = None
    latest_capture_at: datetime | None = None
    session_count: int = Field(ge=0)
    covered_seconds: Decimal = Field(ge=0)
    covered_history_days: Decimal = Field(ge=0)
    coverage_ratio: Decimal = Field(ge=0, le=1)
    maximum_gap_seconds: Decimal = Field(ge=0)
    material_gap_count: int = Field(ge=0)
    passed: bool


def calculate_capture_coverage(
    sessions: list[MarketDataCaptureSession],
    *,
    source: str,
    stream: str,
    symbol: str,
    required_history_days: Decimal = Decimal("90"),
    required_coverage_ratio: Decimal = Decimal("0.999"),
    maximum_allowed_gap_seconds: Decimal = Decimal("120"),
    now: datetime | None = None,
) -> MarketDataCaptureCoverage:
    """Measure connection uptime, never the spacing between sparse market events.

    A quiet liquidation stream is still covered while its websocket connection is
    confirmed alive. Conversely, two events far apart never prove that the stream
    was observed between them.
    """

    window_end = now or datetime.now(UTC)
    window_start = window_end - timedelta(days=float(required_history_days))
    duration_seconds = Decimal(str((window_end - window_start).total_seconds()))
    scoped = [
        session
        for session in sessions
        if session.source == source
        and session.stream == stream
        and symbol in session.symbols
    ]
    intervals: list[tuple[datetime, datetime]] = []
    for session in scoped:
        # An open session is only proven through its most recent durable heartbeat.
        proven_end = min(session.ended_at or session.last_heartbeat_at, window_end)
        proven_start = max(session.started_at, window_start)
        if proven_end > proven_start:
            intervals.append((proven_start, proven_end))
    intervals.sort(key=lambda item: item[0])

    merged: list[list[datetime]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end

    covered_seconds = sum(
        (Decimal(str((end - start).total_seconds())) for start, end in merged),
        Decimal("0"),
    )
    gap_seconds: list[Decimal] = []
    cursor = window_start
    for start, end in merged:
        if start > cursor:
            gap_seconds.append(Decimal(str((start - cursor).total_seconds())))
        cursor = max(cursor, end)
    if cursor < window_end:
        gap_seconds.append(Decimal(str((window_end - cursor).total_seconds())))

    maximum_gap = max(gap_seconds, default=Decimal("0"))
    ratio = (
        (covered_seconds / duration_seconds).quantize(Decimal("0.000000001"))
        if duration_seconds > 0
        else Decimal("0")
    )
    material_gap_count = sum(
        1 for seconds in gap_seconds if seconds > maximum_allowed_gap_seconds
    )
    earliest = min((item[0] for item in intervals), default=None)
    latest = max((item[1] for item in intervals), default=None)
    return MarketDataCaptureCoverage(
        source=source,
        stream=stream,
        symbol=symbol,
        required_history_days=required_history_days,
        required_coverage_ratio=required_coverage_ratio,
        maximum_allowed_gap_seconds=maximum_allowed_gap_seconds,
        window_start=window_start,
        window_end=window_end,
        earliest_capture_at=earliest,
        latest_capture_at=latest,
        session_count=len(scoped),
        covered_seconds=covered_seconds.quantize(Decimal("0.001")),
        covered_history_days=(covered_seconds / Decimal("86400")).quantize(
            Decimal("0.000001")
        ),
        coverage_ratio=ratio,
        maximum_gap_seconds=maximum_gap.quantize(Decimal("0.001")),
        material_gap_count=material_gap_count,
        passed=(
            ratio >= required_coverage_ratio
            and maximum_gap <= maximum_allowed_gap_seconds
        ),
    )
