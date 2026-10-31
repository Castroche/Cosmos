from __future__ import annotations

from .api_contracts import (
    CandidatePromotionRequest,
    CandidateRequest,
    CandidateRevisionRequest,
    CandidateRevisionResponse,
    StrategyGovernanceReport,
)
from .dependencies import ServiceDependency
from .ml_challenger import MLChallengerReport
from .research import BacktestJob, ResearchDataset
from .research_lifecycle import PromotionDecision, StrategyCandidate


def register(app):

    @app.get("/api/research/datasets/{dataset_id}", response_model=ResearchDataset)
    async def research_dataset(dataset_id: str, *, services: ServiceDependency):
        return await services.research_dataset(dataset_id)

    @app.post("/api/research/backtests/{plan_id}", response_model=BacktestJob, status_code=202)
    async def queue_research_backtest(plan_id: str, *, services: ServiceDependency):
        return await services.queue_research_backtest(plan_id)

    @app.get("/api/research/backtests/{job_id}", response_model=BacktestJob)
    async def research_backtest_job(job_id: str, *, services: ServiceDependency):
        return await services.research_backtest_job(job_id)

    @app.get("/api/research/backtests", response_model=list[BacktestJob])
    async def research_backtest_jobs(limit: int = 50, *, services: ServiceDependency):
        return await services.research_backtest_jobs(limit)

    @app.post("/api/research/candidates", response_model=StrategyCandidate)
    async def propose_strategy_candidate(request: CandidateRequest, *, services: ServiceDependency):
        return await services.propose_strategy_candidate(request)

    @app.get("/api/research/candidates", response_model=list[StrategyCandidate])
    async def research_candidates(limit: int = 100, *, services: ServiceDependency):
        return await services.research_candidates(limit)

    @app.post("/api/research/governance/review", response_model=StrategyGovernanceReport)
    async def review_strategy_governance(*, services: ServiceDependency):
        return await services.review_strategy_governance()

    @app.post("/api/research/candidates/{candidate_id}/evaluate", response_model=StrategyCandidate)
    async def evaluate_strategy_candidate(
        candidate_id: str, dataset_id: str, *, services: ServiceDependency
    ):
        return await services.evaluate_strategy_candidate(candidate_id, dataset_id)

    @app.post(
        "/api/research/candidates/{candidate_id}/revise", response_model=CandidateRevisionResponse
    )
    async def revise_strategy_candidate(
        candidate_id: str, request: CandidateRevisionRequest, *, services: ServiceDependency
    ):
        return await services.revise_strategy_candidate(candidate_id, request)

    @app.post("/api/research/candidates/{candidate_id}/promote", response_model=PromotionDecision)
    async def promote_strategy_candidate(
        candidate_id: str, request: CandidatePromotionRequest, *, services: ServiceDependency
    ):
        return await services.promote_strategy_candidate(candidate_id, request)

    @app.post("/api/research/ml-challenger", response_model=MLChallengerReport)
    async def create_ml_challenger(*, services: ServiceDependency):
        return await services.create_ml_challenger()

    @app.get("/api/research/drift")
    async def research_drift(*, services: ServiceDependency):
        return await services.research_drift()
