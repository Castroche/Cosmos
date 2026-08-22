import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from cosmos_quant.demo import demo_candles
from cosmos_quant.domain import (
    BasisPoint,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from cosmos_quant.research import (
    BacktestJob,
    ResearchJobStatus,
    build_research_dataset,
    verify_research_dataset,
)
from cosmos_quant.storage import SqliteStore


def test_research_dataset_is_immutable_and_reloadable(tmp_path):
    candles = demo_candles(40)
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store = SqliteStore(tmp_path / "research.db")

    store.save_research_dataset(dataset, candles)
    store.save_research_dataset(dataset, candles)

    restored = store.load_research_dataset(dataset.id)
    restored_candles = store.load_research_dataset_candles(dataset.id)
    assert restored is not None
    assert restored["data_fingerprint"] == dataset.data_fingerprint
    assert len(restored_candles) == len(candles)
    assert restored_candles[0]["timestamp"] == candles[0].timestamp.isoformat()
    assert len(store.list_research_datasets()) == 1


def test_research_fingerprint_includes_market_identity():
    candles = demo_candles(40)
    hourly = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    four_hour = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="4h",
    )

    assert hourly.id != four_hour.id
    assert hourly.data_fingerprint != four_hour.data_fingerprint


def test_blind_dataset_role_and_split_contract_are_fingerprint_bound():
    candles = demo_candles(40)
    boundary = candles[30].timestamp
    development = build_research_dataset(
        candles[:25],
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        research_role="development",
        source_dataset_id="ds_source",
        blind_set_id="blindset_test",
        split_boundary=boundary,
        embargo_bars=5,
    )
    blind = build_research_dataset(
        candles[30:],
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        research_role="blind",
        source_dataset_id="ds_source",
        blind_set_id="blindset_test",
        split_boundary=boundary,
        embargo_bars=5,
        blind_sealed=True,
    )

    assert development.id != blind.id
    assert verify_research_dataset(development, candles[:25])["verified"]
    assert verify_research_dataset(blind, candles[30:])["verified"]
    tampered = blind.model_copy(update={"research_role": "development"})
    verification = verify_research_dataset(tampered, candles[30:])
    assert not verification["verified"]
    assert not verification["fingerprint_provenance_matches"]


def test_research_dataset_persists_funding_as_part_of_identity(tmp_path):
    candles = demo_candles(40)
    funding = [
        FundingRatePoint(
            timestamp=candles[8].timestamp,
            rate=Decimal("0.0001"),
            mark_price=candles[8].close,
        )
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        funding_rates=funding,
    )
    without_funding = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store = SqliteStore(tmp_path / "funding.db")

    store.save_research_dataset(dataset, candles, funding)

    assert dataset.id != without_funding.id
    assert dataset.funding_status == "recorded"
    assert store.load_research_dataset_funding(dataset.id)[0]["rate"] == "0.0001"


def test_external_archive_evidence_manifest_is_integrity_checked():
    candles = demo_candles(40)
    evidence = json.dumps(
        [{"archive_sha256": "a" * 64, "filename": "official.zip"}],
        sort_keys=True,
        separators=(",", ":"),
    )
    dataset = build_research_dataset(
        candles,
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    manifest = {
        **dataset.acquisition_manifest,
        "external_source_evidence_json": evidence,
        "external_source_evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
    }
    dataset = dataset.model_copy(update={"acquisition_manifest": manifest})

    assert verify_research_dataset(dataset, candles)["verified"]
    tampered = dataset.model_copy(
        update={
            "acquisition_manifest": {
                **manifest,
                "external_source_evidence_json": evidence.replace("official.zip", "other.zip"),
            }
        }
    )
    verification = verify_research_dataset(tampered, candles)
    assert not verification["verified"]
    assert not verification["external_source_evidence_matches"]


def test_external_archive_evidence_is_bound_to_dataset_fingerprint():
    candles = demo_candles(40)
    evidence = [{"archive_sha256": "a" * 64, "manifest_id": "official_manifest"}]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        external_source_evidence=evidence,
    )

    assert dataset.acquisition_manifest["external_evidence_bound_to_fingerprint"] is True
    assert verify_research_dataset(dataset, candles)["verified"]
    altered = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        external_source_evidence=[
            {"archive_sha256": "b" * 64, "manifest_id": "official_manifest"}
        ],
    )
    assert altered.id != dataset.id

    tampered_manifest = {
        **dataset.acquisition_manifest,
        "external_source_evidence_sha256": "b" * 64,
    }
    tampered = dataset.model_copy(
        update={"acquisition_manifest": tampered_manifest}
    )
    verification = verify_research_dataset(tampered, candles)
    assert not verification["verified"]
    assert not verification["fingerprint_provenance_matches"]


