import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant import api
from cosmos_quant.domain import Candle
from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter
from cosmos_quant.research import build_research_dataset
from cosmos_quant.research_universe import (
    HistoricalCatalogArchiveEvidence,
    MarketCatalogEntry,
    PointInTimeUniversePolicy,
    ResearchUniverseSnapshot,
    build_point_in_time_universe_snapshot,
    catalog_archive_sha256,
    validate_universe_snapshot,
)
from cosmos_quant.storage import SqliteStore


def _candles(count: int, timeframe_minutes: int = 15) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(minutes=timeframe_minutes * index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("10"),
        )
        for index in range(count)
    ]


def test_research_universe_materializes_each_requested_exchange(monkeypatch):
    adapters = []

    class Adapter:
        def __init__(self, exchange_id, *_args, **_kwargs):
            self.exchange_id = exchange_id
            self.closed = False
            adapters.append(self)

        async def fetch_candles(self, _symbol, _timeframe, limit):
            return _candles(limit)

        async def close(self):
            self.closed = True

    async def materialize(
        candles,
        *,
        exchange,
        market_type,
        symbol,
        timeframe,
        adapter,
        quality,
    ):
        assert adapter.exchange_id == exchange
        dataset = build_research_dataset(
            candles,
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            quality=quality,
        )
        return dataset, [], [], [], []

    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    monkeypatch.setattr(api, "materialize_research_dataset", materialize)

    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                symbols=["BTC/USDT"],
                timeframes=["15m"],
                exchange_ids=["okx", "binance"],
                bars=500,
                max_markets=2,
            )
        )
    )

    assert report.requested_markets == 2
    assert report.completed_markets == 2
    assert report.healthy_markets == 2
    assert [cell.exchange for cell in report.cells] == ["okx", "binance"]
    assert all(adapter.closed for adapter in adapters)


def test_research_universe_rejects_unapproved_exchange_before_network():
    with pytest.raises(ValueError, match="unsupported research exchanges"):
        asyncio.run(
            api.prepare_research_universe(
                api.ResearchUniverseRequest(
                    symbols=["BTC/USDT"],
                    timeframes=["1h"],
                    exchange_ids=["unknown-exchange"],
                )
            )
        )


def test_public_research_can_override_configured_market_type(tmp_path, monkeypatch):
    observed = {}
    local_store = SqliteStore(tmp_path / "spot-override.db")

    class Adapter:
        def __init__(self, _exchange_id, *_args, **kwargs):
            observed["adapter_market_type"] = kwargs["market_type"]

        async def fetch_candles(self, _symbol, _timeframe, limit):
            return _candles(limit)

        async def close(self):
            pass

    async def materialize(candles, **kwargs):
        observed["dataset_market_type"] = kwargs["market_type"]
        dataset = build_research_dataset(
            candles,
            exchange=kwargs["exchange"],
            market_type=kwargs["market_type"],
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
        )
        return dataset, [], [], [], []

    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    monkeypatch.setattr(api, "materialize_research_dataset", materialize)
    monkeypatch.setattr(api, "store", local_store)
    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                symbols=["BTC/USDT"],
                timeframes=["15m"],
                exchange_ids=["binance"],
                bars=500,
                max_markets=1,
                market_type="spot",
            )
        )
    )

    assert report.healthy_markets == 1
    assert observed == {
        "adapter_market_type": "spot",
        "dataset_market_type": "spot",
    }


def test_market_type_override_never_reuses_a_fresh_opposite_market_dataset(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "market-type-isolation.db")
    fresh_start = datetime.now(UTC) - timedelta(minutes=15 * 500)
    fresh_candles = [
        candle.model_copy(update={"timestamp": fresh_start + timedelta(minutes=15 * index)})
        for index, candle in enumerate(_candles(500))
    ]
    swap_dataset = build_research_dataset(
        fresh_candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="15m",
    )
    local_store.save_research_dataset(swap_dataset, fresh_candles, [], [], [], [])
    materialized = []

    class Adapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def fetch_candles(self, _symbol, _timeframe, _limit):
            return fresh_candles

        async def close(self):
            pass

    async def materialize(candles, **kwargs):
        materialized.append(kwargs["market_type"])
        dataset = build_research_dataset(
            candles,
            exchange=kwargs["exchange"],
            market_type=kwargs["market_type"],
            symbol=kwargs["symbol"],
            timeframe=kwargs["timeframe"],
        )
        local_store.save_research_dataset(dataset, candles, [], [], [], [])
        return dataset, [], [], [], []

    monkeypatch.setattr(api, "store", local_store)
    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    monkeypatch.setattr(api, "materialize_research_dataset", materialize)
    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                symbols=["BTC/USDT"],
                timeframes=["15m"],
                exchange_ids=["binance"],
                bars=500,
                max_markets=1,
                market_type="spot",
            )
        )
    )

    assert materialized == ["spot"]
    assert report.cells[0].dataset_id != swap_dataset.id


