from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime

import pytest
from cosmos_quant.archived_exchange_catalog import (
    ArchivedExchangeCatalogManifest,
    capture_archived_binance_catalog,
    capture_archived_okx_catalog,
    verify_archived_binance_catalog,
    verify_archived_catalog_evidence,
    verify_archived_okx_catalog,
)
from cosmos_quant.research_universe import (
    PointInTimeUniversePolicy,
    build_point_in_time_universe_snapshot,
    validate_universe_snapshot,
)


def _payload() -> bytes:
    symbols = []
    for market_id, base, status, onboard in (
        ("BTCUSDT", "BTC", "TRADING", 1569398400000),
        ("ETHUSDT", "ETH", "TRADING", 1598630400000),
        ("SOLUSDT", "SOL", "TRADING", 1615968000000),
        ("OLDUSDT", "OLD", "SETTLING", 1609459200000),
    ):
        symbols.append(
            {
                "symbol": market_id,
                "pair": market_id,
                "contractType": "PERPETUAL",
                "deliveryDate": 4133404800000,
                "onboardDate": onboard,
                "status": status,
                "baseAsset": base,
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
            }
        )
    symbols.append(
        {
            "symbol": "BTCUSDC",
            "contractType": "PERPETUAL",
            "deliveryDate": 4133404800000,
            "onboardDate": 1700000000000,
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDC",
            "marginAsset": "USDC",
        }
    )
    return json.dumps(
        {
            "timezone": "UTC",
            "serverTime": 1706097611438,
            "futuresType": "U_MARGINED",
            "symbols": symbols,
        },
        separators=(",", ":"),
    ).encode()


def _capture(tmp_path):
    timestamp = "20240124122637"
    payload = _payload()
    digest = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    index = json.dumps(
        {
            "urlkey": "com,binance,fapi)/fapi/v1/exchangeinfo",
            "timestamp": timestamp,
            "url": "https://fapi.binance.com/fapi/v1/exchangeInfo",
            "mime": "application/json",
            "status": "200",
            "digest": digest,
        },
        separators=(",", ":"),
    ).encode()

    def fetcher(url: str) -> bytes:
        return index if "/wayback/cdx?" in url else payload

    return capture_archived_binance_catalog(
        timestamp=timestamp,
        workspace_root=tmp_path,
        output_root=tmp_path / "evidence",
        fetcher=fetcher,
    )


def _capture_binance_wayback(tmp_path):
    timestamp = "20240124122637"
    payload = _payload()
    digest = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    index = json.dumps(
        [
            ["timestamp", "original", "digest", "statuscode", "mimetype"],
            [
                timestamp,
                "https://fapi.binance.com/fapi/v1/exchangeInfo",
                digest,
                "200",
                "application/json",
            ],
        ],
        separators=(",", ":"),
    ).encode()

    def fetcher(url: str) -> bytes:
        return index if "/cdx/search/cdx?" in url else payload

    return capture_archived_binance_catalog(
        timestamp=timestamp,
        workspace_root=tmp_path,
        output_root=tmp_path / "wayback-evidence",
        fetcher=fetcher,
        archive_provider="internet_archive",
    )


def test_archived_official_catalog_is_digest_bound_and_replayable(tmp_path):
    manifest = _capture(tmp_path)

    assert manifest.raw_symbol_count == 5
    assert manifest.normalized_record_count == 4
    assert manifest.status_counts == {"SETTLING": 1, "TRADING": 3}
    assert manifest.original_server_time == datetime(
        2024,
        1,
        24,
        12,
        0,
        11,
        438000,
        tzinfo=UTC,
    )
    assert verify_archived_binance_catalog(manifest, workspace_root=tmp_path)["verified"]
    assert verify_archived_catalog_evidence(
        manifest.archive_evidence(),
        manifest.entries,
        workspace_root=tmp_path,
    )["verified"]

    raw_path = tmp_path / manifest.raw_payload_path
    raw_path.write_bytes(raw_path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="payload hash mismatch"):
        verify_archived_binance_catalog(manifest, workspace_root=tmp_path)


def test_archived_binance_wayback_catalog_is_strictly_replayable(tmp_path):
    manifest = _capture_binance_wayback(tmp_path)

    assert manifest.archive_provider == "internet_archive"
    assert {
        entry.catalog_source for entry in manifest.entries
    } == {"internet_archive_memento_of_binance_exchangeInfo"}
    assert verify_archived_binance_catalog(manifest, workspace_root=tmp_path)["verified"]
    assert verify_archived_catalog_evidence(
        manifest.archive_evidence(),
        manifest.entries,
        workspace_root=tmp_path,
    )["verified"]
    snapshot = build_point_in_time_universe_snapshot(
        {"binance": manifest.entries},
        policy=PointInTimeUniversePolicy(
            timeframes=["1h"],
            allowed_symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            selection_scope="per_exchange",
            minimum_listing_age_days=90,
            minimum_symbols_per_exchange=3,
            maximum_symbols_per_exchange=3,
        ),
        as_of=manifest.original_server_time,
        archive_evidence=[manifest.archive_evidence()],
    )
    assert snapshot.historical_survivorship_complete is True
    validate_universe_snapshot(snapshot)

    payload = manifest.model_dump(mode="python")
    payload["archive_replay_url"] = payload["archive_replay_url"].replace(
        "fapi.binance.com", "example.com"
    )
    with pytest.raises(ValueError, match="outside the allowlisted scope"):
        ArchivedExchangeCatalogManifest.model_validate(payload)


