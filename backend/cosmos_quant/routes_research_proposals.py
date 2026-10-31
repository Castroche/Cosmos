from __future__ import annotations

from .api_contracts import AIProposalRequest, AIProposalResponse
from .dependencies import ServiceDependency


def register(app):

    @app.post("/api/research/ai-candidates", response_model=AIProposalResponse)
    async def propose_ai_strategy_candidates(
        request: AIProposalRequest, *, services: ServiceDependency
    ):
        return await services.propose_ai_strategy_candidates(request)
