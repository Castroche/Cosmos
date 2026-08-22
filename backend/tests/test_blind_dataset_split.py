from datetime import UTC, datetime, timedelta

import pytest
from cosmos_quant.domain import Candle

from scripts.create_blind_dataset_split import (
    _align_common_candle_windows,
    _bound_candle_windows,
    _parse_utc_boundary,
)


def _candles(start: datetime, count: int) -> list[Candle]:
    return [
        Candle(
            timestamp=start + timedelta(hours=4 * index),
            open="100",
            high="101",
            low="99",
            close="100",
            volume="10",
        )
        for index in range(count)
    ]


def test_common_window_alignment_uses_identical_real_timestamps():
    started = datetime(2025, 1, 1, tzinfo=UTC)
    first = _candles(started, 8)
    second = _candles(started + timedelta(hours=8), 8)

    aligned, common_start, common_end = _align_common_candle_windows(
        {"first": first, "second": second}
    )

    assert common_start == started + timedelta(hours=8)
    assert common_end == started + timedelta(hours=28)
    assert [item.timestamp for item in aligned["first"]] == [
        item.timestamp for item in aligned["second"]
    ]


def test_common_window_alignment_rejects_timestamp_gaps():
    started = datetime(2025, 1, 1, tzinfo=UTC)
    complete = _candles(started, 8)
    missing_bar = [*complete[:3], *complete[4:]]

    with pytest.raises(ValueError, match="timestamps are not identical"):
        _align_common_candle_windows({"complete": complete, "gap": missing_bar})


def test_bounded_common_window_is_inclusive_start_and_exclusive_end():
    started = datetime(2025, 1, 1, tzinfo=UTC)
    candles = _candles(started, 8)
    window_start = started + timedelta(hours=8)
    window_end = started + timedelta(hours=24)

    aligned, common_start, common_end = _align_common_candle_windows(
        {"first": candles, "second": candles},
        window_start=window_start,
        window_end_exclusive=window_end,
    )

    assert common_start == window_start
    assert common_end == started + timedelta(hours=20)
    assert len(aligned["first"]) == len(aligned["second"]) == 4
    assert all(candle.timestamp < window_end for candle in aligned["first"])


def test_bounded_window_rejects_empty_or_naive_contracts():
    started = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="empty datasets"):
        _bound_candle_windows(
            {"sample": _candles(started, 2)},
            window_start=started + timedelta(days=2),
        )
    with pytest.raises(ValueError, match="UTC offset"):
        _parse_utc_boundary("2025-01-01T00:00:00", option="--window-start")
