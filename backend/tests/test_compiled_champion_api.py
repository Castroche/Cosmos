import asyncio
from decimal import Decimal
from types import SimpleNamespace

from app_fixture import services as api
from cosmos_quant.api_contracts import GenerateRequest
from cosmos_quant.backtest import run_walk_forward_backtest
from cosmos_quant.configuration import StrategyKind, UserConfiguration
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import PortfolioState
from cosmos_quant.research import ResearchDataset
from cosmos_quant.research_lifecycle import (
    CandidateStatus,
    attach_backtest,
    compile_champion,
    create_candidate,
)
from cosmos_quant.strategy_spec import default_strategy_spec


def _compiled_ai_champion():
    candles = demo_candles()
    dataset = ResearchDataset(
        id="ds_compiled_api",
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        candle_count=len(candles),
        data_start=candles[0].timestamp,
        data_end=candles[-1].timestamp,
        data_fingerprint="compiled_api_data",
    )
    spec = default_strategy_spec(StrategyKind.TREND).model_copy(
        update={"source": "ai", "hypothesis": "validated current trend evidence"}
    )
    candidate = create_candidate(spec)
    result = run_walk_forward_backtest(candles).model_copy(
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
    return candidate, dataset, candles


def test_generate_strategy_accepts_only_server_validated_ai_champion(monkeypatch):
    candidate, dataset, candles = _compiled_ai_champion()
    snapshot = snapshot_from_candles(candles)
    portfolio = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        available_margin=Decimal("100000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )

    class Runtime:
        async def market_context(self, **_kwargs):
            return snapshot, candles

        async def portfolio(self):
            return portfolio

    class Store:
        def load_strategy_candidate(self, candidate_id):
            return candidate.model_dump(mode="json") if candidate_id == candidate.id else None

        def load_research_dataset(self, dataset_id):
            return dataset.model_dump(mode="json") if dataset_id == dataset.id else None

        def save_strategy(self, _plan):
            return None

    monkeypatch.setattr(api, "runtime", Runtime())
    monkeypatch.setattr(api, "store", Store())
    monkeypatch.setattr(api, "plans", {})
    monkeypatch.setattr(api, "add_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api,
        "configuration_service",
        SimpleNamespace(current_configuration=lambda: UserConfiguration()),
    )

    plan = asyncio.run(
        api.generate_strategy(
            GenerateRequest(
                symbol="BTC/USDT",
                timeframe="1h",
                strategy_kind=StrategyKind.TREND,
                strategy_spec=candidate.spec,
                research_candidate_id=candidate.id,
            )
        )
    )

    assert plan.research_candidate_id == candidate.id
    assert plan.validated_strategy_artifact["spec_fingerprint"] == candidate.spec_fingerprint
    assert plan.strategy_spec["source"] == "ai"
