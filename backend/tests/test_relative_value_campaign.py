import asyncio
import math
from decimal import Decimal

import pytest
from app_fixture import services as api
from cosmos_quant.api_contracts import RelativeValueCampaignRequest
from cosmos_quant.demo import demo_candles
from cosmos_quant.multi_leg_execution import validate_multi_leg_execution_certificate
from cosmos_quant.relative_value import PAIR_STRATEGY_LIBRARY
from cosmos_quant.relative_value_campaign import (
    PAIR_CAMPAIGN_ENGINE_VERSION,
    PairMarketData,
    generate_pair_failure_revisions,
    pair_strategy_id,
    run_pair_research_campaign,
)
from cosmos_quant.research import build_research_dataset
from cosmos_quant.storage import SqliteStore
from fastapi import HTTPException


def _scaled_series(base, *, scale: str, phase: float, amplitude: float):
    result = []
    quantum = Decimal("0.00000001")
    for index, candle in enumerate(base):
        factor = Decimal(str(1 + amplitude * math.sin(index / 12 + phase)))
        multiplier = Decimal(scale) * factor
        result.append(
            candle.model_copy(
                update={
                    "open": (candle.open * multiplier).quantize(quantum),
                    "high": (candle.high * multiplier).quantize(quantum),
                    "low": (candle.low * multiplier).quantize(quantum),
                    "close": (candle.close * multiplier).quantize(quantum),
                }
            )
        )
    return result


def _campaign_markets():
    base = demo_candles(800)
    markets = []
    for exchange, phase_shift in (("alpha", 0.0), ("beta", 0.3)):
        series = {
            "BTC/USDT": _scaled_series(
                base,
                scale="1",
                phase=phase_shift,
                amplitude=0.04,
            ),
            "ETH/USDT": _scaled_series(
                base,
                scale="0.05",
                phase=phase_shift + 0.8,
                amplitude=0.025,
            ),
            "SOL/USDT": _scaled_series(
                base,
                scale="0.002",
                phase=phase_shift + 1.6,
                amplitude=0.035,
            ),
        }
        datasets = {
            symbol: build_research_dataset(
                candles,
                exchange=exchange,
                market_type="spot",
                symbol=symbol,
                timeframe="1h",
            )
            for symbol, candles in series.items()
        }
        for symbol_a, symbol_b in (
            ("BTC/USDT", "ETH/USDT"),
            ("BTC/USDT", "SOL/USDT"),
            ("ETH/USDT", "SOL/USDT"),
        ):
            markets.append(
                PairMarketData(
                    dataset_a=datasets[symbol_a],
                    candles_a=series[symbol_a],
                    funding_a=[],
                    dataset_b=datasets[symbol_b],
                    candles_b=series[symbol_b],
                    funding_b=[],
                )
            )
    return markets


def _sealed_pair_splits():
    originals = {}
    for market in _campaign_markets():
        originals[market.dataset_a.id] = (market.dataset_a, market.candles_a)
        originals[market.dataset_b.id] = (market.dataset_b, market.candles_b)
    development = []
    blind = []
    for source, candles in originals.values():
        boundary = candles[600].timestamp
        common = {
            "exchange": source.exchange,
            "market_type": source.market_type,
            "symbol": source.symbol,
            "timeframe": source.timeframe,
            "source_dataset_id": source.id,
            "blind_set_id": "blindset_pair_once",
            "split_boundary": boundary,
            "embargo_bars": 50,
        }
        development.append(
            (
                build_research_dataset(
                    candles[:550],
                    research_role="development",
                    blind_sealed=False,
                    **common,
                ),
                candles[:550],
            )
        )
        blind.append(
            (
                build_research_dataset(
                    candles[600:],
                    research_role="blind",
                    blind_sealed=True,
                    **common,
                ),
                candles[600:],
            )
        )
    return development, blind


def test_pair_campaign_runs_independent_models_across_pairs_and_exchanges():
    markets = _campaign_markets()

    report = run_pair_research_campaign(markets)

    assert report.engine_version == PAIR_CAMPAIGN_ENGINE_VERSION
    assert report.formation_fraction == Decimal("0.25")
    assert report.strict_formation_trading_separation is True
    assert len(report.cells) == len(PAIR_STRATEGY_LIBRARY) * 6
    assert len(report.candidates) == len(PAIR_STRATEGY_LIBRARY)
    assert report.pbo.candidate_count == len(PAIR_STRATEGY_LIBRARY)
    assert {item.cell.archetype for item in report.cells} == {
        "log_spread_equal_dollar",
        "rolling_beta_residual",
        "relative_momentum_equal_dollar",
    }
    assert all(candidate.market_count == 6 for candidate in report.candidates)
    assert all(candidate.unique_symbol_count == 3 for candidate in report.candidates)
    assert all(candidate.unique_exchange_count == 2 for candidate in report.candidates)
    assert all(item.stress_return_percent <= item.base_return_percent for item in report.cells)
    assert report.independent_blind_test_completed is False
    assert report.execution_eligible is False
    assert report.profitability_claim is False
    assert any("密封盲测" in reason for reason in report.selection_reasons)
    assert any("双腿原子下单" in reason for reason in report.selection_reasons)
    if report.provisional_selected_strategy_id is None:
        assert report.failure_attribution is not None
        assert 1 <= len(report.recommended_revisions) <= 3
        assert all(
            revision.parent_strategy_fingerprint
            in {candidate.strategy_fingerprint for candidate in report.candidates}
            for revision in report.recommended_revisions
        )


