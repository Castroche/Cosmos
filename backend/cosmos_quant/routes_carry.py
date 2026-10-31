from __future__ import annotations

from .api_contracts import CarryCampaignRequest, CarryResearchRequest
from .carry import CarryBacktestReport, CarryStrategySpec
from .carry_campaign import CarryResearchCampaignReport
from .dependencies import ServiceDependency


def register(app):

    @app.get("/api/research/carry/strategy-library", response_model=list[CarryStrategySpec])
    async def get_carry_strategy_library(*, services: ServiceDependency):
        """Expose immutable public-prior carry hypotheses; none are pre-approved."""
        return await services.get_carry_strategy_library()

    @app.post("/api/research/carry/backtests", response_model=list[CarryBacktestReport])
    async def run_carry_backtests(request: CarryResearchRequest, *, services: ServiceDependency):
        return await services.run_carry_backtests(request)

    @app.get("/api/research/carry/backtests", response_model=list[CarryBacktestReport])
    async def list_carry_backtests(limit: int = 50, *, services: ServiceDependency):
        return await services.list_carry_backtests(limit)

    @app.post("/api/research/carry/campaigns", response_model=CarryResearchCampaignReport)
    async def run_carry_campaign(request: CarryCampaignRequest, *, services: ServiceDependency):
        return await services.run_carry_campaign(request)

    @app.get("/api/research/carry/campaigns", response_model=list[CarryResearchCampaignReport])
    async def list_carry_campaigns(limit: int = 50, *, services: ServiceDependency):
        return await services.list_carry_campaigns(limit)

    @app.get(
        "/api/research/carry/campaigns/{campaign_id}", response_model=CarryResearchCampaignReport
    )
    async def get_carry_campaign(campaign_id: str, *, services: ServiceDependency):
        return await services.get_carry_campaign(campaign_id)
