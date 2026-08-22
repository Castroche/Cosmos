from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .domain import Candle

TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}

CRYPTO_TRADING_SECONDS_PER_YEAR = 365 * 24 * 60 * 60

CONFIRMATION_TIMEFRAMES = {
    "1m": "15m",
    "3m": "15m",
    "5m": "30m",
    "15m": "1h",
    "30m": "2h",
    "1h": "4h",
    "2h": "6h",
    "4h": "1d",
    "6h": "1d",
    "12h": "1d",
    "1d": "1w",
    "1w": "1w",
}


def timeframe_seconds(timeframe: str) -> int:
    try:
        return TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc


def timeframe_delta(timeframe: str, bars: int = 1) -> timedelta:
    if bars <= 0:
        raise ValueError("bars must be positive")
    return timedelta(seconds=timeframe_seconds(timeframe) * bars)


def periods_per_year(timeframe: str) -> float:
    return CRYPTO_TRADING_SECONDS_PER_YEAR / timeframe_seconds(timeframe)


def candle_close_time(candle: Candle, timeframe: str) -> datetime:
    timestamp = candle.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp + timeframe_delta(timeframe)


def closed_candles(
    candles: list[Candle],
    timeframe: str,
    as_of: datetime,
) -> list[Candle]:
    """Return only bars whose complete interval is known at ``as_of``.

    Exchange OHLCV timestamps identify the start of a bar. A bar is safe for
    signal generation only after start + timeframe, regardless of whether the
    REST endpoint happens to include the currently forming bar.
    """

    boundary = as_of
    if boundary.tzinfo is None:
        boundary = boundary.replace(tzinfo=UTC)
    return [candle for candle in candles if candle_close_time(candle, timeframe) <= boundary]


def signal_expiry(signal_candle: Candle, timeframe: str, bars: int = 8) -> datetime:
    return candle_close_time(signal_candle, timeframe) + timeframe_delta(timeframe, bars)


def confirmation_timeframe(timeframe: str) -> str:
    try:
        return CONFIRMATION_TIMEFRAMES[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc


def resample_closed_candles(
    candles: list[Candle], source_timeframe: str, target_timeframe: str
) -> list[Candle]:
    """Aggregate closed source bars without exposing an incomplete target bar."""

    source_seconds = timeframe_seconds(source_timeframe)
    target_seconds = timeframe_seconds(target_timeframe)
    if target_seconds == source_seconds:
        return list(candles)
    if target_seconds < source_seconds or target_seconds % source_seconds:
        raise ValueError("target timeframe must be a whole multiple of source timeframe")
    required = target_seconds // source_seconds
    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        timestamp = candle.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        bucket = int(timestamp.timestamp()) // target_seconds * target_seconds
        buckets.setdefault(bucket, []).append(candle)

    result: list[Candle] = []
    for bucket, rows in sorted(buckets.items()):
        ordered = sorted(rows, key=lambda item: item.timestamp)
        if len(ordered) != required:
            continue
        expected = [bucket + index * source_seconds for index in range(required)]
        actual = [
            int(
                (
                    item.timestamp if item.timestamp.tzinfo else item.timestamp.replace(tzinfo=UTC)
                ).timestamp()
            )
            for item in ordered
        ]
        if actual != expected:
            continue
        result.append(
            Candle(
                timestamp=datetime.fromtimestamp(bucket, tz=UTC),
                open=ordered[0].open,
                high=max(item.high for item in ordered),
                low=min(item.low for item in ordered),
                close=ordered[-1].close,
                volume=sum((item.volume for item in ordered), Decimal("0")),
            )
        )
    return result
