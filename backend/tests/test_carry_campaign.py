import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant import api
from cosmos_quant.carry import CARRY_STRATEGY_LIBRARY
from cosmos_quant.carry_campaign import (
    CARRY_CAMPAIGN_ENGINE_VERSION,
    CarryMarketData,
    carry_strategy_id,
    generate_carry_failure_revisions,
    run_carry_research_campaign,
)
from cosmos_quant.domain import Candle, FundingRatePoint
from cosmos_quant.multi_leg_execution import validate_multi_leg_execution_certificate
from cosmos_quant.research import build_research_dataset
from cosmos_quant.storage import SqliteStore


def _markets():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    quantum = Decimal("0.00000001")
    markets = []
    for exchange_index, exchange in enumerate(("alpha", "beta")):
        for symbol_index, symbol in enumerate(("BTC/USDT", "ETH/USDT", "SOL/USDT")):
            spot = []
            perpetual = []
            for index in range(1000):
                timestamp = start + timedelta(hours=index)
                base = Decimal(100 + symbol_index * 20) + Decimal(index) * Decimal("0.003")
                phase = (index + exchange_index * 7 + symbol_index * 11) % 100
                if phase < 20 or phase >= 75:
                    basis = Decimal("0.0001")
                elif phase < 50:
                    basis = Decimal("0.006")
                else:
                    basis = Decimal("0.006") * Decimal(75 - phase) / Decimal(25)
                perp = (base * (Decimal("1") + basis)).quantize(quantum)
                spot.append(
                    Candle(
                        timestamp=timestamp,
                        open=base,
                        high=(base * Decimal("1.001")).quantize(quantum),
                        low=(base * Decimal("0.999")).quantize(quantum),
                        close=base,
                        volume=Decimal("1000"),
                    )
                )
                perpetual.append(
                    Candle(
                        timestamp=timestamp,
                        open=perp,
                        high=(perp * Decimal("1.001")).quantize(quantum),
                        low=(perp * Decimal("0.999")).quantize(quantum),
                        close=perp,
                        volume=Decimal("1000"),
                    )
                )
            funding = [
                FundingRatePoint(
                    timestamp=start + timedelta(hours=index),
                    rate=Decimal("0.0001"),
                )
                for index in range(0, 1000, 8)
            ]
            spot_dataset = build_research_dataset(
                spot,
                exchange=exchange,
                market_type="spot",
                symbol=symbol,
                timeframe="1h",
            )
            perpetual_dataset = build_research_dataset(
                perpetual,
                exchange=exchange,
                market_type="swap",
                symbol=symbol,
                timeframe="1h",
                funding_rates=funding,
            )
            markets.append(
                CarryMarketData(
                    spot_dataset=spot_dataset,
                    spot_candles=spot,
                    perpetual_dataset=perpetual_dataset,
                    perpetual_candles=perpetual,
                    funding_rates=funding,
                )
            )
    return markets


def _sealed_carry_splits():
    development = []
    blind = []
    for market in _markets():
        boundary = market.spot_candles[750].timestamp
        for source, candles, funding in (
            (market.spot_dataset, market.spot_candles, []),
            (market.perpetual_dataset, market.perpetual_candles, market.funding_rates),
        ):
            common = {
                "exchange": source.exchange,
                "market_type": source.market_type,
                "symbol": source.symbol,
                "timeframe": source.timeframe,
                "source_dataset_id": source.id,
                "blind_set_id": "blindset_carry_once",
                "split_boundary": boundary,
                "embargo_bars": 50,
            }
            development_funding = [
                item for item in funding if item.timestamp <= candles[699].timestamp
            ]
            blind_funding = [item for item in funding if item.timestamp >= candles[750].timestamp]
            development.append(
                (
                    build_research_dataset(
                        candles[:700],
                        funding_rates=development_funding,
                        research_role="development",
                        blind_sealed=False,
                        **common,
                    ),
                    candles[:700],
                    development_funding,
                )
            )
            blind.append(
                (
                    build_research_dataset(
                        candles[750:],
                        funding_rates=blind_funding,
                        research_role="blind",
                        blind_sealed=True,
                        **common,
                    ),
                    candles[750:],
                    blind_funding,
                )
            )
    return development, blind