def test_research_dataset_binds_and_persists_open_interest(tmp_path):
    candles = demo_candles(40)
    open_interest = [
        OpenInterestPoint(
            timestamp=candle.timestamp,
            open_interest_amount=Decimal(1000 + index),
            open_interest_value=Decimal(50_000_000 + index),
        )
        for index, candle in enumerate(candles)
    ]
    dataset = build_research_dataset(
        candles,
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        open_interest=open_interest,
    )
    without_open_interest = build_research_dataset(
        candles,
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store = SqliteStore(tmp_path / "open-interest.db")

    store.save_research_dataset(
        dataset,
        candles,
        [],
        open_interest,
    )

    assert dataset.id != without_open_interest.id
    assert dataset.open_interest_count == len(candles)
    assert dataset.open_interest_coverage_ratio == Decimal("1.0000")
    assert (
        store.load_research_dataset_open_interest(dataset.id)[-1]["open_interest_amount"] == "1039"
    )
    assert verify_research_dataset(
        dataset,
        candles,
        [],
        open_interest,
    )["verified"]
    assert not verify_research_dataset(dataset, candles, [], [])["verified"]


def test_research_dataset_binds_and_persists_positioning(tmp_path):
    candles = demo_candles(40)
    positioning = [
        PositioningPoint(
            timestamp=candle.timestamp,
            long_short_ratio=Decimal("1.5"),
            long_account_fraction=Decimal("0.6"),
            short_account_fraction=Decimal("0.4"),
        )
        for candle in candles
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        positioning=positioning,
    )
    without_positioning = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
    )
    store = SqliteStore(tmp_path / "positioning.db")

    store.save_research_dataset(dataset, candles, [], [], positioning)

    restored = store.load_research_dataset_positioning(dataset.id)
    assert dataset.id != without_positioning.id
    assert dataset.positioning_count == len(candles)
    assert dataset.positioning_coverage_ratio == Decimal("1.0000")
    assert restored[-1]["long_short_ratio"] == "1.5"
    assert verify_research_dataset(
        dataset,
        candles,
        [],
        [],
        positioning,
    )["verified"]
    assert not verify_research_dataset(dataset, candles, [], [], [])["verified"]


def test_research_dataset_rejects_future_positioning_in_verification():
    candles = demo_candles(40)
    positioning = [
        PositioningPoint(
            timestamp=candles[-1].timestamp + timedelta(hours=1),
            long_short_ratio=Decimal("1.2"),
        )
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        positioning=positioning,
    )

    verification = verify_research_dataset(dataset, candles, [], [], positioning)

    assert not verification["verified"]
    assert not verification["positioning_causal"]