def test_archived_catalog_builds_v4_historical_universe(tmp_path):
    manifest = _capture(tmp_path)
    snapshot = build_point_in_time_universe_snapshot(
        {"binance": manifest.entries},
        policy=PointInTimeUniversePolicy(
            timeframes=["1h"],
            allowed_symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            selection_scope="per_exchange",
            minimum_listing_age_days=90,
            minimum_symbols_per_exchange=3,
            maximum_symbols_per_exchange=3,
        ),
        as_of=manifest.original_server_time,
        archive_evidence=[manifest.archive_evidence()],
    )

    assert snapshot.version == 4
    assert snapshot.historical_survivorship_complete is True
    assert snapshot.selected_markets == [
        "binance:BTC/USDT",
        "binance:ETH/USDT",
        "binance:SOL/USDT",
    ]
    settling = next(item for item in snapshot.instruments if item.entry.symbol == "OLD/USDT")
    assert "outside_allowed_symbol_scope" in settling.exclusion_reasons
    assert "market_inactive" in settling.exclusion_reasons
    validate_universe_snapshot(snapshot)


def test_archived_manifest_rejects_unbound_or_unsafe_evidence(tmp_path):
    manifest = _capture(tmp_path)
    payload = manifest.model_dump(mode="python")
    payload["raw_payload_path"] = "../forged.json"
    with pytest.raises(ValueError, match="local path is unsafe"):
        ArchivedExchangeCatalogManifest.model_validate(payload)

    evidence = manifest.archive_evidence().model_copy(
        update={"capture_lag_seconds": manifest.capture_lag_seconds + 1}
    )
    with pytest.raises(ValueError, match="capture lag is inconsistent"):
        build_point_in_time_universe_snapshot(
            {"binance": manifest.entries},
            policy=PointInTimeUniversePolicy(
                timeframes=["1h"],
                allowed_symbols=["BTC/USDT"],
                selection_scope="per_exchange",
                minimum_symbols_per_exchange=1,
                maximum_symbols_per_exchange=1,
            ),
            as_of=manifest.original_server_time,
            archive_evidence=[evidence],
        )


def test_archived_okx_catalog_verifies_compressed_cdx_payload(tmp_path):
    timestamp = "20240216004338"
    records = []
    for base, state, list_time in (
        ("BTC", "live", "1611916828000"),
        ("ETH", "live", "1611917000000"),
        ("SOL", "live", "1615968000000"),
    ):
        records.append(
            {
                "instId": f"{base}-USDT-SWAP",
                "instType": "SWAP",
                "instFamily": f"{base}-USDT",
                "settleCcy": "USDT",
                "ctType": "linear",
                "state": state,
                "listTime": list_time,
            }
        )
    records.append(
        {
            "instId": "BTC-USD-SWAP",
            "instType": "SWAP",
            "settleCcy": "BTC",
            "ctType": "inverse",
            "state": "live",
            "listTime": "1611916828000",
        }
    )
    payload = gzip.compress(
        json.dumps({"code": "0", "data": records}, separators=(",", ":")).encode(),
        mtime=0,
    )
    digest = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    header = ["timestamp", "original", "digest", "statuscode", "mimetype"]
    index = json.dumps(
        [
            header,
            [
                timestamp,
                "https://www.okx.com/api/v5/public/instruments?instType=SWAP",
                digest,
                "200",
                "application/json",
            ],
        ],
        separators=(",", ":"),
    ).encode()

    def fetcher(url: str) -> bytes:
        return index if "/cdx/search/cdx?" in url else payload

    manifest = capture_archived_okx_catalog(
        timestamp=timestamp,
        workspace_root=tmp_path,
        output_root=tmp_path / "okx-evidence",
        fetcher=fetcher,
    )

    assert manifest.raw_symbol_count == 4
    assert manifest.normalized_record_count == 3
    assert manifest.status_counts == {"live": 3}
    assert verify_archived_okx_catalog(manifest, workspace_root=tmp_path)["verified"]
    snapshot = build_point_in_time_universe_snapshot(
        {"okx": manifest.entries},
        policy=PointInTimeUniversePolicy(
            timeframes=["1h"],
            allowed_symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            selection_scope="per_exchange",
            minimum_listing_age_days=90,
            minimum_symbols_per_exchange=3,
            maximum_symbols_per_exchange=3,
        ),
        as_of=manifest.original_server_time,
        archive_evidence=[manifest.archive_evidence()],
    )
    assert snapshot.version == 4
    assert snapshot.historical_survivorship_complete is True
    validate_universe_snapshot(snapshot)

    binance = _capture(tmp_path)
    combined = build_point_in_time_universe_snapshot(
        {"binance": binance.entries, "okx": manifest.entries},
        policy=PointInTimeUniversePolicy(
            timeframes=["1h"],
            allowed_symbols=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            selection_scope="common_across_exchanges",
            minimum_listing_age_days=90,
            minimum_symbols_per_exchange=3,
            maximum_symbols_per_exchange=3,
        ),
        as_of=manifest.original_server_time,
        archive_evidence=[binance.archive_evidence(), manifest.archive_evidence()],
    )
    assert combined.selected_market_count == 6
    assert combined.historical_survivorship_complete is True
    validate_universe_snapshot(combined)

    with pytest.raises(ValueError, match="explicit fixed symbol scope"):
        build_point_in_time_universe_snapshot(
            {"binance": binance.entries, "okx": manifest.entries},
            policy=PointInTimeUniversePolicy(
                timeframes=["1h"],
                selection_scope="common_across_exchanges",
                minimum_symbols_per_exchange=3,
                maximum_symbols_per_exchange=3,
            ),
            as_of=manifest.original_server_time,
            archive_evidence=[binance.archive_evidence(), manifest.archive_evidence()],
        )
