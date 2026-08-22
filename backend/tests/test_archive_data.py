import hashlib
import io
import json
import threading
import time
import urllib.error
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.archive_data import (
    OkxFundingArchiveDescriptor,
    aggregate_order_flow,
    binance_daily_aggtrade_url,
    binance_daily_kline_url,
    binance_daily_metrics_url,
    binance_monthly_aggtrade_url,
    binance_monthly_kline_url,
    download_hashed_okx_funding_archive,
    download_verified_archive,
    downsample_latest_observation,
    fetch_okx_funding_archive_descriptors,
    iter_binance_aggregate_trades,
    iter_binance_futures_metrics,
    iter_binance_kline_order_flow,
    iter_okx_funding_rates,
    metrics_to_research_points,
    resample_order_flow,
)
from cosmos_quant.domain import OrderFlowBar


def _archive_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            "BTCUSDT-aggTrades-2026-07-28.csv",
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
            "transact_time,is_buyer_maker\n"
            "1,100,2,10,11,1785196800000,false\n"
            "2,101,3,12,14,1785198600000,true\n"
            "3,102,1,15,15,1785200400000,false\n",
        )
    return output.getvalue()


def test_official_archive_is_checksum_verified_and_aggregated(tmp_path, monkeypatch):
    content = _archive_bytes()
    checksum = hashlib.sha256(content).hexdigest()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        def open(self, url, timeout):
            assert timeout == 120
            return Response(
                f"{checksum}  test.zip\n".encode() if url.endswith(".CHECKSUM") else content
            )

    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: Opener())
    archive, observed = download_verified_archive(
        "https://data.binance.vision/test.zip",
        tmp_path,
    )
    trades = list(iter_binance_aggregate_trades(archive))
    start = datetime(2026, 7, 28, tzinfo=UTC)
    bars = aggregate_order_flow(
        trades,
        timeframe="1h",
        start=start,
        end=start + timedelta(hours=1),
    )

    assert observed == checksum
    assert len(trades) == 3
    assert len(bars) == 2
    assert bars[0].underlying_trade_count == 5
    assert bars[0].taker_buy_base_volume == Decimal("2")
    assert bars[0].taker_sell_base_volume == Decimal("3")
    assert bars[0].delta_base_volume == Decimal("-1")


def test_archive_checksum_mismatch_fails_closed(tmp_path, monkeypatch):
    content = _archive_bytes()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        def open(self, url, timeout):
            del timeout
            return Response(
                (("0" * 64) + "  test.zip").encode() if url.endswith(".CHECKSUM") else content
            )

    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: Opener())
    with pytest.raises(ValueError, match="checksum mismatch"):
        download_verified_archive("https://data.binance.vision/test.zip", tmp_path)


def test_archive_checksum_mismatch_retries_empty_transport_body(tmp_path, monkeypatch):
    content = _archive_bytes()
    checksum = hashlib.sha256(content).hexdigest()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        content_attempts = 0

        def open(self, url, timeout):
            del timeout
            if url.endswith(".CHECKSUM"):
                return Response(f"{checksum}  test.zip\n".encode())
            self.content_attempts += 1
            return Response(b"" if self.content_attempts == 1 else content)

    opener = Opener()
    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: opener)
    monkeypatch.setattr("cosmos_quant.archive_data.time.sleep", lambda _seconds: None)

    archive, observed = download_verified_archive(
        "https://data.binance.vision/test.zip",
        tmp_path,
    )

    assert archive.read_bytes() == content
    assert observed == checksum
    assert opener.content_attempts == 2


def test_archive_download_retries_transient_transport_failure(tmp_path, monkeypatch):
    content = _archive_bytes()
    checksum = hashlib.sha256(content).hexdigest()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        attempts = 0

        def open(self, url, timeout):
            del timeout
            if url.endswith(".CHECKSUM"):
                self.attempts += 1
                if self.attempts == 1:
                    raise urllib.error.URLError("temporary EOF")
                return Response(f"{checksum}  test.zip\n".encode())
            return Response(content)

    opener = Opener()
    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: opener)
    monkeypatch.setattr("cosmos_quant.archive_data.time.sleep", lambda _seconds: None)

    archive, observed = download_verified_archive(
        "https://data.binance.vision/test.zip",
        tmp_path,
    )

    assert archive.exists()
    assert observed == checksum
    assert opener.attempts == 2


