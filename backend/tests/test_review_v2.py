import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app_fixture import services as api
from cosmos_quant.ai_configuration import AIProvider
from cosmos_quant.ai_usage import AICompletionResult, AIUsage
from cosmos_quant.api_contracts import AIProposalRequest
from cosmos_quant.backtest import BacktestResult, run_walk_forward_backtest
from cosmos_quant.configuration import StrategyKind
from cosmos_quant.demo import demo_candles
from cosmos_quant.research_lifecycle import (
    CandidateResearchContext,
    CandidateStatus,
    StrategyCandidate,
    create_candidate,
)
from cosmos_quant.review_pipeline import (
    ReviewScopeResult,
    StrategyReviewJob,
    StrategyReviewJobStatus,
)
from cosmos_quant.strategy_spec import default_strategy_spec


class ReviewStore:
    def __init__(self, candidate: StrategyCandidate):
        self.candidate = candidate.model_dump(mode="json")
        self.job = None

    def load_strategy_candidate(self, candidate_id):
        return self.candidate if self.candidate["id"] == candidate_id else None

    def list_strategy_candidates(self, _limit):
        return [self.candidate]

    def save_strategy_candidate(self, candidate):
        self.candidate = candidate.model_dump(mode="json")

    def save_strategy_review_job(self, job):
        self.job = job.model_copy(deep=True)


def v2_candidate(symbol: str = "BTC/USDT") -> StrategyCandidate:
    spec = default_strategy_spec(StrategyKind.TREND)
    return create_candidate(
        spec,
        research_context=CandidateResearchContext(
            exchange="okx",
            market_type="swap",
            symbol=symbol,
            timeframe="5m",
            market_as_of=datetime.now(UTC),
            summary_fingerprint="summary-v2",
            fee_bps=Decimal("6"),
            slippage_bps=Decimal("2"),
            risk_fraction=Decimal("0.005"),
            max_gross_exposure=Decimal("2"),
            max_leverage=Decimal("3"),
            trial_group="trial-v2",
            trial_count=4,
        ),
    )


def review_job(candidate: StrategyCandidate) -> StrategyReviewJob:
    context = candidate.research_context
    assert context is not None
    return StrategyReviewJob(
        candidate_id=candidate.id,
        candidate_fingerprint=candidate.spec_fingerprint,
        review_fingerprint="review-v2",
        review_contract_version=2,
        policy_fingerprint="policy-v2",
        origin_symbol=context.symbol,
        origin_timeframe=context.timeframe,
        trial_count=context.trial_count,
    )


def result_with_status(passed: bool) -> BacktestResult:
    result = run_walk_forward_backtest(demo_candles())
    return result.model_copy(
        deep=True,
        update={
            "robustness_passed": passed,
            "validation_reasons": ["基础稳健性门槛全部通过"]
            if passed
            else ["Profit Factor 低于门槛"],
        },
    )


def install_scope_runner(monkeypatch, outcomes):
    async def run_scope(candidate, *, symbol, role, trial_count):
        assert trial_count == 4
        result = outcomes[symbol]
        return result, ReviewScopeResult(
            symbol=symbol,
            timeframe="5m",
            role=role,
            status=(
                "blocked"
                if result is None
                else "passed"
                if result.robustness_passed
                else "failed"
            ),
            stage="data_required" if result is None else "validated",
            dataset_id=f"ds-{symbol}",
            dataset_fingerprint=f"fp-{symbol}",
            metrics={} if result is None else {"profit_factor": str(result.profit_factor)},
            reasons=(
                ["缺少审核所需历史特征：funding_rate"]
                if result is None
                else result.validation_reasons
            ),
        )

    monkeypatch.setattr(api, "run_candidate_review_scope", run_scope)


@pytest.mark.parametrize(
    ("eth_passed", "sol_passed", "expected_status", "expected_tier"),
    [
        (True, False, CandidateStatus.CHALLENGER, "sandbox_canary"),
        (False, False, CandidateStatus.VALIDATED, "paper_eligible"),
    ],
)
def test_origin_pass_then_cross_market_requires_two_of_three(
    monkeypatch,
    eth_passed,
    sol_passed,
    expected_status,
    expected_tier,
):
    candidate = v2_candidate()
    store = ReviewStore(candidate)
    monkeypatch.setattr(api, "store", store)
    install_scope_runner(
        monkeypatch,
        {
            "BTC/USDT": result_with_status(True),
            "ETH/USDT": result_with_status(eth_passed),
            "SOL/USDT": result_with_status(sol_passed),
        },
    )

    completed = asyncio.run(api.process_strategy_review_job(review_job(candidate)))
    persisted = StrategyCandidate.model_validate(store.candidate)

    assert completed.status == StrategyReviewJobStatus.ADMITTED
    assert completed.evidence_tier == expected_tier
    assert persisted.status == expected_status
    assert completed.metrics["cross_market_passed_count"] == (
        2 if eth_passed else 1
    )