def test_pair_failure_revisions_are_bounded_single_variable_new_hypotheses():
    report = run_pair_research_campaign(_campaign_markets())
    parent = report.candidates[0]
    parent_spec = next(
        spec for spec in PAIR_STRATEGY_LIBRARY if spec.fingerprint == parent.strategy_fingerprint
    )

    attribution, revisions = generate_pair_failure_revisions(parent, parent_spec)

    assert attribution.codes
    assert 1 <= len(revisions) <= 3
    assert len({item.strategy_spec.fingerprint for item in revisions}) == len(revisions)
    assert all(item.parent_strategy_fingerprint == parent_spec.fingerprint for item in revisions)
    assert all(len(item.changed_parameters) == 1 for item in revisions)
    assert all(item.strategy_spec.fingerprint != parent_spec.fingerprint for item in revisions)


def test_pair_campaign_rejects_cross_exchange_legs_in_one_market_cell():
    market = _campaign_markets()[0]
    invalid = PairMarketData(
        dataset_a=market.dataset_a,
        candles_a=market.candles_a,
        funding_a=[],
        dataset_b=market.dataset_b.model_copy(update={"exchange": "other"}),
        candles_b=market.candles_b,
        funding_b=[],
    )

    with pytest.raises(ValueError, match="one exchange"):
        run_pair_research_campaign([invalid])


def test_pair_campaign_freezes_custom_formation_fraction_out_of_sample():
    reports = []
    report = run_pair_research_campaign(
        [_campaign_markets()[0]],
        strategy_specs=[PAIR_STRATEGY_LIBRARY[0]],
        formation_fraction=Decimal("0.50"),
        fold_count=4,
        report_sink=reports.append,
        generate_failure_revisions=False,
    )

    assert report.formation_fraction == Decimal("0.50")
    assert report.strict_formation_trading_separation is True
    assert len(reports) == 2
    assert all(item.formation_bars == 400 for item in reports)
    assert all(item.evaluation_bars == 400 for item in reports)


def test_pair_campaign_persists_its_complete_research_evidence(tmp_path):
    store = SqliteStore(tmp_path / "pair-campaign.db")
    reports = []
    report = run_pair_research_campaign(
        _campaign_markets(),
        strategy_specs=list(PAIR_STRATEGY_LIBRARY[:2]),
        report_sink=reports.append,
    )

    assert len(reports) == len(report.cells) * 2
    for backtest in reports:
        store.save_relative_value_report(backtest)
    store.save_relative_value_campaign(report)

    restored = store.load_relative_value_campaign(report.id)
    assert restored is not None
    assert restored["pbo"]["candidate_count"] == 2
    assert len(store.list_relative_value_campaigns()) == 1
    assert all(store.load_relative_value_report(item.base_report_id) for item in report.cells)
    assert all(store.load_relative_value_report(item.stress_report_id) for item in report.cells)
    assert all(
        store.load_relative_value_report(item.base_report_id)["formation_bars"] >= 200
        for item in report.cells
    )


