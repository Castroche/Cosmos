import hashlib
import io
import json
import re
import urllib.error
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.liquidation_archive import (
    CoinMLiquidationArchiveManifest,
    _aggregate_flow,
    _fetch_bytes,
    _parse_archive,
    _parse_listing,
    coinm_bars_to_research_points,
    import_binance_coinm_liquidation_history,
    summarize_coinm_liquidation_flow,
    verify_binance_coinm_liquidation_manifest,
)


def _listing(instrument: str, days: list[date], *, truncated: str = "false") -> bytes:
    prefix = f"data/futures/cm/daily/liquidationSnapshot/{instrument}/"
    contents = []
    for day in days:
        key = f"{prefix}{instrument}-liquidationSnapshot-{day.isoformat()}.zip"
        contents.extend(
            [
                f"<Contents><Key>{key}</Key></Contents>",
                f"<Contents><Key>{key}.CHECKSUM</Key></Contents>",
            ]
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>data.binance.vision</Name><Prefix>{prefix}</Prefix>"
        f"<IsTruncated>{truncated}</IsTruncated>{''.join(contents)}</ListBucketResult>"
    ).encode()


def _archive(instrument: str, day: date) -> bytes:
    timestamp = int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
    header = (
        "time,side,order_type,time_in_force,original_quantity,price,average_price,"
        "order_status,last_fill_quantity,accumulated_fill_quantity\n"
    )
    first = f"{timestamp + 1},SELL,LIMIT,IOC,5,100,99,FILLED,2,5\n"
    second = f"{timestamp + 3_600_001},BUY,LIMIT,IOC,7,101,100,FILLED,3,7\n"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(
            f"{instrument}-liquidationSnapshot-{day.isoformat()}.csv",
            header + first + first + second,
        )
    return output.getvalue()


def test_listing_requires_official_complete_zip_checksum_pairs():
    day = date(2024, 1, 1)
    assert _parse_listing(_listing("BTCUSD_PERP", [day]), instrument_id="BTCUSD_PERP") == {
        day
    }
    with pytest.raises(ValueError, match="paginated or incomplete"):
        _parse_listing(
            _listing("BTCUSD_PERP", [day], truncated="true"),
            instrument_id="BTCUSD_PERP",
        )
    unmatched = _listing("BTCUSD_PERP", [day]).replace(b".zip.CHECKSUM", b".bad")
    with pytest.raises(ValueError, match="CHECKSUM files do not match"):
        _parse_listing(unmatched, instrument_id="BTCUSD_PERP")


def test_listing_fetch_retries_transient_transport_failure(monkeypatch):
    payload = _listing("BTCUSD_PERP", [date(2024, 1, 1)])

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    class Opener:
        attempts = 0

        def open(self, _url, timeout):
            assert timeout == 5
            self.attempts += 1
            if self.attempts == 1:
                raise urllib.error.URLError("reset")
            return Response(payload)

    opener = Opener()
    monkeypatch.setattr(
        "cosmos_quant.liquidation_archive.urllib.request.build_opener",
        lambda *_args: opener,
    )
    monkeypatch.setattr("cosmos_quant.liquidation_archive.time.sleep", lambda _seconds: None)
    observed = _fetch_bytes(
        "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=x",
        proxy_url="",
        timeout_seconds=5,
    )
    assert observed == payload
    assert opener.attempts == 2