def test_carry_campaign_runs_public_hypotheses_across_exchanges_and_symbols():
    reports = []
    report = run_carry_research_campaign(_markets(), report_sink=reports.append)

    assert report.engine_version == CARRY_CAMPAIGN_ENGINE_VERSION
    assert report.strict_formation_trading_separation is True
    assert report.formation_fraction == Decimal("0.25")
    assert len(report.cells) == len(CARRY_STRATEGY_LIBRARY) * 6
    assert len(report.candidates) == len(CARRY_STRATEGY_LIBRARY)
    assert report.pbo.candidate_count == len(CARRY_STRATEGY_LIBRARY)
    assert len(reports) == len(report.cells) * 2
    assert all(item.formation_bars == 250 for item in reports)
    assert all(item.evaluation_bars == 750 for item in reports)
    assert all(item.stress_return_percent <= item.base_return_percent for item in report.cells)
    assert all(candidate.unique_exchange_count == 2 for candidate in report.candidates)
    assert all(candidate.unique_symbol_count == 3 for candidate in report.candidates)
    assert report.independent_blind_test_completed is False
    assert report.execution_eligible is False
    assert report.profitability_claim is False
    assert report.failure_attribution is not None
    assert report.recommended_revisions
    assert all(len(revision.changed_parameters) == 1 for revision in report.recommended_revisions)
    assert all(
        revision.strategy_spec.entry_basis_rate >= CARRY_STRATEGY_LIBRARY[0].entry_basis_rate
        for revision in report.recommended_revisions
    )


def test_zero_opportunity_carry_failure_requires_new_data_not_weaker_thresholds():
    campaign = run_carry_research_campaign(_markets())
    zero_trade_candidate = campaign.candidates[0].model_copy(
        update={
            "total_trades": 0,
            "median_win_rate_percent": Decimal("0"),
            "median_payoff_ratio": Decimal("0"),
            "median_profit_factor": Decimal("0"),
        }
    )

    attribution, revisions = generate_carry_failure_revisions(
        zero_trade_candidate,
        CARRY_STRATEGY_LIBRARY[0],
    )

    assert attribution.requires_new_data is True
    assert "no_confirmed_entry_opportunity" in attribution.codes
    assert revisions == []


def test_carry_campaign_rejects_duplicate_market_cells():
    market = _markets()[0]

    try:
        run_carry_research_campaign([market, market])
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate carry markets must fail")


def test_carry_campaign_and_reports_persist_separately(tmp_path):
    store = SqliteStore(tmp_path / "carry.db")
    reports = []
    campaign = run_carry_research_campaign(
        _markets(),
        strategy_specs=[CARRY_STRATEGY_LIBRARY[0]],
        report_sink=reports.append,
    )

    for report in reports:
        store.save_carry_report(report)
    store.save_carry_campaign(campaign)

    assert len(store.list_carry_reports()) == 12
    assert store.load_carry_campaign(campaign.id)["trial_count"] == 6
    assert [item["id"] for item in store.list_carry_campaigns()] == [campaign.id]


def test_carry_campaign_api_builds_exact_spot_swap_markets_and_replays(
    tmp_path,
    monkeypatch,
):
    store = SqliteStore(tmp_path / "carry-api.db")
    monkeypatch.setattr(api, "store", store)
    dataset_ids = []
    for market in _markets():
        store.save_research_dataset(market.spot_dataset, market.spot_candles)
        store.save_research_dataset(
            market.perpetual_dataset,
            market.perpetual_candles,
            market.funding_rates,
        )
        dataset_ids.extend([market.spot_dataset.id, market.perpetual_dataset.id])
    request = api.CarryCampaignRequest(
        dataset_ids=dataset_ids,
        strategy_specs=[CARRY_STRATEGY_LIBRARY[0].model_dump(mode="json")],
    )

    first = asyncio.run(api.run_carry_campaign(request))
    replay = asyncio.run(api.run_carry_campaign(request))

    assert first.id == replay.id
    assert len(first.research_market_ids) == 6
    assert first.trial_count == 6
    assert first.research_lineage_id.startswith("carry_lineage_")
    assert first.execution_eligible is False
    assert store.load_carry_campaign(first.id) is not None
    assert asyncio.run(api.get_carry_campaign(first.id)).id == first.id

    weakened = CARRY_STRATEGY_LIBRARY[0].model_copy(update={"entry_basis_rate": Decimal("0.0005")})
    with pytest.raises(api.HTTPException, match="child not frozen"):
        asyncio.run(
            api.run_carry_campaign(
                request.model_copy(
                    update={
                        "strategy_specs": [
                            CARRY_STRATEGY_LIBRARY[0].model_dump(mode="json"),
                            weakened.model_dump(mode="json"),
                        ],
                        "parent_campaign_id": first.id,
                    }
                )
            )
        )

    child_specs = [
        CARRY_STRATEGY_LIBRARY[0],
        *(item.strategy_spec for item in first.recommended_revisions),
    ]
    successor = asyncio.run(
        api.run_carry_campaign(
            request.model_copy(
                update={
                    "strategy_specs": [item.model_dump(mode="json") for item in child_specs],
                    "parent_campaign_id": first.id,
                }
            )
        )
    )
    assert successor.parent_campaign_id == first.id
    assert successor.experiment_kind == "bounded_revision"
    assert successor.cumulative_hypothesis_count > first.cumulative_hypothesis_count
    assert successor.effective_trial_count >= successor.cumulative_hypothesis_count
    assert successor.recommended_revisions == []
    with pytest.raises(api.HTTPException, match="only one successor"):
        asyncio.run(
            api.run_carry_campaign(
                request.model_copy(
                    update={
                        "strategy_specs": [item.model_dump(mode="json") for item in child_specs],
                        "parent_campaign_id": successor.id,
                    }
                )
            )
        )


