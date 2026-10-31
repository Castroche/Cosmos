from __future__ import annotations

from .api_contracts import ResearchCampaignRequest
from .dependencies import ServiceDependency
from .research_campaign import ResearchCampaignJob, ResearchCampaignReport


def register(app):

    @app.post("/api/research/campaigns/run", response_model=ResearchCampaignReport)
    async def run_research_campaign(
        request: ResearchCampaignRequest, *, services: ServiceDependency
    ) -> ResearchCampaignReport:
        """Run synchronously for Agent tools and backwards-compatible callers."""
        return await services.run_research_campaign(request)

    @app.post("/api/research/campaign-jobs", response_model=ResearchCampaignJob, status_code=202)
    async def queue_research_campaign(
        request: ResearchCampaignRequest, *, services: ServiceDependency
    ) -> ResearchCampaignJob:
        return await services.queue_research_campaign(request)

    @app.get("/api/research/campaign-jobs", response_model=list[ResearchCampaignJob])
    async def list_research_campaign_jobs(limit: int = 50, *, services: ServiceDependency):
        return await services.list_research_campaign_jobs(limit)

    @app.get("/api/research/campaign-jobs/{job_id}", response_model=ResearchCampaignJob)
    async def get_research_campaign_job(job_id: str, *, services: ServiceDependency):
        return await services.get_research_campaign_job(job_id)

    @app.get("/api/research/campaigns", response_model=list[ResearchCampaignReport])
    async def list_research_campaign_reports(*, services: ServiceDependency):
        return await services.list_research_campaign_reports()

    @app.get("/api/research/campaigns/{campaign_id}", response_model=ResearchCampaignReport)
    async def get_research_campaign_report(campaign_id: str, *, services: ServiceDependency):
        return await services.get_research_campaign_report(campaign_id)
