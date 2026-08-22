from __future__ import annotations

import hashlib

import pytest
from cosmos_quant.official_archive_index import (
    BINANCE_VISION_INDEX_URL,
    BYBIT_PUBLIC_TRADING_INDEX_URL,
    OfficialArchiveIndex,
    _validated_official_url,
    download_official_archive_index,
)
from cosmos_quant.storage import SqliteStore


def _binance_xml(*, truncated: str = "false", bucket: str = "data.binance.vision") -> bytes:
    prefixes = (
        "BTCUSDT",
        "ETHUSDT",
        "1000PEPEUSDT",
        "BTCUSD_PERP",
        "ETHUSDT_240628",
    )
    children = "".join(
        "<CommonPrefixes><Prefix>data/futures/um/monthly/klines/"
        f"{symbol}/</Prefix></CommonPrefixes>"
        for symbol in prefixes
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>{bucket}</Name>"
        "<Prefix>data/futures/um/monthly/klines/</Prefix>"
        f"<IsTruncated>{truncated}</IsTruncated>{children}"
        "</ListBucketResult>"
    ).encode()


def _bybit_html() -> bytes:
    return b"""
    <html><head><title>Directory listing for /trading/</title></head><body>
    <a href="BTCUSDT/">BTCUSDT/</a>
    <a href="/trading/ETHUSDT/">ETHUSDT/</a>
    <a href="SOLUSDT/">SOLUSDT/</a>
    <a href="BTCUSD/">BTCUSD/</a>
    <a href="BTCUSDT-31DEC21/">BTCUSDT-31DEC21/</a>
    </body></html>
    """


def test_binance_server_capture_hashes_and_normalizes_official_payload():
    raw = _binance_xml()
    archive = download_official_archive_index("binance", fetcher=lambda _: raw)

    assert archive.source_url == BINANCE_VISION_INDEX_URL
    assert archive.source_payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert archive.source_listing_complete is True
    assert archive.raw_market_count == 5
    assert [item.market_id for item in archive.instruments] == [
        "1000PEPEUSDT",
        "BTCUSDT",
        "ETHUSDT",
    ]
    assert archive.historical_point_in_time_complete is False
    assert "not that it was tradable" in archive.limitations[0]


def test_identical_official_payload_capture_is_idempotent():
    raw = _binance_xml()
    first = download_official_archive_index("binance", fetcher=lambda _: raw)
    second = download_official_archive_index("binance", fetcher=lambda _: raw)

    assert first.fingerprint == second.fingerprint
    assert first.id == second.id


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (_binance_xml(truncated="true"), "paginated or incomplete"),
        (_binance_xml(bucket="attacker-bucket"), "bucket or prefix"),
    ],
)
def test_binance_capture_rejects_incomplete_or_wrong_scope(raw, message):
    with pytest.raises(ValueError, match=message):
        download_official_archive_index("binance", fetcher=lambda _: raw)


def test_bybit_server_capture_accepts_archive_presence_without_pit_claim():
    raw = _bybit_html()
    archive = download_official_archive_index("bybit", fetcher=lambda _: raw)

    assert archive.source_url == BYBIT_PUBLIC_TRADING_INDEX_URL
    assert archive.source_payload_sha256 == hashlib.sha256(raw).hexdigest()
    assert [item.market_id for item in archive.instruments] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    ]
    assert archive.historical_point_in_time_complete is False


def test_archive_source_allowlist_rejects_ssrf_and_lookalike_paths():
    with pytest.raises(ValueError, match="allowlisted"):
        _validated_official_url("bybit", "https://example.com/trading/")
    with pytest.raises(ValueError, match="official trading archive root"):
        _validated_official_url("bybit", "https://public.bybit.com/trading/../private/")


def test_official_archive_index_persists_idempotently(tmp_path):
    archive = download_official_archive_index("binance", fetcher=lambda _: _binance_xml())
    duplicate = download_official_archive_index("binance", fetcher=lambda _: _binance_xml())
    store = SqliteStore(tmp_path / "archives.db")

    store.save_official_archive_index(archive)
    store.save_official_archive_index(duplicate)

    restored = OfficialArchiveIndex.model_validate(
        store.load_official_archive_index(archive.id)
    )
    assert restored.id == archive.id
    assert restored.fingerprint == archive.fingerprint
    assert len(store.list_official_archive_indexes(exchange="binance")) == 1
    assert store.list_official_archive_indexes(exchange="bybit") == []