def test_carry_campaign_opens_one_sealed_blind_set_once_without_metric_leakage(
    tmp_path,
    monkeypatch,
):
    store = SqliteStore(tmp_path / "carry-blind.db")
    monkeypatch.setattr(api, "store", store)
    development, blind = _sealed_carry_splits()
    for dataset, candles, funding in [*development, *blind]:
        store.save_research_dataset(dataset, candles, funding)

    real_runner = api.run_carry_research_campaign
    calls = []

    def controlled_runner(markets, **kwargs):
        roles = {market.spot_dataset.research_role for market in markets}
        calls.append((roles, len(kwargs["strategy_specs"])))
        result = real_runner(markets, **kwargs)
        result = result.model_copy(
            update={
                "candidates": [
                    result.candidates[0].model_copy(
                        update={"cross_market_passed": True}
                    )
                ],
                "pbo": result.pbo.model_copy(
                    update={
                        "passed": True,
                        "upper_confidence_bound": Decimal("0.10"),
                        "reasons": [],
                    }
                ),
            }
        )
        if roles == {"development"}:
            result = result.model_copy(
                update={
                    "provisional_selected_strategy_id": carry_strategy_id(
                        kwargs["strategy_specs"][0]
                    )
                }
            )
        return result

    monkeypatch.setattr(api, "run_carry_research_campaign", controlled_runner)
    request = api.CarryCampaignRequest(
        dataset_ids=[dataset.id for dataset, _, _ in development],
        blind_dataset_ids=[dataset.id for dataset, _, _ in blind],
        strategy_specs=[CARRY_STRATEGY_LIBRARY[0].model_dump(mode="json")],
    )

    first = asyncio.run(api.run_carry_campaign(request))
    replay = asyncio.run(api.run_carry_campaign(request))

    assert replay.id == first.id
    assert calls == [({"development"}, 1), ({"blind"}, 1)]
    assert first.independent_blind_test_completed is True
    assert first.blind_consumption_id
    assert first.blind_evidence_fingerprint
    assert first.blind_market_count == 6
    assert first.execution_eligible is True
    assert first.execution_certificate is not None
    validate_multi_leg_execution_certificate(first.execution_certificate)
    assert len(first.cells) == 6
    assert len(store.list_carry_reports(100)) == 12
    consumption = store.load_blind_set_consumption("blindset_carry_once")
    assert consumption["status"] == "completed"
    assert consumption["campaign_id"] == first.id
    public_payload = first.model_dump(mode="json")
    assert "blind_candidate" not in public_payload
    assert "blind_dataset_ids" not in public_payload
    assert "blind_evidence_fingerprint" in public_payload["execution_certificate"]
    assert all(
        item["cell"]["dataset_id"] not in request.blind_dataset_ids
        for item in public_payload["cells"]
    )
    with pytest.raises(api.HTTPException, match="sealed after blind-set opening"):
        asyncio.run(
            api.run_carry_campaign(
                request.model_copy(
                    update={"strategy_specs": [CARRY_STRATEGY_LIBRARY[1].model_dump(mode="json")]}
                )
            )
        )


def test_carry_strategy_library_endpoint_is_non_empty_and_not_preapproved():
    strategies = asyncio.run(api.get_carry_strategy_library())

    assert len(strategies) >= 2
    assert all(strategy.model == "spot_perpetual_cash_and_carry" for strategy in strategies)
    assert len({strategy.fingerprint for strategy in strategies}) == len(strategies)