def test_concurrent_archive_downloads_share_one_locked_cache_target(tmp_path, monkeypatch):
    content = _archive_bytes()
    checksum = hashlib.sha256(content).hexdigest()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        def __init__(self):
            self.content_requests = 0
            self.guard = threading.Lock()

        def open(self, url, timeout):
            del timeout
            if url.endswith(".CHECKSUM"):
                return Response(f"{checksum}  test.zip\n".encode())
            with self.guard:
                self.content_requests += 1
            time.sleep(0.05)
            return Response(content)

    opener = Opener()
    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: opener)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: download_verified_archive(
                    "https://data.binance.vision/test.zip",
                    tmp_path,
                ),
                range(2),
            )
        )

    assert results[0] == results[1]
    assert opener.content_requests == 1
    assert not list(tmp_path.glob("*.partial"))
    assert not list(tmp_path.glob("*.lock"))


def test_binance_archive_url_is_canonical():
    assert binance_daily_aggtrade_url("BTC/USDT:USDT", date(2026, 7, 28)).endswith(
        "/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-07-28.zip"
    )
    assert binance_monthly_aggtrade_url("BTC/USDT:USDT", date(2026, 7, 1)).endswith(
        "/monthly/aggTrades/BTCUSDT/BTCUSDT-aggTrades-2026-07.zip"
    )
    assert binance_daily_metrics_url("BTC/USDT:USDT", date(2026, 7, 28)).endswith(
        "/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-07-28.zip"
    )
    assert binance_monthly_kline_url("BTC/USDT:USDT", "1h", date(2026, 7, 1)).endswith(
        "/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2026-07.zip"
    )
    assert binance_daily_kline_url("BTC/USDT:USDT", "1h", date(2026, 7, 28)).endswith(
        "/daily/klines/BTCUSDT/1h/BTCUSDT-1h-2026-07-28.zip"
    )


def _okx_funding_archive_bytes(*, instrument_id: str = "BTC-USDT-SWAP") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            f"{instrument_id}-fundingrates-2025-01.csv",
            "instrument_name,funding_rate,funding_time\n"
            f"{instrument_id},0.0001,1735689600000\n"
            f"{instrument_id},-0.0002,1735718400000\n",
        )
    return output.getvalue()


def test_okx_history_endpoint_resolves_only_exact_official_funding_files(monkeypatch):
    document = {
        "code": "0",
        "data": {
            "details": [
                {
                    "instType": "SWAP",
                    "instFamily": "BTC-USDT",
                    "groupDetails": [
                        {
                            "filename": "BTC-USDT-SWAP-fundingrates-2024-12.zip",
                            "url": (
                                "https://static.okx.com/cdn/okex/traderecords/"
                                "swaprates/monthly/202412/"
                                "BTC-USDT-SWAP-fundingrates-2024-12.zip?v=999"
                            ),
                        },
                        {
                            "filename": "BTC-USDT-SWAP-fundingrates-2025-01.zip",
                            "url": (
                                "https://static.okx.com/cdn/okex/traderecords/"
                                "swaprates/monthly/202501/"
                                "BTC-USDT-SWAP-fundingrates-2025-01.zip?v=999"
                            ),
                        }
                    ],
                }
            ]
        },
    }
    raw = json.dumps(document).encode()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def geturl(self):
            return (
                "https://www.okx.com/priapi/v5/broker/public/trade-data/download-link"
            )

    class Opener:
        def open(self, request, timeout):
            assert request.method == "POST"
            assert timeout == 60
            payload = json.loads(request.data)
            assert payload["module"] == "3"
            assert payload["instQueryParam"] == {"instFamilyList": ["BTC-USDT"]}
            return Response(raw)

    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: Opener())
    descriptors = fetch_okx_funding_archive_descriptors(
        "BTC/USDT:USDT",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 31, tzinfo=UTC),
    )

    assert len(descriptors) == 1
    assert descriptors[0].instrument_id == "BTC-USDT-SWAP"
    assert descriptors[0].month == "2025-01"
    assert descriptors[0].response_sha256 == hashlib.sha256(raw).hexdigest()


