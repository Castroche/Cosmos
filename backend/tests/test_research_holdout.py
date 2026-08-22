from datetime import UTC, datetime, timedelta

import pytest
from cosmos_quant.domain import Candle
from cosmos_quant.research import ResearchDataset, build_research_dataset
from cosmos_quant.research_holdout import SealedHoldoutRequest, seal_research_holdout
from cosmos_quant.research_universe import (
    MarketCatalogEntry,
    PointInTimeUniversePolicy,
    build_point_in_time_universe_snapshot,
)
from cosmos_quant.storage import SqliteStore


def _candles(count: int) -> list[Candle]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(hours=index),
            open="100",
            high="101",
            low="99",
            close="100",
            volume="10",
        )
        for index in range(count)
    ]


def _entry(exchange: str) -> MarketCatalogEntry:
    return MarketCatalogEntry(
        exchange=exchange,
        market_id="BTCUSDT" if exchange == "binance" else "BTC-USDT-SWAP",
        symbol="BTC/USDT",
        base="BTC",
        quote="USDT",
        settle="USDT",
        market_type="swap",
        active=True,
        contract=True,
        linear=True,
        inverse=False,
        listed_at=datetime(2020, 1, 1, tzinfo=UTC),
        listing_time_source="test",
        status="TRADING" if exchange == "binance" else "live",
        asset_class="crypto",
        catalog_source="test",
    )


def _store_with_scope(tmp_path):
    store = SqliteStore(tmp_path / "holdout.db")
    snapshot = build_point_in_time_universe_snapshot(
        {"binance": [_entry("binance")], "okx": [_entry("okx")]},
        policy=PointInTimeUniversePolicy(
            timeframes=["1h"],
            allowed_symbols=["BTC/USDT"],
            selection_scope="common_across_exchanges",
            required_bars_by_timeframe={"1h": 500},
            minimum_symbols_per_exchange=1,
            maximum_symbols_per_exchange=1,
        ),
        as_of=datetime(2024, 12, 1, tzinfo=UTC),
    )
    store.save_research_universe_snapshot(snapshot)
    candles = _candles(1000)
    datasets = []
    for exchange in ("binance", "okx"):
        dataset = build_research_dataset(
            candles,
            exchange=exchange,
            market_type="swap",
            symbol="BTC/USDT",
            timeframe="1h",
        )
        store.save_research_dataset(dataset, candles)
        datasets.append(dataset)
    return store, snapshot, datasets


def test_holdout_service_seals_exact_bounded_universe(tmp_path):
    store, snapshot, sources = _store_with_scope(tmp_path)
    end_exclusive = datetime(2025, 2, 11, 16, tzinfo=UTC)
    report = seal_research_holdout(
        store,
        SealedHoldoutRequest(
            dataset_ids=[source.id for source in sources],
            universe_snapshot_id=snapshot.id,
            development_ratio=0.70,
            embargo_days=1,
            window_start=datetime(2025, 1, 1, tzinfo=UTC),
            window_end_exclusive=end_exclusive,
        ),
    )

    assert report.universe_snapshot_id == snapshot.id
    assert len(report.datasets) == 2
    assert len(report.development_dataset_ids) == len(report.blind_dataset_ids) == 2
    for item in report.datasets:
        development = ResearchDataset.model_validate(
            store.load_research_dataset(item.development_dataset_id)
        )
        blind = ResearchDataset.model_validate(
            store.load_research_dataset(item.blind_dataset_id)
        )
        assert development.research_role == "development"
        assert development.blind_sealed is False
        assert blind.research_role == "blind"
        assert blind.blind_sealed is True
        assert blind.data_end < end_exclusive
        assert development.blind_set_id == blind.blind_set_id == report.blind_set_id
        assert development.source_dataset_id == blind.source_dataset_id


def test_holdout_service_rejects_cherry_picked_scope(tmp_path):
    store, snapshot, sources = _store_with_scope(tmp_path)

    with pytest.raises(ValueError, match="exactly cover"):
        seal_research_holdout(
            store,
            SealedHoldoutRequest(
                dataset_ids=[sources[0].id],
                universe_snapshot_id=snapshot.id,
            ),
        )
