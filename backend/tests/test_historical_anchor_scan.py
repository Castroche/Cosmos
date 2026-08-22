from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from cosmos_quant.historical_anchor_scan import (
    HistoricalAnchorScan,
    build_binance_historical_anchor_scan,
)
from cosmos_quant.official_archive_index import download_official_archive_index
from cosmos_quant.storage import SqliteStore


def _index_xml() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Name>data.binance.vision</Name>
      <Prefix>data/futures/um/monthly/klines/</Prefix>
      <IsTruncated>false</IsTruncated>
      <CommonPrefixes><Prefix>data/futures/um/monthly/klines/BTCUSDT/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>data/futures/um/monthly/klines/ETHUSDT/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>data/futures/um/monthly/klines/OLDUSDT/</Prefix></CommonPrefixes>
    </ListBucketResult>"""


def _kline_zip(symbol: str, timestamps: list[datetime]) -> bytes:
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer, lineterminator="\n")
    writer.writerow(
        [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "count",
            "taker_buy_volume",
            "taker_buy_quote_volume",
            "ignore",
        ]
    )
    for index, timestamp in enumerate(timestamps, start=1):
        writer.writerow(
            [
                int(timestamp.timestamp() * 1000),
                "100",
                "110",
                "90",
                "105",
                "10",
                int(timestamp.timestamp() * 1000) + 86_399_999,
                str(index * 3100),
                "42",
                "6",
                "1900",
                "0",
            ]
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{symbol}-1d-2025-01.csv", csv_buffer.getvalue())
    return output.getvalue()


def _official_index():
    return download_official_archive_index("binance", fetcher=lambda _: _index_xml())


def test_complete_anchor_scan_checks_every_candidate_without_overclaiming_listing_state():
    btc = _kline_zip(
        "BTCUSDT",
        [datetime(2025, 1, day, tzinfo=UTC) for day in (1, 2, 3)],
    )
    eth = _kline_zip(
        "ETHUSDT",
        [datetime(2025, 1, day, tzinfo=UTC) for day in (1, 2)],
    )

    def fetch(url: str):
        if "OLDUSDT" in url:
            return None
        raw = btc if "BTCUSDT" in url else eth
        return raw, hashlib.sha256(raw).hexdigest()

    scan = build_binance_historical_anchor_scan(
        _official_index(),
        anchor_month=date(2025, 1, 1),
        maximum_workers=3,
        fetcher=fetch,
    )

    assert scan.candidate_count == scan.checked_count == 3
    assert scan.observed_count == 2
    assert scan.absent_count == 1
    assert scan.complete_candidate_scan is True
    assert scan.historical_point_in_time_complete is False
    assert [item.market_id for item in scan.market_catalog()] == ["BTCUSDT", "ETHUSDT"]
    assert scan.market_catalog()[0].quote_volume_24h == Decimal("600")
    HistoricalAnchorScan.model_validate(scan.model_dump())


def test_anchor_scan_is_deterministic_for_identical_official_evidence():
    raw = _kline_zip("BTCUSDT", [datetime(2025, 1, 1, tzinfo=UTC)])

    def fetch(url: str):
        if "BTCUSDT" not in url:
            return None
        return raw, hashlib.sha256(raw).hexdigest()

    first = build_binance_historical_anchor_scan(
        _official_index(), anchor_month=date(2025, 1, 1), fetcher=fetch
    )
    second = build_binance_historical_anchor_scan(
        _official_index(), anchor_month=date(2025, 1, 1), fetcher=fetch
    )
    assert first.fingerprint == second.fingerprint
    assert first.id == second.id


def test_anchor_scan_fails_closed_on_checksum_mismatch_or_future_leak():
    raw = _kline_zip("BTCUSDT", [datetime(2025, 2, 1, tzinfo=UTC)])

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        build_binance_historical_anchor_scan(
            _official_index(),
            anchor_month=date(2025, 1, 1),
            fetcher=lambda url: None
            if "BTCUSDT" not in url
            else (raw, "a" * 64),
        )

    digest = hashlib.sha256(raw).hexdigest()
    with pytest.raises(RuntimeError, match="outside its month"):
        build_binance_historical_anchor_scan(
            _official_index(),
            anchor_month=date(2025, 1, 1),
            fetcher=lambda url: None
            if "BTCUSDT" not in url
            else (raw, digest),
        )


def test_archive_scan_cannot_be_forged_into_listing_state_completeness():
    raw = _kline_zip("BTCUSDT", [datetime(2025, 1, 1, tzinfo=UTC)])

    def fetch(url: str):
        if "BTCUSDT" not in url:
            return None
        return raw, hashlib.sha256(raw).hexdigest()

    scan = build_binance_historical_anchor_scan(
        _official_index(), anchor_month=date(2025, 1, 1), fetcher=fetch
    )
    payload = scan.model_dump(exclude={"fingerprint"})
    payload["historical_point_in_time_complete"] = True
    with pytest.raises(ValueError, match="cannot alone certify"):
        HistoricalAnchorScan.model_validate(payload)


def test_anchor_scan_persistence_requires_exact_official_parent(tmp_path):
    raw = _kline_zip("BTCUSDT", [datetime(2025, 1, 1, tzinfo=UTC)])
    index = _official_index()
    scan = build_binance_historical_anchor_scan(
        index,
        anchor_month=date(2025, 1, 1),
        fetcher=lambda url: (
            (raw, hashlib.sha256(raw).hexdigest()) if "BTCUSDT" in url else None
        ),
    )
    store = SqliteStore(tmp_path / "anchor.db")
    with pytest.raises(RuntimeError, match="unknown archive index"):
        store.save_historical_anchor_scan(scan)
    store.save_official_archive_index(index)
    store.save_historical_anchor_scan(scan)
    store.save_historical_anchor_scan(scan)

    restored = HistoricalAnchorScan.model_validate(
        store.load_historical_anchor_scan(scan.id)
    )
    assert restored.fingerprint == scan.fingerprint
    assert len(store.list_historical_anchor_scans(exchange="binance")) == 1