def test_okx_history_endpoint_rejects_untrusted_archive_url(monkeypatch):
    raw = json.dumps(
        {
            "code": "0",
            "data": {
                "details": [
                    {
                        "instType": "SWAP",
                        "instFamily": "BTC-USDT",
                        "groupDetails": [
                            {
                                "filename": "BTC-USDT-SWAP-fundingrates-2025-01.zip",
                                "url": (
                                    "https://attacker.example/202501/"
                                    "BTC-USDT-SWAP-fundingrates-2025-01.zip"
                                ),
                            }
                        ],
                    }
                ]
            },
        }
    ).encode()

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def geturl(self):
            return (
                "https://www.okx.com/priapi/v5/broker/public/trade-data/download-link"
            )

    class Opener:
        def open(self, _request, timeout):
            assert timeout == 60
            return Response(raw)

    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: Opener())
    with pytest.raises(ValueError, match="official static.okx.com"):
        fetch_okx_funding_archive_descriptors(
            "BTC/USDT",
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 31, tzinfo=UTC),
        )


def test_okx_funding_archive_is_hashed_schema_checked_and_causally_filtered(
    tmp_path,
    monkeypatch,
):
    content = _okx_funding_archive_bytes()
    descriptor = OkxFundingArchiveDescriptor(
        instrument_family="BTC-USDT",
        instrument_id="BTC-USDT-SWAP",
        month="2025-01",
        filename="BTC-USDT-SWAP-fundingrates-2025-01.zip",
        url=(
            "https://static.okx.com/cdn/okex/traderecords/swaprates/monthly/"
            "202501/BTC-USDT-SWAP-fundingrates-2025-01.zip?v=999"
        ),
        response_sha256="a" * 64,
    )

    class Headers(dict):
        pass

    class Response(io.BytesIO):
        headers = Headers({"Content-Length": str(len(content))})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def geturl(self):
            return descriptor.url

    class Opener:
        calls = 0

        def open(self, request, timeout):
            assert request.full_url == descriptor.url
            assert timeout == 120
            self.calls += 1
            return Response(content)

    opener = Opener()
    monkeypatch.setattr("cosmos_quant.archive_data._opener", lambda _proxy: opener)
    archive, digest, size = download_hashed_okx_funding_archive(descriptor, tmp_path)
    cached_archive, cached_digest, cached_size = download_hashed_okx_funding_archive(
        descriptor,
        tmp_path,
    )
    points = list(
        iter_okx_funding_rates(
            archive,
            instrument_id=descriptor.instrument_id,
            start=datetime(2025, 1, 1, 4, tzinfo=UTC),
            end=datetime(2025, 1, 2, tzinfo=UTC),
        )
    )

    assert digest == hashlib.sha256(content).hexdigest()
    assert size == len(content)
    assert (cached_archive, cached_digest, cached_size) == (archive, digest, size)
    assert opener.calls == 1
    assert len(points) == 1
    assert points[0].timestamp == datetime(2025, 1, 1, 8, tzinfo=UTC)
    assert points[0].rate == Decimal("-0.0002")


def test_okx_funding_archive_rejects_cross_market_rows(tmp_path):
    archive = tmp_path / "BTC-USDT-SWAP-fundingrates-2025-01.zip"
    archive.write_bytes(_okx_funding_archive_bytes(instrument_id="ETH-USDT-SWAP"))

    with pytest.raises(ValueError, match="different instrument"):
        list(
            iter_okx_funding_rates(
                archive,
                instrument_id="BTC-USDT-SWAP",
                start=datetime(2025, 1, 1, tzinfo=UTC),
                end=datetime(2025, 2, 1, tzinfo=UTC),
            )
        )