def test_research_dataset_binds_and_persists_basis(tmp_path):
    candles = demo_candles(40)
    basis = [
        BasisPoint(
            timestamp=candle.timestamp,
            mark_price=candle.close + Decimal("1"),
            index_price=candle.close,
            basis_rate=Decimal("1") / candle.close,
        )
        for candle in candles
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        basis=basis,
    )
    store = SqliteStore(tmp_path / "basis.db")

    store.save_research_dataset(dataset, candles, [], [], [], basis)

    restored = [
        BasisPoint.model_validate(item) for item in store.load_research_dataset_basis(dataset.id)
    ]
    assert len(restored) == len(candles)
    assert dataset.basis_coverage_ratio == Decimal("1.0000")
    assert verify_research_dataset(dataset, candles, [], [], [], restored)["verified"]
    assert not verify_research_dataset(dataset, candles, [], [], [], [])["verified"]


def test_research_dataset_binds_and_persists_order_flow(tmp_path):
    candles = demo_candles(40)
    order_flow = [
        OrderFlowBar(
            timestamp=candle.timestamp,
            aggregate_count=10,
            underlying_trade_count=20,
            base_volume=Decimal("5"),
            quote_volume=Decimal("500"),
            taker_buy_base_volume=Decimal("3"),
            taker_sell_base_volume=Decimal("2"),
            delta_base_volume=Decimal("1"),
            maximum_trade_quote=Decimal("100"),
        )
        for candle in candles
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        order_flow=order_flow,
    )
    store = SqliteStore(tmp_path / "order-flow.db")

    store.save_research_dataset(dataset, candles, [], [], [], [], order_flow)

    restored = [
        OrderFlowBar.model_validate(item)
        for item in store.load_research_dataset_order_flow(dataset.id)
    ]
    assert len(restored) == len(candles)
    assert dataset.order_flow_coverage_ratio == Decimal("1.0000")
    assert verify_research_dataset(
        dataset,
        candles,
        [],
        [],
        [],
        [],
        restored,
    )["verified"]
    assert not verify_research_dataset(dataset, candles, [], [], [], [], [])["verified"]


def test_research_dataset_binds_persists_and_rejects_future_liquidation_flow(tmp_path):
    candles = demo_candles(40)
    flow = [
        LiquidationFlowPoint(
            timestamp=candle.timestamp,
            interval_start=candle.timestamp - timedelta(hours=1),
            long_liquidated_contracts=Decimal(index + 1),
            short_liquidated_contracts=Decimal(index + 2),
            unique_event_count=index + 1,
            source_instrument_id="BTCUSD_PERP",
        )
        for index, candle in enumerate(candles)
    ]
    dataset = build_research_dataset(
        candles,
        exchange="binance",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        liquidation_flow=flow,
    )
    store = SqliteStore(tmp_path / "liquidation-flow.db")
    store.save_research_dataset(dataset, candles, [], [], [], [], [], flow)

    restored = [
        LiquidationFlowPoint.model_validate(item)
        for item in store.load_research_dataset_liquidation_flow(dataset.id)
    ]
    assert dataset.liquidation_flow_count == 40
    assert dataset.liquidation_flow_coverage_ratio == Decimal("1.0000")
    assert verify_research_dataset(
        dataset,
        candles,
        [],
        [],
        [],
        [],
        [],
        restored,
    )["verified"]
    assert not verify_research_dataset(dataset, candles, [], [], [], [], [], [])["verified"]
    future = restored[-1].model_copy(
        update={
            "timestamp": candles[-1].timestamp + timedelta(hours=1),
            "interval_start": candles[-1].timestamp,
        }
    )
    verification = verify_research_dataset(
        dataset,
        candles,
        [],
        [],
        [],
        [],
        [],
        [*restored[:-1], future],
    )
    assert not verification["verified"]
    assert not verification["liquidation_flow_causal"]


def test_research_job_state_is_durable(tmp_path):
    store = SqliteStore(tmp_path / "jobs.db")
    job = BacktestJob(id="btj_test", plan_id="stg_test")

    store.save_research_job(job)
    job.status = ResearchJobStatus.RUNNING
    store.save_research_job(job)

    restored = BacktestJob.model_validate(store.load_research_job(job.id))
    assert restored.status == ResearchJobStatus.RUNNING
    assert store.list_research_jobs()[0]["id"] == job.id
