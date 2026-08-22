import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cosmos_quant import api
from cosmos_quant.backtest import run_walk_forward_backtest
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.demo import demo_candles
from cosmos_quant.research import ResearchDataset
from cosmos_quant.research_lifecycle import (
    CandidateStatus,
    StrategyCandidate,
    attach_backtest,
    compare_for_promotion,
    compile_champion,
    create_candidate,
    validate_compiled_champion,
)
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy_spec import default_strategy_spec


def test_ai_candidate_is_validated_but_never_directly_executable():
    spec = default_strategy_spec(StrategyKind.TREND).model_copy(
        update={"source": "ai", "hypothesis": "研究趋势阈值的稳定性"}
    )
    candidate = create_candidate(spec)

    assert candidate.source == "ai"
    assert candidate.status == CandidateStatus.PROPOSED
    assert candidate.backtest is None


def test_stale_candidate_fingerprint_is_isolated_instead_of_reinterpreted():
    candidate = create_candidate(default_strategy_spec(StrategyKind.TREND))
    stale_payload = candidate.model_dump(mode="json")
    stale_payload["spec"]["parameters"]["ema_fast"] = 12

    parsed = api.parse_strategy_candidate_payload(
        stale_payload,
        audit_context="test",
        emit_audit=False,
    )

    assert parsed is None


def test_research_candidate_list_skips_stale_payloads(monkeypatch):
    valid = create_candidate(default_strategy_spec(StrategyKind.TREND))
    stale_payload = valid.model_dump(mode="json")
    stale_payload["id"] = "cand_stale_contract"
    stale_payload["spec"]["parameters"]["ema_fast"] = 12

    class Store:
        def list_strategy_candidates(self, _limit):
            return [stale_payload, valid.model_dump(mode="json")]

    monkeypatch.setattr(api, "store", Store())
    monkeypatch.setattr(api, "add_audit", lambda *_args, **_kwargs: None)
    api.stale_candidate_audit_ids.clear()

    candidates = asyncio.run(api.research_candidates())

    assert [item.id for item in candidates] == [valid.id]


def test_ai_champion_compiles_only_with_exact_robust_evidence():
    dataset = ResearchDataset(
        id="ds_ai_compile",
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        candle_count=len(demo_candles()),
        data_start=demo_candles()[0].timestamp,
        data_end=demo_candles()[-1].timestamp,
        data_fingerprint="ai_compile_data",
    )
    spec = default_strategy_spec(StrategyKind.TREND).model_copy(
        update={"source": "ai", "hypothesis": "compile validated trend evidence"}
    )
    candidate = create_candidate(spec)
    result = run_walk_forward_backtest(demo_candles()).model_copy(
        update={
            "dataset_id": dataset.id,
            "data_fingerprint": dataset.data_fingerprint,
            "strategy_fingerprint": candidate.spec_fingerprint,
            "robustness_passed": True,
        }
    )
    candidate = attach_backtest(candidate, dataset.id, result)
    candidate.status = CandidateStatus.CHAMPION
    candidate.execution_artifact = compile_champion(
        candidate,
        dataset_id=dataset.id,
        data_fingerprint=dataset.data_fingerprint,
        drift_status="stable",
    )

    validate_compiled_champion(
        candidate,
        candidate.execution_artifact,
        dataset_id=dataset.id,
        data_fingerprint=dataset.data_fingerprint,
    )

    retired = candidate.model_copy(update={"status": CandidateStatus.RETIRED})
    with pytest.raises(ValueError, match="no longer"):
        validate_compiled_champion(
            retired,
            candidate.execution_artifact,
            dataset_id=dataset.id,
            data_fingerprint=dataset.data_fingerprint,
        )

    tampered = candidate.execution_artifact.model_copy(update={"data_fingerprint": "tampered"})
    with pytest.raises(ValueError, match="does not match"):
        validate_compiled_champion(
            candidate,
            tampered,
            dataset_id=dataset.id,
            data_fingerprint=dataset.data_fingerprint,
        )


def test_candidate_requires_robust_dataset_bound_backtest_for_promotion():
    candidate = create_candidate(default_strategy_spec(StrategyKind.TREND))
    result = run_walk_forward_backtest(demo_candles())
    result = result.model_copy(
        update={
            "dataset_id": "ds_fixture",
            "robustness_passed": True,
            "validation_reasons": ["independent fixture gate passed"],
        }
    )
    validated = attach_backtest(candidate, "ds_fixture", result)
    decision = compare_for_promotion(validated, None)

    assert validated.status == CandidateStatus.CHALLENGER
    assert decision.approved is True