def test_durable_universe_job_persists_progress_and_report(tmp_path, monkeypatch):
    local_store = SqliteStore(tmp_path / "universe-job.db")
    request = api.ResearchUniverseRequest(
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        exchange_ids=["binance"],
        bars=500,
        max_markets=1,
    )
    cell = api.ResearchUniverseCell(
        exchange="binance",
        symbol="BTC/USDT",
        timeframe="1h",
        requested_bars=500,
        candle_count=500,
        coverage_ratio=Decimal("1"),
        dataset_id="dataset-durable",
        quality_status="healthy",
        ok=True,
    )

    async def prepare(_request, *, progress_callback=None):
        assert progress_callback is not None
        await progress_callback(1, 1, cell)
        return api.ResearchUniverseReport(
            requested_markets=1,
            completed_markets=1,
            healthy_markets=1,
            total_candles=500,
            cells=[cell],
        )

    monkeypatch.setattr(api, "store", local_store)
    monkeypatch.setattr(api, "prepare_research_universe", prepare)
    normalized = request.model_dump(mode="json")
    job = api.ResearchUniverseJob(
        request=normalized,
        request_fingerprint=api.research_universe_request_fingerprint(normalized),
        total_markets=1,
    )

    asyncio.run(api.run_research_universe_job(job))

    restored = api.ResearchUniverseJob.model_validate(
        local_store.load_research_universe_job(job.id)
    )
    assert restored.status == api.ResearchJobStatus.COMPLETED
    assert restored.completed_markets == 1
    assert restored.healthy_markets == 1
    assert restored.report is not None
    assert restored.report.cells[0].dataset_id == "dataset-durable"


def test_research_universe_reuses_fresh_complete_dataset_after_restart(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "universe-resume.db")
    candles = _candles(8761, timeframe_minutes=60)
    now = datetime.now(UTC)
    offset = now - candles[-1].timestamp - timedelta(minutes=30)
    candles = [
        candle.model_copy(update={"timestamp": candle.timestamp + offset}) for candle in candles
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    local_store.save_research_dataset(dataset, candles, [], [], [], [])

    class Adapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def fetch_candles(self, *_args, **_kwargs):
            raise AssertionError("fresh completed market should not be fetched again")

        async def close(self):
            pass

    monkeypatch.setattr(api, "store", local_store)
    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                symbols=["BTC/USDT"],
                timeframes=["1h"],
                exchange_ids=["binance"],
                bars=500,
                max_markets=1,
            )
        )
    )

    assert report.completed_markets == 1
    assert report.cells[0].dataset_id == dataset.id


def _catalog_entry(
    exchange: str,
    symbol: str,
    volume: str,
    *,
    active: bool | None = True,
    listed_at: datetime | None = None,
    asset_class: str = "unknown",
) -> MarketCatalogEntry:
    base = symbol.split("/", 1)[0]
    return MarketCatalogEntry(
        exchange=exchange,
        market_id=f"{base}USDT",
        symbol=symbol,
        base=base,
        quote="USDT",
        settle="USDT",
        market_type="swap",
        active=active,
        contract=True,
        linear=True,
        inverse=False,
        listed_at=listed_at or datetime(2020, 1, 1, tzinfo=UTC),
        listing_time_source="test.listed_at",
        quote_volume_24h=Decimal(volume),
        asset_class=asset_class,
    )


