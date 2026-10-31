from __future__ import annotations

from .api_contracts import (
    CampaignRevisionRequest,
    CandidateRevisionResponse,
    CausalPolicyRevisionRequest,
    CausalPolicyRevisionResponse,
)
from .dependencies import ServiceDependency


def register(app):

    @app.post(
        "/api/research/campaigns/{campaign_id}/revise", response_model=CandidateRevisionResponse
    )
    async def revise_campaign_strategy(
        campaign_id: str, request: CampaignRevisionRequest, *, services: ServiceDependency
    ):
        return await services.revise_campaign_strategy(campaign_id, request)

    @app.post(
        "/api/research/campaigns/{campaign_id}/revise-causal-policy",
        response_model=CausalPolicyRevisionResponse,
    )
    async def revise_campaign_causal_policy(
        campaign_id: str, request: CausalPolicyRevisionRequest, *, services: ServiceDependency
    ) -> CausalPolicyRevisionResponse:
        return await services.revise_campaign_causal_policy(campaign_id, request)
