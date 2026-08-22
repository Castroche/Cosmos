from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.catalog_journal import (
    MarketCatalogJournalSnapshot,
    build_catalog_journal_observation,
    evaluate_catalog_journal_coverage,
    validate_catalog_journal_observation,
)
from cosmos_quant.research_universe import MarketCatalogEntry
from cosmos_quant.storage import SqliteStore


def _entry(market_id: str, *, active: bool = True) -> MarketCatalogEntry:
    base = market_id.removesuffix("USDT")
    return MarketCatalogEntry(
        exchange="binance",
        market_id=market_id,
        symbol=f"{base}/USDT",
        base=base,
        quote="USDT",
        settle="USDT",
        market_type="swap",
        active=active,
        contract=True,
        linear=True,
        inverse=False,
        catalog_source="binance-fapi-exchangeInfo",
    )


def test_catalog_journal_records_added_removed_and_modified_markets(tmp_path):
    observed_at = datetime(2026, 8, 15, tzinfo=UTC)
    first, initial_events = build_catalog_journal_observation(
        [_entry("BTCUSDT"), _entry("ETHUSDT")],
        exchange="binance",
        market_type="swap",
        source_url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        observed_at=observed_at,
    )
    second, change_events = build_catalog_journal_observation(
        [_entry("BTCUSDT", active=False), _entry("SOLUSDT")],
        exchange="binance",
        market_type="swap",
        source_url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        observed_at=observed_at + timedelta(hours=1),
        previous=first,
    )

    assert len(initial_events) == 2
    assert {(item.market_id, item.change_type) for item in change_events} == {
        ("BTCUSDT", "modified"),
        ("ETHUSDT", "removed"),
        ("SOLUSDT", "added"),
    }
    assert second.previous_snapshot_id == first.id
    validate_catalog_journal_observation(second, change_events, previous=first)

    store = SqliteStore(tmp_path / "catalog.db")
    store.append_market_catalog_journal_observation(first, initial_events)
    store.append_market_catalog_journal_observation(second, change_events)
    restored = MarketCatalogJournalSnapshot.model_validate(
        store.load_latest_market_catalog_journal_snapshot(
            exchange="binance", market_type="swap"
        )
    )
    assert restored.id == second.id
    assert len(store.list_market_catalog_change_events()) == 5


def test_catalog_journal_unchanged_observation_advances_chain_without_fake_events():
    observed_at = datetime(2026, 8, 15, tzinfo=UTC)
    first, _ = build_catalog_journal_observation(
        [_entry("BTCUSDT")],
        exchange="binance",
        market_type="swap",
        source_url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        observed_at=observed_at,
    )
    second, events = build_catalog_journal_observation(
        list(first.entries),
        exchange="binance",
        market_type="swap",
        source_url=first.source_url,
        observed_at=observed_at + timedelta(minutes=5),
        previous=first,
    )

    assert events == []
    assert second.chain_sha256 != first.chain_sha256
    validate_catalog_journal_observation(second, events, previous=first)


def test_catalog_journal_rejects_tampering_and_chain_forks(tmp_path):
    observed_at = datetime(2026, 8, 15, tzinfo=UTC)
    first, events = build_catalog_journal_observation(
        [_entry("BTCUSDT")],
        exchange="binance",
        market_type="swap",
        source_url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        observed_at=observed_at,
    )
    tampered = first.model_copy(update={"entries": [_entry("BTCUSDT", active=False)]})
    with pytest.raises(ValueError, match="payload hash mismatch"):
        validate_catalog_journal_observation(tampered, events, previous=None)

    store = SqliteStore(tmp_path / "fork.db")
    store.append_market_catalog_journal_observation(first, events)
    competing, competing_events = build_catalog_journal_observation(
        [_entry("ETHUSDT")],
        exchange="binance",
        market_type="swap",
        source_url=first.source_url,
        observed_at=observed_at + timedelta(minutes=5),
        previous=None,
    )
    with pytest.raises(RuntimeError, match="fork or skip"):
        store.append_market_catalog_journal_observation(competing, competing_events)


def test_catalog_journal_reports_polling_continuity_without_claiming_official_archive():
    observed_at = datetime(2026, 8, 15, tzinfo=UTC)
    first, first_events = build_catalog_journal_observation(
        [_entry("BTCUSDT")],
        exchange="binance",
        market_type="swap",
        source_url="https://fapi.binance.com/fapi/v1/exchangeInfo",
        observed_at=observed_at,
    )
    second, second_events = build_catalog_journal_observation(
        [_entry("BTCUSDT"), _entry("ETHUSDT")],
        exchange="binance",
        market_type="swap",
        source_url=first.source_url,
        observed_at=observed_at + timedelta(hours=1),
        previous=first,
    )

    coverage = evaluate_catalog_journal_coverage(
        [first, second],
        first_events + second_events,
        exchange="binance",
        market_type="swap",
        now=observed_at + timedelta(hours=1, minutes=5),
    )

    assert coverage.chain_valid is True
    assert coverage.observation_schedule_gap_free is True
    assert coverage.maximum_observation_gap_seconds == Decimal("3600.0")
    assert coverage.historical_survivorship_complete is False
    assert "not an official gap-free" in coverage.reasons[-1]