def test_point_in_time_snapshot_is_deterministic_ranked_and_tamper_evident():
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    policy = PointInTimeUniversePolicy(
        timeframes=["15m"],
        minimum_symbols_per_exchange=2,
        maximum_symbols_per_exchange=2,
    )
    catalogs = {
        "binance": [
            _catalog_entry("binance", "ETH/USDT", "200"),
            _catalog_entry("binance", "BTC/USDT", "300"),
            _catalog_entry("binance", "OLD/USDT", "999", active=False),
        ]
    }

    first = build_point_in_time_universe_snapshot(catalogs, policy=policy, as_of=as_of)
    second = build_point_in_time_universe_snapshot(
        {"binance": list(reversed(catalogs["binance"]))},
        policy=policy,
        as_of=as_of,
    )

    assert first.fingerprint == second.fingerprint
    assert first.selected_markets == ["binance:BTC/USDT", "binance:ETH/USDT"]
    assert first.point_in_time_selection_passed is True
    assert first.historical_survivorship_complete is False
    inactive = next(item for item in first.instruments if item.entry.symbol == "OLD/USDT")
    assert inactive.eligible is False
    assert "market_inactive" in inactive.exclusion_reasons
    validate_universe_snapshot(first)
    tampered = first.model_copy(update={"selected_market_count": 999})
    with pytest.raises(ValueError, match="derived evidence mismatch"):
        validate_universe_snapshot(tampered)
    payload = first.model_dump(mode="python")
    payload["instruments"][0]["selected"] = not payload["instruments"][0]["selected"]
    with pytest.raises(ValueError, match="(derived evidence|fingerprint) mismatch"):
        validate_universe_snapshot(ResearchUniverseSnapshot.model_validate(payload))


def test_historical_archive_snapshot_requires_source_bound_complete_evidence():
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    catalogs = {
        "binance": [
            _catalog_entry("binance", "BTC/USDT", "300"),
            _catalog_entry("binance", "OLD/USDT", "1", active=False),
        ]
    }
    archive = HistoricalCatalogArchiveEvidence(
        exchange="binance",
        market_type="swap",
        archive_as_of=as_of,
        source_kind="official_point_in_time_archive",
        source_url="https://example.test/binance/archive-2026-08-01.json",
        payload_sha256=catalog_archive_sha256(catalogs["binance"]),
        record_count=2,
    )

    snapshot = build_point_in_time_universe_snapshot(
        catalogs,
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=as_of,
        archive_evidence=[archive],
    )

    assert snapshot.version == 3
    assert snapshot.historical_survivorship_complete is True
    assert snapshot.survivorship_scope == "historical_archive"
    assert snapshot.archive_evidence == [archive]
    assert snapshot.limitations == []
    validate_universe_snapshot(snapshot)


def test_historical_event_archive_rejects_gaps_and_tampering():
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    catalogs = {"binance": [_catalog_entry("binance", "BTC/USDT", "300")]}
    incomplete = HistoricalCatalogArchiveEvidence(
        exchange="binance",
        market_type="swap",
        archive_as_of=as_of,
        source_kind="event_sourced_catalog_journal",
        source_url="https://example.test/binance/catalog-events.jsonl",
        payload_sha256=catalog_archive_sha256(catalogs["binance"]),
        record_count=1,
        initial_full_snapshot_at=as_of - timedelta(days=30),
        event_coverage_end=as_of,
        event_log_gap_free=False,
    )

    with pytest.raises(ValueError, match="gap-free events"):
        build_point_in_time_universe_snapshot(
            catalogs,
            policy=PointInTimeUniversePolicy(
                timeframes=["15m"],
                minimum_symbols_per_exchange=1,
                maximum_symbols_per_exchange=1,
            ),
            as_of=as_of,
            archive_evidence=[incomplete],
        )


def test_historical_archive_import_persists_only_hash_matching_catalogs(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "historical-archive.db")
    monkeypatch.setattr(api, "store", local_store)
    as_of = datetime(2026, 8, 1, tzinfo=UTC)
    catalogs = {
        "binance": [
            _catalog_entry("binance", "BTC/USDT", "300"),
            _catalog_entry("binance", "OLD/USDT", "1", active=False),
        ]
    }
    evidence = HistoricalCatalogArchiveEvidence(
        exchange="binance",
        market_type="swap",
        archive_as_of=as_of,
        source_kind="official_point_in_time_archive",
        source_url="https://example.test/binance/archive-2026-08-01.json",
        payload_sha256=catalog_archive_sha256(catalogs["binance"]),
        record_count=2,
    )
    request = api.HistoricalUniverseArchiveImportRequest(
        as_of=as_of,
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        catalogs=catalogs,
        archive_evidence=[evidence],
    )

    snapshot = asyncio.run(api.import_historical_universe_archive(request))

    assert snapshot.historical_survivorship_complete is True
    assert local_store.load_research_universe_snapshot(snapshot.id) is not None
    tampered = request.model_copy(
        update={"archive_evidence": [evidence.model_copy(update={"payload_sha256": "c" * 64})]}
    )
    with pytest.raises(api.HTTPException, match="payload hash"):
        asyncio.run(api.import_historical_universe_archive(tampered))