def test_origin_failure_is_rejected_before_cross_market(monkeypatch):
    candidate = v2_candidate()
    store = ReviewStore(candidate)
    monkeypatch.setattr(api, "store", store)
    install_scope_runner(monkeypatch, {"BTC/USDT": result_with_status(False)})

    completed = asyncio.run(api.process_strategy_review_job(review_job(candidate)))
    persisted = StrategyCandidate.model_validate(store.candidate)

    assert completed.status == StrategyReviewJobStatus.REJECTED
    assert completed.stage == "origin_rejected"
    assert persisted.status == CandidateStatus.REJECTED


def test_missing_required_data_blocks_without_zero_trade_rejection(monkeypatch):
    candidate = v2_candidate()
    store = ReviewStore(candidate)
    monkeypatch.setattr(api, "store", store)
    install_scope_runner(monkeypatch, {"BTC/USDT": None})

    completed = asyncio.run(api.process_strategy_review_job(review_job(candidate)))
    persisted = StrategyCandidate.model_validate(store.candidate)

    assert completed.status == StrategyReviewJobStatus.BLOCKED
    assert completed.stage == "data_required"
    assert "funding_rate" in (completed.error or "")
    assert persisted.status == CandidateStatus.PROPOSED
    assert persisted.backtest is None


def test_review_overview_excludes_legacy_proposed_from_active_waiting(monkeypatch):
    legacy = create_candidate(default_strategy_spec(StrategyKind.TREND))
    current = v2_candidate("ETH/USDT")

    class OverviewStore:
        def list_strategy_candidates(self, _limit):
            return [legacy.model_dump(mode="json"), current.model_dump(mode="json")]

        def list_strategy_review_jobs(self, _limit):
            return []

        def list_research_campaigns(self, _limit):
            return []

        def list_research_campaign_jobs(self, _limit):
            return []

    monkeypatch.setattr(api, "store", OverviewStore())

    overview = api.build_review_overview()

    assert overview.legacy == 1
    assert overview.unreviewed == 1
    assert overview.queued == 0
    assert overview.jobs == []


def test_ai_generation_uses_reasoning_once_then_non_reasoning_schema_repair(
    monkeypatch,
):
    requests = []
    saved = []
    spec = default_strategy_spec(StrategyKind.TREND)

    class Gateway:
        async def generate_json(self, **kwargs):
            requests.append(kwargs)
            data = {} if len(requests) == 1 else spec.model_dump(mode="json")
            return (
                AICompletionResult(
                    provider=AIProvider.DEEPSEEK,
                    model="deepseek-test",
                    data=data,
                    usage=AIUsage(),
                    latency_ms=1,
                ),
                [AIProvider.DEEPSEEK],
            )

    class CandidateStore:
        def list_strategy_candidates(self, _limit):
            return []

        def save_strategy_candidate(self, candidate):
            saved.append(candidate.model_copy(deep=True))

    async def market_context(**_kwargs):
        market = type("Market", (), {"timestamp": datetime.now(UTC)})()
        return market, []

    async def generation_summary(**_kwargs):
        return {
            "origin_timeframe": "5m",
            "timeframes": {
                "5m": {
                    "last_price": "100",
                    "atr_14": "10",
                    "coverage_ratio": "1",
                    "closed_bars_only": True,
                },
                "15m": {"coverage_ratio": "1"},
                "1h": {"coverage_ratio": "1"},
                "4h": {"coverage_ratio": "1"},
            },
        }

    gateway = Gateway()
    monkeypatch.setattr(api, "ai_gateway", gateway)
    monkeypatch.setattr(api, "store", CandidateStore())
    monkeypatch.setattr(api, "add_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api.runtime, "market_context", market_context)
    monkeypatch.setattr(api, "build_ai_generation_summary", generation_summary)
    monkeypatch.setattr(
        api,
        "recent_candidate_failure_digest",
        lambda **_kwargs: ([{"reasons": ["cost exceeded"]}], ["rejected-fp"]),
    )

    response = asyncio.run(
        api.propose_ai_strategy_candidates(
            AIProposalRequest(
                hypothesis="验证成本感知的低换手趋势假设",
                provider=AIProvider.DEEPSEEK,
                symbol="BTC/USDT",
                timeframe="5m",
            )
        )
    )

    assert [request["thinking_enabled"] for request in requests] == [True, False]
    assert "fee_bps=" in requests[0]["prompt"]
    assert "Recent development failures" in requests[0]["prompt"]
    assert "coverage_ratio" in requests[0]["prompt"]
    assert len(saved) == 1
    assert saved[0].research_context is not None
    assert response.proposals[0].preflight["eligible"] is True
