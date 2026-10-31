from __future__ import annotations

from fastapi import Query

from .dependencies import ServiceDependency
from .operator_workspace import ReviewEvidenceItem, ReviewJobView, ReviewOverview


def register(app):

    @app.get("/api/review/overview", response_model=ReviewOverview)
    async def review_agent_overview(*, services: ServiceDependency):
        return await services.review_agent_overview()

    @app.get("/api/review/jobs", response_model=list[ReviewJobView])
    async def review_agent_jobs(
        limit: int = Query(default=100, ge=1, le=500), *, services: ServiceDependency
    ):
        return await services.review_agent_jobs(limit)

    @app.get("/api/review/jobs/{job_id}", response_model=ReviewJobView)
    async def review_agent_job(job_id: str, *, services: ServiceDependency):
        return await services.review_agent_job(job_id)

    @app.post("/api/review/backlog/reconcile")
    async def reconcile_review_backlog(*, services: ServiceDependency):
        return await services.reconcile_review_backlog()

    @app.get("/api/review/evidence", response_model=list[ReviewEvidenceItem])
    async def review_evidence(
        status: str | None = None,
        search: str = "",
        limit: int = Query(default=200, ge=1, le=500),
        *,
        services: ServiceDependency,
    ):
        return await services.review_evidence(status, search, limit)

    @app.post("/api/review/jobs/{job_id}/retry", response_model=ReviewJobView)
    async def retry_review_job(job_id: str, *, services: ServiceDependency):
        return await services.retry_review_job(job_id)