def test_point_in_time_snapshot_excludes_verified_non_crypto_underlyings():
    snapshot = build_point_in_time_universe_snapshot(
        {
            "binance": [
                _catalog_entry("binance", "BTC/USDT", "300", asset_class="crypto"),
                _catalog_entry(
                    "binance",
                    "SNDK/USDT",
                    "999",
                    asset_class="non_crypto",
                ),
            ]
        },
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert snapshot.selected_markets == ["binance:BTC/USDT"]
    excluded = next(item for item in snapshot.instruments if item.entry.symbol == "SNDK/USDT")
    assert "verified_non_crypto_underlying" in excluded.exclusion_reasons


def test_snapshot_listing_quarantines_invalid_legacy_artifact(monkeypatch):
    valid = build_point_in_time_universe_snapshot(
        {"binance": [_catalog_entry("binance", "BTC/USDT", "300")]},
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )
    invalid_payload = valid.model_dump(mode="python")
    invalid_payload["id"] = "universe_invalid_legacy"
    invalid_payload["selected_market_count"] = 999

    class Store:
        def list_research_universe_snapshots(self, _limit):
            return [invalid_payload, valid.model_dump(mode="python")]

        def load_research_universe_snapshot(self, snapshot_id):
            return invalid_payload if snapshot_id == "universe_invalid_legacy" else None

    monkeypatch.setattr(api, "store", Store())

    listed = asyncio.run(api.list_research_universe_snapshots())
    assert [snapshot.id for snapshot in listed] == [valid.id]
    with pytest.raises(api.HTTPException) as exc_info:
        asyncio.run(api.get_research_universe_snapshot("universe_invalid_legacy"))
    assert exc_info.value.status_code == 409


def test_ccxt_catalog_classifies_exchange_underlying_metadata():
    assert CcxtExchangeAdapter._catalog_asset_class(
        {"underlyingType": "STOCK", "underlyingSubType": ["US_EQUITY"]}
    )[:2] == ("non_crypto", "market.info.underlyingType,market.info.underlyingSubType")
    assert (
        CcxtExchangeAdapter._catalog_asset_class(
            {"underlyingType": "COIN", "underlyingSubType": ["Layer-1"]}
        )[0]
        == "crypto"
    )


def test_frozen_snapshot_materializes_exact_archived_scope(tmp_path, monkeypatch):
    local_store = SqliteStore(tmp_path / "frozen-universe.db")
    snapshot = build_point_in_time_universe_snapshot(
        {
            "binance": [_catalog_entry("binance", "BTC/USDT", "300")],
            "okx": [_catalog_entry("okx", "BTC/USDT", "300")],
        },
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            allowed_symbols=["BTC/USDT"],
            selection_scope="common_across_exchanges",
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=datetime(2025, 8, 1, tzinfo=UTC),
    )
    local_store.save_research_universe_snapshot(snapshot)
    adapters = []

    class Adapter:
        def __init__(self, exchange_id, *_args, **_kwargs):
            self.exchange_id = exchange_id
            self.closed = False
            adapters.append(self)

        async def fetch_candles(self, _symbol, _timeframe, limit):
            return _candles(limit)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(api, "store", local_store)
    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                selection_mode="frozen_snapshot",
                universe_snapshot_id=snapshot.id,
                symbols=["BTC/USDT"],
                timeframes=["15m"],
                exchange_ids=["binance", "okx"],
                bars=500,
                max_markets=2,
                market_type="swap",
                feature_mode="ohlcv_only",
            )
        )
    )

    assert report.universe_snapshot_id == snapshot.id
    assert report.requested_markets == report.completed_markets == 2
    assert report.materialized_scope_complete is True
    assert all(cell.universe_snapshot_id == snapshot.id for cell in report.cells)
    assert all(adapter.closed for adapter in adapters)

    with pytest.raises(ValueError, match="symbols must exactly match"):
        asyncio.run(
            api.prepare_research_universe(
                api.ResearchUniverseRequest(
                    selection_mode="frozen_snapshot",
                    universe_snapshot_id=snapshot.id,
                    symbols=["ETH/USDT"],
                    timeframes=["15m"],
                    exchange_ids=["binance", "okx"],
                    bars=500,
                    max_markets=2,
                    market_type="swap",
                )
            )
        )
    assert all(adapter.closed for adapter in adapters)