def test_archive_schema_deduplication_and_causal_hour_close(tmp_path):
    day = date(2024, 1, 1)
    path = tmp_path / "BTCUSD_PERP-liquidationSnapshot-2024-01-01.zip"
    path.write_bytes(_archive("BTCUSD_PERP", day))
    raw_count, events = _parse_archive(
        path,
        research_symbol="BTC/USDT",
        instrument_id="BTCUSD_PERP",
        expected_day=day,
    )
    bars = _aggregate_flow(events)

    assert raw_count == 3
    assert len(events) == 2
    assert len(bars) == 2
    assert bars[0].long_liquidated_contracts == Decimal("5")
    assert bars[0].short_liquidated_contracts == Decimal("0")
    assert bars[0].available_at == bars[0].interval_start + timedelta(hours=1)
    assert bars[1].short_liquidated_contracts == Decimal("7")
    assert bars[0].source_market_type == "coin_m_inverse_perpetual"

    points = coinm_bars_to_research_points(
        bars,
        research_symbol="BTC/USDT",
        start=bars[0].available_at,
        end=bars[-1].available_at,
    )
    assert len(points) == 2
    assert points[0].timestamp == bars[0].available_at
    assert points[0].short_minus_long_imbalance == Decimal("-1")

    at_first_close = summarize_coinm_liquidation_flow(
        bars,
        research_symbol="BTC/USDT",
        as_of=bars[0].available_at,
        lookback_hours=24,
    )
    assert at_first_close.observed_bar_count == 1
    assert at_first_close.long_liquidated_contracts == Decimal("5")
    assert at_first_close.short_minus_long_imbalance == Decimal("-1")
    future_changed = bars[1].model_copy(update={"short_liquidated_contracts": Decimal("999")})
    unchanged = summarize_coinm_liquidation_flow(
        [bars[0], future_changed],
        research_symbol="BTC/USDT",
        as_of=bars[0].available_at,
        lookback_hours=24,
    )
    assert unchanged == at_first_close


def test_real_import_contract_and_tamper_detection(tmp_path, monkeypatch):
    start = date(2024, 1, 1)
    days = [start + timedelta(days=index) for index in range(90)]
    listing = _listing("BTCUSD_PERP", days)
    monkeypatch.setattr(
        "cosmos_quant.liquidation_archive._fetch_bytes",
        lambda *_args, **_kwargs: listing,
    )

    def fake_download(url, destination, **_kwargs):
        match = re.search(r"(\d{4}-\d{2}-\d{2})\.zip$", url)
        assert match is not None
        day = date.fromisoformat(match.group(1))
        content = _archive("BTCUSD_PERP", day)
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / url.rsplit("/", 1)[-1]
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(
        "cosmos_quant.liquidation_archive.download_verified_archive",
        fake_download,
    )
    manifest = import_binance_coinm_liquidation_history(
        "BTC/USDT",
        days[0],
        days[-1],
        workspace_root=tmp_path,
        workers=4,
    )
    result = verify_binance_coinm_liquidation_manifest(manifest, workspace_root=tmp_path)

    assert result["verified"] is True
    assert manifest.archive_day_count == 90
    assert manifest.unique_event_count == 180
    assert manifest.duplicate_row_count == 90
    assert manifest.flow_bar_count == 180
    assert "COIN-M" in manifest.limitations[0]

    flow = tmp_path / manifest.flow_path
    flow.write_text(json.dumps([]), encoding="utf-8")
    tampered = verify_binance_coinm_liquidation_manifest(manifest, workspace_root=tmp_path)
    assert tampered["verified"] is False
    assert tampered["flow_verified"] is False


def test_manifest_rejects_calendar_gaps():
    with pytest.raises(ValueError, match="calendar"):
        CoinMLiquidationArchiveManifest.model_validate(
            {
                "id": "pending",
                "source_listing_url": "https://example.invalid",
                "source_listing_path": "listing.xml",
                "source_listing_sha256": "0" * 64,
                "research_symbol": "BTC/USDT",
                "source_instrument_id": "BTCUSD_PERP",
                "coverage_start": "2024-01-01T00:00:00Z",
                "coverage_end": "2024-04-01T00:00:00Z",
                "expected_calendar_days": 91,
                "archive_day_count": 90,
                "raw_row_count": 100,
                "unique_event_count": 100,
                "duplicate_row_count": 0,
                "flow_bar_count": 1,
                "flow_path": "flow.json",
                "flow_sha256": "0" * 64,
                "days": [],
                "limitations": ["test"],
            }
        )
