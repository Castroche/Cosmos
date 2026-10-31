from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter

from fastapi import HTTPException

from .api_contracts import ResearchCampaignRequest
from .research_campaign import (
    ResearchCampaignJob,
    ResearchCampaignJobStatus,
    ResearchCampaignReport,
    campaign_request_fingerprint,
)

logger = __import__("logging").getLogger(__name__)


class ResearchJobsService:
    async def run_research_campaign(
        self,
        request: ResearchCampaignRequest,
    ) -> ResearchCampaignReport:
        """Run synchronously for Agent tools and backwards-compatible callers."""

        return await self.execute_research_campaign(request)

    async def run_research_campaign_job(self, job: ResearchCampaignJob) -> None:
        job.status = ResearchCampaignJobStatus.RUNNING
        job.started_at = job.started_at or datetime.now(UTC)
        job.completed_at = None
        job.error = None
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(self.store.save_research_campaign_job, job)
        last_progress_persisted_at = perf_counter()

        async def persist_progress(
            completed: int,
            total: int,
            cache_hits: int,
            computed: int,
        ) -> None:
            nonlocal last_progress_persisted_at
            job.total_cells = total
            job.completed_cells = completed
            job.cache_hits = cache_hits
            job.computed_cells = computed
            job.updated_at = datetime.now(UTC)
            now = perf_counter()
            if completed not in {0, total} and now - last_progress_persisted_at < 0.5:
                return
            await asyncio.to_thread(self.store.save_research_campaign_job, job)
            last_progress_persisted_at = now

        try:
            report = await self.execute_research_campaign(
                ResearchCampaignRequest.model_validate(job.request),
                progress_callback=persist_progress,
            )
            job.report_id = report.id
            job.status = ResearchCampaignJobStatus.COMPLETED
            job.completed_at = datetime.now(UTC)
        except asyncio.CancelledError:
            job.status = ResearchCampaignJobStatus.QUEUED
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_research_campaign_job, job)
            raise
        except Exception as exc:
            job.status = ResearchCampaignJobStatus.FAILED
            job.error = str(getattr(exc, "detail", exc))[:1000]
            job.failed_cells = max(job.total_cells - job.completed_cells, 0)
            job.completed_at = datetime.now(UTC)
        finally:
            if job.status != ResearchCampaignJobStatus.QUEUED:
                job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_research_campaign_job, job)

    def schedule_research_campaign_job(self, job: ResearchCampaignJob) -> None:
        task = asyncio.create_task(
            self.run_research_campaign_job(job),
            name=f"research-campaign:{job.id}",
        )
        self.campaign_job_tasks.add(task)
        task.add_done_callback(self.campaign_job_tasks.discard)

    async def queue_research_campaign(
        self,
        request: ResearchCampaignRequest,
    ) -> ResearchCampaignJob:
        normalized_request = request.model_dump(mode="json")
        normalized_request["dataset_ids"] = sorted(set(request.dataset_ids))
        normalized_request["blind_dataset_ids"] = sorted(set(request.blind_dataset_ids))
        normalized_request["library_entry_ids"] = sorted(set(request.library_entry_ids))
        normalized_request["candidate_ids"] = sorted(set(request.candidate_ids))
        normalized_request["causal_policy_specs"] = sorted(
            [
                {
                    **item.model_dump(mode="json"),
                    "library_entry_ids": sorted(set(item.library_entry_ids)),
                }
                for item in request.causal_policy_specs
            ],
            key=lambda item: item["id"],
        )
        request_fingerprint = campaign_request_fingerprint(normalized_request)
        recent_payloads = await asyncio.to_thread(
            self.store.list_research_campaign_jobs,
            100,
        )
        for payload in recent_payloads:
            existing = ResearchCampaignJob.model_validate(payload)
            if existing.request_fingerprint == request_fingerprint and existing.status in {
                ResearchCampaignJobStatus.QUEUED,
                ResearchCampaignJobStatus.RUNNING,
            }:
                return existing
        job = ResearchCampaignJob(
            request=normalized_request,
            request_fingerprint=request_fingerprint,
        )
        await asyncio.to_thread(self.store.save_research_campaign_job, job)
        self.schedule_research_campaign_job(job)
        return job

    async def list_research_campaign_jobs(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_research_campaign_jobs,
            min(max(limit, 1), 200),
        )
        return [ResearchCampaignJob.model_validate(payload) for payload in payloads]

    async def get_research_campaign_job(self, job_id: str):
        payload = await asyncio.to_thread(
            self.store.load_research_campaign_job,
            job_id,
        )
        if not payload:
            raise HTTPException(404, "research campaign job not found")
        return ResearchCampaignJob.model_validate(payload)

    async def list_research_campaign_reports(self):
        payloads = await asyncio.to_thread(self.store.list_research_campaigns, 20)
        return [ResearchCampaignReport.model_validate(payload) for payload in payloads]

    async def get_research_campaign_report(self, campaign_id: str):
        payload = await asyncio.to_thread(
            self.store.load_research_campaign,
            campaign_id,
        )
        if not payload:
            raise HTTPException(404, "research campaign not found")
        return ResearchCampaignReport.model_validate(payload)