def test_point_in_time_universe_discovers_and_persists_exact_market_scope(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "point-in-time-universe.db")
    adapters = []

    class Adapter:
        def __init__(self, exchange_id, *_args, **_kwargs):
            self.exchange_id = exchange_id
            self.closed = False
            adapters.append(self)

        async def research_market_catalog(self):
            return [
                _catalog_entry(self.exchange_id, "BTC/USDT", "300"),
                _catalog_entry(self.exchange_id, "ETH/USDT", "200"),
                _catalog_entry(self.exchange_id, "SOL/USDT", "100"),
                _catalog_entry(self.exchange_id, "OLD/USDT", "999", active=False),
            ]

        async def fetch_candles(self, _symbol, _timeframe, limit):
            return _candles(limit)

        async def close(self):
            self.closed = True

    async def materialize(
        candles,
        *,
        exchange,
        market_type,
        symbol,
        timeframe,
        adapter,
        quality,
    ):
        dataset = build_research_dataset(
            candles,
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            quality=quality,
        )
        local_store.save_research_dataset(dataset, candles, [], [], [], [])
        return dataset, [], [], [], []

    monkeypatch.setattr(api, "store", local_store)
    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    monkeypatch.setattr(api, "materialize_research_dataset", materialize)

    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                selection_mode="point_in_time",
                timeframes=["15m"],
                exchange_ids=["binance", "okx"],
                bars=500,
                max_markets=6,
                minimum_symbols_per_exchange=3,
                maximum_symbols_per_exchange=3,
            )
        )
    )

    assert report.requested_markets == 6
    assert report.completed_markets == 6
    assert report.point_in_time_selection_passed is True
    assert report.historical_survivorship_complete is False
    assert all(cell.universe_snapshot_id == report.universe_snapshot_id for cell in report.cells)
    assert {cell.symbol for cell in report.cells} == {
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
    }
    payload = local_store.load_research_universe_snapshot(report.universe_snapshot_id)
    assert payload is not None
    snapshot = ResearchUniverseSnapshot.model_validate(payload)
    validate_universe_snapshot(snapshot)
    assert snapshot.selected_market_count == 6
    assert all(adapter.closed for adapter in adapters)


def test_campaign_rejects_cherry_picked_subset_of_frozen_universe(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "universe-campaign.db")
    snapshot = build_point_in_time_universe_snapshot(
        {
            "binance": [
                _catalog_entry("binance", "BTC/USDT", "300"),
                _catalog_entry("binance", "ETH/USDT", "200"),
            ]
        },
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            minimum_symbols_per_exchange=2,
            maximum_symbols_per_exchange=2,
        ),
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )
    local_store.save_research_universe_snapshot(snapshot)
    candles = _candles(1200)
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="15m",
    )
    local_store.save_research_dataset(dataset, candles, [], [], [], [])
    monkeypatch.setattr(api, "store", local_store)

    with pytest.raises(api.HTTPException, match="exact frozen point-in-time universe"):
        asyncio.run(
            api.execute_research_campaign(
                api.ResearchCampaignRequest(
                    dataset_ids=[dataset.id],
                    universe_snapshot_id=snapshot.id,
                    library_entry_ids=["builtin_ema_12_26_trend_v2"],
                    max_backtests=1,
                )
            )
        )


def test_campaign_rejects_exact_scope_with_incomplete_frozen_history(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "universe-history-contract.db")
    snapshot = build_point_in_time_universe_snapshot(
        {"binance": [_catalog_entry("binance", "BTC/USDT", "300")]},
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            required_bars_by_timeframe={"15m": 2000},
            minimum_history_coverage_ratio=Decimal("0.90"),
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )
    local_store.save_research_universe_snapshot(snapshot)
    candles = _candles(1200)
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="15m",
    )
    local_store.save_research_dataset(dataset, candles, [], [], [], [])
    monkeypatch.setattr(api, "store", local_store)

    with pytest.raises(api.HTTPException, match="history coverage contract"):
        asyncio.run(
            api.execute_research_campaign(
                api.ResearchCampaignRequest(
                    dataset_ids=[dataset.id],
                    universe_snapshot_id=snapshot.id,
                    library_entry_ids=["builtin_ema_12_26_trend_v2"],
                    max_backtests=1,
                )
            )
        )