def test_candidate_identity_is_immutable_and_champion_switch_is_atomic(tmp_path):
    store = SqliteStore(tmp_path / "lifecycle.db")
    first = create_candidate(default_strategy_spec(StrategyKind.TREND))
    first.status = CandidateStatus.CHALLENGER
    store.save_strategy_candidate(first)
    first.status = CandidateStatus.CHAMPION
    first.updated_at = datetime.now(UTC)
    store.promote_strategy_candidate(first)

    second_spec = default_strategy_spec(StrategyKind.TREND).model_copy(
        update={"hypothesis": "second bounded challenger"}
    )
    second = create_candidate(second_spec)
    second.status = CandidateStatus.CHALLENGER
    store.save_strategy_candidate(second)
    first.status = CandidateStatus.RETIRED
    second.status = CandidateStatus.CHAMPION
    store.promote_strategy_candidate(second, first)

    candidates = [
        StrategyCandidate.model_validate(payload) for payload in store.list_strategy_candidates()
    ]
    assert sum(item.status == CandidateStatus.CHAMPION for item in candidates) == 1
    assert next(item for item in candidates if item.id == second.id).status == "champion"

    tampered = second.model_copy(
        update={"spec": {**second.spec, "hypothesis": "tampered after registration"}}
    )
    with pytest.raises(ValueError, match="fingerprint"):
        store.save_strategy_candidate(tampered)


def test_challenger_cannot_be_compared_with_another_strategy_family():
    challenger = create_candidate(default_strategy_spec(StrategyKind.TREND))
    champion = create_candidate(default_strategy_spec(StrategyKind.BREAKOUT))
    challenger.status = CandidateStatus.CHALLENGER
    champion.status = CandidateStatus.CHAMPION
    challenger.backtest = run_walk_forward_backtest(
        demo_candles(),
        strategy_kind=StrategyKind.TREND,
    ).model_copy(update={"robustness_passed": True, "dataset_id": "ds_fixture"})
    challenger.dataset_id = "ds_fixture"

    decision = compare_for_promotion(challenger, champion)

    assert decision.approved is False
    assert decision.checks == {"strategy_family_match": False}


def test_promotion_retests_incumbent_on_challenger_dataset(monkeypatch):
    candles = demo_candles()
    dataset = ResearchDataset(
        id="ds_new",
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        candle_count=len(candles),
        data_start=candles[0].timestamp,
        data_end=candles[-1].timestamp,
        data_fingerprint="dataset_new_fingerprint",
    )
    challenger = create_candidate(
        default_strategy_spec(StrategyKind.TREND).model_copy(
            update={"hypothesis": "new challenger"}
        )
    )
    challenger_result = run_walk_forward_backtest(
        candles,
        strategy_kind=StrategyKind.TREND,
    ).model_copy(
        update={
            "dataset_id": dataset.id,
            "robustness_passed": True,
            "validation_reasons": ["challenger passed"],
            "profit_factor": Decimal("1.80"),
            "return_percent": Decimal("6"),
            "max_drawdown_percent": Decimal("3"),
            "total_trades": 100,
        }
    )
    challenger = attach_backtest(challenger, dataset.id, challenger_result)
    champion = create_candidate(
        default_strategy_spec(StrategyKind.TREND).model_copy(
            update={"hypothesis": "incumbent champion"}
        )
    )
    champion.status = CandidateStatus.CHAMPION
    champion.dataset_id = "ds_old"
    champion.backtest = challenger_result.model_copy(update={"dataset_id": "ds_old"})

    incumbent_result = challenger_result.model_copy(
        update={
            "dataset_id": dataset.id,
            "strategy_fingerprint": champion.spec_fingerprint,
            "profit_factor": Decimal("1.50"),
            "return_percent": Decimal("5"),
            "max_drawdown_percent": Decimal("3.5"),
            "total_trades": 90,
        }
    )
    calls = []

    class Store:
        def load_strategy_candidate(self, candidate_id):
            return challenger.model_dump(mode="json") if candidate_id == challenger.id else None

        def list_strategy_candidates(self, _limit):
            return [
                challenger.model_dump(mode="json"),
                champion.model_dump(mode="json"),
            ]

        def load_research_dataset(self, dataset_id):
            return dataset.model_dump(mode="json") if dataset_id == dataset.id else None

        def load_research_dataset_candles(self, dataset_id):
            assert dataset_id == dataset.id
            return [item.model_dump(mode="json") for item in candles]

        def load_research_dataset_funding(self, dataset_id):
            assert dataset_id == dataset.id
            return []

        def load_research_dataset_open_interest(self, dataset_id):
            assert dataset_id == dataset.id
            return []

        def promote_strategy_candidate(self, promoted, retired):
            calls.append(("promoted", promoted.id, retired.id if retired else None))

        def append_audit(self, _event):
            return None

    async def fake_backtest(candidate, selected_dataset, *_args):
        calls.append(("retested", candidate.id, selected_dataset.id))
        return incumbent_result

    monkeypatch.setattr(api, "store", Store())
    monkeypatch.setattr(api, "backtest_candidate_on_dataset", fake_backtest)
    monkeypatch.setattr(
        api,
        "detect_return_drift",
        lambda _candles: SimpleNamespace(status="stable"),
    )

    decision = asyncio.run(
        api.promote_strategy_candidate(
            challenger.id,
            api.CandidatePromotionRequest(confirmation=f"PROMOTE {challenger.spec_fingerprint}"),
        )
    )

    assert decision.approved is True
    assert decision.checks["same_dataset"] is True
    assert decision.checks["incumbent_retested_on_challenger_dataset"] is True
    assert ("retested", champion.id, dataset.id) in calls
    assert ("promoted", challenger.id, champion.id) in calls
    assert champion.dataset_id == "ds_old"