def test_official_metrics_archive_converts_to_causal_research_points(tmp_path):
    archive = tmp_path / "metrics.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "BTCUSDT-metrics-2026-07-28.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-07-28 00:05:00,BTCUSDT,100,200,1.1,1.2,1.5,0.9\n",
        )

    metrics = list(iter_binance_futures_metrics(archive))
    start = datetime(2026, 7, 28, tzinfo=UTC)
    open_interest, positioning = metrics_to_research_points(
        metrics,
        start=start,
        end=start + timedelta(days=1),
    )

    assert len(open_interest) == 1
    assert open_interest[0].open_interest_amount == Decimal("100")
    assert open_interest[0].open_interest_value == Decimal("200")
    assert positioning[0].long_short_ratio == Decimal("1.5")
    assert positioning[0].long_account_fraction == Decimal("0.6")
    assert positioning[0].short_account_fraction == Decimal("0.4")
    assert positioning[0].source_variant == "binance_metrics_global_account"


def test_metrics_missing_ratio_keeps_open_interest_without_fabricating_positioning(tmp_path):
    archive = tmp_path / "metrics-missing-ratio.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "BTCUSDT-metrics-2026-07-28.csv",
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2026-07-28 00:05:00,BTCUSDT,100,200,,,,\n",
        )

    start = datetime(2026, 7, 28, tzinfo=UTC)
    open_interest, positioning = metrics_to_research_points(
        iter_binance_futures_metrics(archive),
        start=start,
        end=start + timedelta(days=1),
    )

    assert len(open_interest) == 1
    assert positioning == []


def test_official_kline_taker_volume_builds_closed_bar_order_flow(tmp_path):
    archive = tmp_path / "klines.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "BTCUSDT-1h-2026-07.csv",
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n"
            "1782864000000,100,101,99,100,10,1782867599999,1000,25,6,600,0\n",
        )
    start = datetime(2026, 7, 1, tzinfo=UTC)

    bars = list(
        iter_binance_kline_order_flow(
            archive,
            start=start,
            end=start + timedelta(hours=1),
        )
    )

    assert len(bars) == 1
    assert bars[0].underlying_trade_count == 25
    assert bars[0].taker_buy_base_volume == Decimal("6")
    assert bars[0].taker_sell_base_volume == Decimal("4")
    assert bars[0].delta_base_volume == Decimal("2")


def test_metrics_downsampling_keeps_latest_causal_timestamp_per_bar():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    points = [
        type("Point", (), {"timestamp": start + timedelta(minutes=minute)})()
        for minute in (5, 55, 65)
    ]

    sampled = downsample_latest_observation(points, timeframe="1h")

    assert [item.timestamp for item in sampled] == [
        start + timedelta(minutes=55),
        start + timedelta(minutes=65),
    ]


def test_verified_hourly_order_flow_resamples_to_four_hours_without_losing_accounting():
    start = datetime(2026, 7, 28, tzinfo=UTC)
    bars = [
        OrderFlowBar(
            timestamp=start + timedelta(hours=index),
            aggregate_count=10,
            underlying_trade_count=20,
            base_volume=Decimal("5"),
            quote_volume=Decimal("500"),
            taker_buy_base_volume=Decimal("3"),
            taker_sell_base_volume=Decimal("2"),
            delta_base_volume=Decimal("1"),
            maximum_trade_quote=Decimal(100 + index),
        )
        for index in range(4)
    ]

    result = resample_order_flow(
        bars,
        timeframe="4h",
        start=start,
        end=start + timedelta(hours=4),
    )

    assert len(result) == 1
    assert result[0].aggregate_count == 40
    assert result[0].underlying_trade_count == 80
    assert result[0].base_volume == Decimal("20")
    assert result[0].delta_base_volume == Decimal("4")
    assert result[0].maximum_trade_quote == Decimal("103")