def test_campaign_rejects_data_that_predates_the_frozen_universe(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "universe-temporal-order.db")
    snapshot = build_point_in_time_universe_snapshot(
        {"binance": [_catalog_entry("binance", "BTC/USDT", "300")]},
        policy=PointInTimeUniversePolicy(
            timeframes=["15m"],
            required_bars_by_timeframe={"15m": 500},
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=datetime(2026, 8, 1, tzinfo=UTC),
    )
    local_store.save_research_universe_snapshot(snapshot)
    candles = _candles(1200)
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="15m",
    )
    local_store.save_research_dataset(dataset, candles, [], [], [], [])
    monkeypatch.setattr(api, "store", local_store)

    with pytest.raises(api.HTTPException, match="start at or after"):
        asyncio.run(
            api.execute_research_campaign(
                api.ResearchCampaignRequest(
                    dataset_ids=[dataset.id],
                    universe_snapshot_id=snapshot.id,
                    library_entry_ids=["builtin_ema_12_26_trend_v2"],
                    max_backtests=1,
                )
            )
        )


def test_point_in_time_report_marks_partial_history_as_unmaterialized(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "universe-partial-history.db")

    class Adapter:
        def __init__(self, exchange_id, *_args, **_kwargs):
            self.exchange_id = exchange_id

        async def research_market_catalog(self):
            return [_catalog_entry(self.exchange_id, "BTC/USDT", "300")]

        async def fetch_candles(self, *_args):
            return _candles(400)

        async def fetch_funding_history(self, *_args):
            return []

        async def fetch_open_interest_history(self, *_args):
            return []

        async def fetch_positioning_history(self, *_args):
            return []

        async def fetch_basis_history(self, *_args):
            return []

        async def close(self):
            pass

    monkeypatch.setattr(api, "store", local_store)
    monkeypatch.setattr(api, "CcxtExchangeAdapter", Adapter)
    report = asyncio.run(
        api.prepare_research_universe(
            api.ResearchUniverseRequest(
                selection_mode="point_in_time",
                timeframes=["15m"],
                exchange_ids=["binance"],
                bars=500,
                max_markets=1,
                minimum_symbols_per_exchange=1,
                maximum_symbols_per_exchange=1,
                minimum_history_coverage_ratio=Decimal("0.90"),
            )
        )
    )

    assert report.completed_markets == 1
    assert report.healthy_markets == 0
    assert report.materialized_scope_complete is False
    assert report.cells[0].coverage_ratio == Decimal("0.800")
    assert report.cells[0].history_coverage_passed is False


def test_dataset_materialization_fetches_independent_features_concurrently(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "parallel-features.db")
    active = 0
    maximum_active = 0

    class Adapter:
        async def _feature(self):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        async def fetch_funding_history(self, *_args):
            return await self._feature()

        async def fetch_open_interest_history(self, *_args):
            return await self._feature()

        async def fetch_positioning_history(self, *_args):
            return await self._feature()

        async def fetch_basis_history(self, *_args):
            return await self._feature()

    monkeypatch.setattr(api, "store", local_store)
    dataset, funding, open_interest, positioning, basis = asyncio.run(
        api.materialize_research_dataset(
            _candles(500),
            exchange="binance",
            market_type="swap",
            symbol="BTC/USDT",
            timeframe="15m",
            adapter=Adapter(),
        )
    )

    assert maximum_active == 4
    assert funding == open_interest == positioning == basis == []
    assert local_store.load_research_dataset(dataset.id) is not None


def test_dataset_materialization_can_seal_ohlcv_before_feature_enrichment(
    tmp_path,
    monkeypatch,
):
    local_store = SqliteStore(tmp_path / "ohlcv-first.db")

    class Adapter:
        def __getattr__(self, name):
            if name.startswith("fetch_"):
                raise AssertionError(f"unexpected derivative request: {name}")
            raise AttributeError(name)

    monkeypatch.setattr(api, "store", local_store)
    dataset, funding, open_interest, positioning, basis = asyncio.run(
        api.materialize_research_dataset(
            _candles(500),
            exchange="okx",
            market_type="swap",
            symbol="BTC/USDT",
            timeframe="15m",
            adapter=Adapter(),
            fetch_derivative_features=False,
        )
    )

    assert funding == open_interest == positioning == basis == []
    assert dataset.funding_status == "unavailable"
    assert dataset.open_interest_status == "unavailable"
    assert local_store.load_research_dataset(dataset.id) is not None
