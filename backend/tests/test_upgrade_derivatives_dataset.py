import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cosmos_quant.domain import BasisPoint, FundingRatePoint
from cosmos_quant.research import ResearchDataset

from scripts import upgrade_derivatives_dataset as upgrade_script


def test_optional_positioning_failure_does_not_block_basis_upgrade(monkeypatch):
    started = datetime(2025, 1, 1, tzinfo=UTC)
    source = ResearchDataset(
        id="ds_source",
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="4h",
        candle_count=2,
        data_start=started,
        data_end=started + timedelta(hours=4),
        data_fingerprint="source-fingerprint",
        quality_status="healthy",
        source="okx:rest",
        timeframe_seconds=14_400,
        expected_candle_count=2,
        coverage_ratio="1",
    )
    candle_payloads = [
        {
            "timestamp": started + timedelta(hours=4 * index),
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "10",
        }
        for index in range(2)
    ]

    class FakeStore:
        def load_research_dataset(self, dataset_id):
            assert dataset_id == source.id
            return source.model_dump(mode="json")

        def load_research_dataset_candles(self, dataset_id):
            return candle_payloads

        def load_research_dataset_funding(self, dataset_id):
            return []

        def load_research_dataset_open_interest(self, dataset_id):
            return []

        def load_research_dataset_positioning(self, dataset_id):
            return []

        def load_research_dataset_basis(self, dataset_id):
            return []

        def load_research_dataset_order_flow(self, dataset_id):
            return []

        def load_research_dataset_liquidation_flow(self, dataset_id):
            return []

        def save_research_dataset(self, *args):
            return None

    class FakeAdapter:
        async def fetch_positioning_history(self, *args):
            raise TimeoutError("optional positioning endpoint timed out")

        async def fetch_basis_history(self, *args):
            return [
                BasisPoint(
                    timestamp=started,
                    mark_price="101",
                    index_price="100",
                    basis_rate="0.01",
                )
            ]

    captured = {}

    def fake_build(candles, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="ds_upgraded",
            exchange="okx",
            symbol="BTC/USDT",
            timeframe="4h",
            positioning_count=len(kwargs["positioning"]),
            positioning_coverage_ratio=0,
            basis_count=len(kwargs["basis"]),
            basis_coverage_ratio=1,
            order_flow_count=len(kwargs["order_flow"]),
        )

    monkeypatch.setattr(upgrade_script.api, "store", FakeStore())
    monkeypatch.setattr(upgrade_script, "build_research_dataset", fake_build)
    monkeypatch.setattr(
        upgrade_script,
        "verify_research_dataset",
        lambda *args, **kwargs: {"verified": True},
    )

    report = asyncio.run(upgrade_script.upgrade(source.id, adapter=FakeAdapter()))

    assert report["verified"] is True
    assert report["upgrade_complete"] is True
    assert report["basis_upgrade_complete"] is True
    assert report["basis_count"] == 1
    assert report["positioning_count"] == 0
    assert report["warnings"][0]["feature"] == "positioning"
    assert report["warnings"][0]["error_type"] == "TimeoutError"
    assert len(captured["basis"]) == 1


def test_okx_official_funding_archives_upgrade_long_dataset_and_bind_manifest(
    tmp_path,
    monkeypatch,
):
    started = datetime(2025, 1, 1, tzinfo=UTC)
    ended = started + timedelta(days=40)
    source = ResearchDataset(
        id="ds_okx_source",
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        candle_count=2,
        data_start=started,
        data_end=ended,
        data_fingerprint="source-fingerprint",
        quality_status="healthy",
        source="okx:rest",
        basis_coverage_ratio="1",
        timeframe_seconds=3_600,
        expected_candle_count=961,
        coverage_ratio="0.0021",
    )
    candle_payloads = [
        {
            "timestamp": timestamp,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "10",
        }
        for timestamp in (started, ended)
    ]
    saved = {}

    class FakeStore:
        def load_research_dataset(self, dataset_id):
            assert dataset_id == source.id
            return source.model_dump(mode="json")

        def load_research_dataset_candles(self, _dataset_id):
            return candle_payloads

        def load_research_dataset_funding(self, _dataset_id):
            return [{"timestamp": started, "rate": "0.0099"}]

        def load_research_dataset_open_interest(self, _dataset_id):
            return []

        def load_research_dataset_positioning(self, _dataset_id):
            return []

        def load_research_dataset_basis(self, _dataset_id):
            return []

        def load_research_dataset_order_flow(self, _dataset_id):
            return []

        def load_research_dataset_liquidation_flow(self, _dataset_id):
            return []

        def save_research_dataset(self, dataset, *args):
            saved["dataset"] = dataset
            saved["funding"] = args[1]

    class FakeAdapter:
        pass

    evidence = SimpleNamespace(
        manifest=lambda: {
            "filename": "BTC-USDT-SWAP-fundingrates-2025-01.zip",
            "archive_sha256": "a" * 64,
            "archive_size_bytes": 100,
        }
    )

    def fake_archive_fetch(symbol, start, end, **kwargs):
        assert symbol == source.symbol
        assert start == started
        assert end == ended
        assert kwargs["destination"] == tmp_path
        return (
            [
                FundingRatePoint(timestamp=started, rate="0.0001"),
                FundingRatePoint(timestamp=ended, rate="-0.0001"),
            ],
            [evidence],
        )

    monkeypatch.setattr(upgrade_script.api, "store", FakeStore())
    monkeypatch.setattr(upgrade_script, "fetch_okx_funding_archive_history", fake_archive_fetch)
    monkeypatch.setattr(
        upgrade_script,
        "verify_research_dataset",
        lambda *args, **kwargs: {"verified": True},
    )

    report = asyncio.run(
        upgrade_script.upgrade(
            source.id,
            adapter=FakeAdapter(),
            fetch_positioning=False,
            archive_destination=tmp_path,
        )
    )

    assert report["upgrade_complete"] is True
    assert report["funding_upgrade_requested"] is True
    assert report["funding_upgrade_complete"] is True
    assert report["funding_coverage_ratio"] == "1.0000"
    assert report["funding_archive_count"] == 1
    assert report["funding_archive_conflict_count"] == 1
    assert saved["funding"][0].rate == FundingRatePoint(
        timestamp=started,
        rate="0.0001",
    ).rate
    manifest = saved["dataset"].acquisition_manifest
    assert manifest["external_source_evidence_kind"] == "okx_official_monthly_funding_v1"
    assert len(manifest["external_source_evidence_sha256"]) == 64
    assert manifest["funding_archive_precedence"] == "official_settled_archive_over_rest"


def test_batch_upgrade_records_missing_dataset_without_aborting(monkeypatch):
    class FakeStore:
        def load_research_dataset(self, _dataset_id):
            return None

    monkeypatch.setattr(upgrade_script.api, "store", FakeStore())
    monkeypatch.setattr(
        upgrade_script.api,
        "configuration_service",
        SimpleNamespace(current_configuration=lambda: SimpleNamespace(network_proxy_url="")),
    )

    reports = asyncio.run(upgrade_script.upgrade_many(["ds_missing"]))

    assert len(reports) == 1
    assert reports[0]["upgrade_complete"] is False
    assert reports[0]["error"]["error_type"] == "LookupError"
