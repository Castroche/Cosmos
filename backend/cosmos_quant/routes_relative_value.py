from __future__ import annotations

from .api_contracts import RelativeValueCampaignRequest, RelativeValueResearchRequest
from .dependencies import ServiceDependency
from .relative_value import PairBacktestReport
from .relative_value_campaign import PairResearchCampaignReport


def register(app):

    @app.get("/api/research/relative-value/library")
    async def relative_value_strategy_library(*, services: ServiceDependency):
        return await services.relative_value_strategy_library()

    @app.post("/api/research/relative-value/backtests", response_model=list[PairBacktestReport])
    async def run_relative_value_backtests(
        request: RelativeValueResearchRequest, *, services: ServiceDependency
    ):
        return await services.run_relative_value_backtests(request)

    @app.get("/api/research/relative-value/backtests", response_model=list[PairBacktestReport])
    async def list_relative_value_backtests(limit: int = 50, *, services: ServiceDependency):
        return await services.list_relative_value_backtests(limit)

    @app.post("/api/research/relative-value/campaigns", response_model=PairResearchCampaignReport)
    async def run_relative_value_campaign(
        request: RelativeValueCampaignRequest, *, services: ServiceDependency
    ):
        return await services.run_relative_value_campaign(request)

    @app.get(
        "/api/research/relative-value/campaigns", response_model=list[PairResearchCampaignReport]
    )
    async def list_relative_value_campaigns(limit: int = 50, *, services: ServiceDependency):
        return await services.list_relative_value_campaigns(limit)

    @app.get(
        "/api/research/relative-value/campaigns/{campaign_id}",
        response_model=PairResearchCampaignReport,
    )
    async def get_relative_value_campaign(campaign_id: str, *, services: ServiceDependency):
        return await services.get_relative_value_campaign(campaign_id)