def test_relative_value_campaign_api_builds_pairs_and_persists_report(
    tmp_path,
    monkeypatch,
):
    store = SqliteStore(tmp_path / "pair-campaign-api.db")
    monkeypatch.setattr(api, "store", store)
    markets = _campaign_markets()
    unique = {}
    for market in markets:
        unique[market.dataset_a.id] = (market.dataset_a, market.candles_a)
        unique[market.dataset_b.id] = (market.dataset_b, market.candles_b)
    for dataset, candles in unique.values():
        store.save_research_dataset(dataset, candles)

    report = asyncio.run(
        api.run_relative_value_campaign(
            RelativeValueCampaignRequest(
                dataset_ids=list(unique),
                strategy_specs=[PAIR_STRATEGY_LIBRARY[0].model_dump(mode="json")],
            )
        )
    )

    assert len(report.research_pair_ids) == 6
    assert len(report.cells) == 6
    assert report.strategy_fingerprints == [PAIR_STRATEGY_LIBRARY[0].fingerprint]
    assert report.research_lineage_id.startswith("pair_lineage_")
    assert report.cumulative_hypothesis_count == 1
    assert report.sequential_experiment_count == 1
    assert store.load_relative_value_campaign(report.id) is not None
    restored = asyncio.run(api.get_relative_value_campaign(report.id))
    assert restored.id == report.id
    listed = asyncio.run(api.list_relative_value_campaigns())
    assert [item.id for item in listed] == [report.id]

    assert report.recommended_revisions
    successor_specs = [
        PAIR_STRATEGY_LIBRARY[0].model_dump(mode="json"),
        report.recommended_revisions[0].strategy_spec.model_dump(mode="json"),
    ]
    successor = asyncio.run(
        api.run_relative_value_campaign(
            RelativeValueCampaignRequest(
                dataset_ids=list(unique),
                strategy_specs=successor_specs,
                parent_campaign_id=report.id,
            )
        )
    )
    assert successor.parent_campaign_id == report.id
    assert successor.research_lineage_id == report.research_lineage_id
    assert successor.cumulative_hypothesis_count == 2
    assert successor.sequential_experiment_count == 2
    assert successor.effective_trial_count == 3
    assert successor.experiment_kind == "bounded_revision"
    assert successor.recommended_revisions == []
    with pytest.raises(HTTPException, match="only one successor"):
        asyncio.run(
            api.run_relative_value_campaign(
                RelativeValueCampaignRequest(
                    dataset_ids=list(unique),
                    strategy_specs=successor_specs,
                    parent_campaign_id=successor.id,
                )
            )
        )

    extension = asyncio.run(
        api.run_relative_value_campaign(
            RelativeValueCampaignRequest(
                dataset_ids=list(unique),
                strategy_specs=[PAIR_STRATEGY_LIBRARY[-1].model_dump(mode="json")],
            )
        )
    )
    assert extension.parent_campaign_id is None
    assert extension.predecessor_campaign_id == successor.id
    assert extension.experiment_kind == "structural_extension"
    assert extension.cumulative_hypothesis_count == 3
    assert extension.sequential_experiment_count == 3
    assert extension.effective_trial_count == 5
    assert extension.new_trial_variant_count == 1

    replay = asyncio.run(
        api.run_relative_value_campaign(
            RelativeValueCampaignRequest(
                dataset_ids=list(unique),
                strategy_specs=[PAIR_STRATEGY_LIBRARY[-1].model_dump(mode="json")],
            )
        )
    )
    assert replay.id == extension.id
    experiments = store.list_research_campaign_experiments(extension.research_lineage_id)
    assert len(experiments) == 3


def test_pair_campaign_opens_one_sealed_blind_set_once_without_disclosing_metrics(
    tmp_path,
    monkeypatch,
):
    store = SqliteStore(tmp_path / "pair-blind.db")
    monkeypatch.setattr(api, "store", store)
    development, blind = _sealed_pair_splits()
    for dataset, candles in [*development, *blind]:
        store.save_research_dataset(dataset, candles)

    real_runner = api.run_pair_research_campaign
    calls = []

    def controlled_runner(markets, **kwargs):
        roles = {market.dataset_a.research_role for market in markets}
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
                    "provisional_selected_strategy_id": pair_strategy_id(
                        kwargs["strategy_specs"][0]
                    )
                }
            )
        return result

    monkeypatch.setattr(api, "run_pair_research_campaign", controlled_runner)
    request = RelativeValueCampaignRequest(
        dataset_ids=[dataset.id for dataset, _ in development],
        blind_dataset_ids=[dataset.id for dataset, _ in blind],
        strategy_specs=[PAIR_STRATEGY_LIBRARY[0].model_dump(mode="json")],
    )

    first = asyncio.run(api.run_relative_value_campaign(request))
    replay = asyncio.run(api.run_relative_value_campaign(request))

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
    assert len(store.list_relative_value_reports(100)) == 12
    consumption = store.load_blind_set_consumption("blindset_pair_once")
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
    with pytest.raises(HTTPException, match="sealed after blind-set opening"):
        asyncio.run(
            api.run_relative_value_campaign(
                request.model_copy(
                    update={"strategy_specs": [PAIR_STRATEGY_LIBRARY[1].model_dump(mode="json")]}
                )
            )
        )


def test_pair_blind_scope_rejects_source_mismatch_before_opening():
    development, blind = _sealed_pair_splits()
    malformed = [
        blind[0][0].model_copy(update={"source_dataset_id": "unmatched_source"}),
        *(dataset for dataset, _ in blind[1:]),
    ]

    with pytest.raises(ValueError, match="matching split per source"):
        api._validate_sealed_blind_scope(
            [dataset for dataset, _ in development],
            malformed,
        )


def test_relative_value_campaign_api_never_uses_sealed_blind_data_for_selection(
    tmp_path,
    monkeypatch,
):
    store = SqliteStore(tmp_path / "pair-campaign-blind.db")
    monkeypatch.setattr(api, "store", store)
    markets = _campaign_markets()
    unique = {}
    for market in markets:
        unique[market.dataset_a.id] = (market.dataset_a, market.candles_a)
        unique[market.dataset_b.id] = (market.dataset_b, market.candles_b)
    first_id = next(iter(unique))
    for dataset_id, (dataset, candles) in unique.items():
        if dataset_id == first_id:
            dataset = dataset.model_copy(
                update={
                    "research_role": "blind",
                    "blind_set_id": "blindset_pair_test",
                    "blind_sealed": True,
                }
            )
        store.save_research_dataset(dataset, candles)

    with pytest.raises(HTTPException, match="sealed blind datasets"):
        asyncio.run(
            api.run_relative_value_campaign(
                RelativeValueCampaignRequest(dataset_ids=list(unique))
            )
        )
