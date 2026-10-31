from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import HTTPException, Query
from pydantic import ValidationError

from .operator_workspace import ReviewEvidenceItem, ReviewJobView, ReviewOverview
from .research_campaign import (
    ResearchCampaignJob,
    ResearchCampaignJobStatus,
    ResearchCampaignReport,
)
from .research_lifecycle import CandidateStatus, StrategyCandidate
from .review_pipeline import StrategyReviewJob, StrategyReviewJobStatus

logger = __import__("logging").getLogger(__name__)


class ReviewService:
    def build_review_jobs(
        self,
        limit: int = 100,
        *,
        candidate_payloads: list[dict] | None = None,
        candidate_summaries: list[dict] | None = None,
        campaign_payloads: list[dict] | None = None,
    ) -> list[ReviewJobView]:
        views: list[ReviewJobView] = []
        if candidate_summaries is None:
            if candidate_payloads is not None:
                candidate_summaries = [
                    {
                        "candidate_id": str(payload.get("id") or ""),
                        "status": str(payload.get("status") or ""),
                        "display_name": str(
                            (payload.get("spec") or {}).get("name")
                            or (payload.get("spec") or {}).get("label")
                            or payload.get("id")
                            or ""
                        ),
                    }
                    for payload in candidate_payloads
                ]
            else:
                candidate_summaries = self.store.list_strategy_candidate_summaries(max(500, limit))
        candidate_summary_by_id = {
            str(summary.get("candidate_id") or ""): summary for summary in candidate_summaries
        }
        for payload in self.store.list_strategy_review_jobs(limit):
            job = StrategyReviewJob.model_validate(payload)
            candidate_summary = candidate_summary_by_id.get(job.candidate_id)
            views.append(
                ReviewJobView(
                    id=job.id,
                    candidate_id=job.candidate_id,
                    candidate_name=str(
                        (candidate_summary or {}).get("display_name") or job.candidate_id
                    ),
                    candidate_fingerprint=job.candidate_fingerprint,
                    review_contract_version=job.review_contract_version,
                    policy_fingerprint=job.policy_fingerprint,
                    origin_scope={
                        key: value
                        for key, value in {
                            "symbol": job.origin_symbol,
                            "timeframe": job.origin_timeframe,
                        }.items()
                        if value
                    },
                    scope_results=[result.model_dump(mode="json") for result in job.scope_results],
                    status=str(job.status),
                    stage=job.stage,
                    evidence_tier=job.evidence_tier,
                    progress_percent=job.progress_percent,
                    rejection_reasons=job.rejection_reasons,
                    evidence_refs=job.evidence_refs,
                    metrics=job.metrics,
                    dataset_id=job.dataset_id,
                    attempt_count=job.attempt_count,
                    error=job.error,
                    updated_at=job.updated_at,
                )
            )
        remaining = max(0, limit - len(views))
        if remaining == 0:
            return views[:limit]
        if campaign_payloads is None:
            campaign_payloads = self.store.list_research_campaigns(remaining)
        campaign_payload_by_id = {
            str(payload.get("id") or ""): payload for payload in campaign_payloads
        }
        for payload in self.store.list_research_campaign_jobs(remaining):
            job = ResearchCampaignJob.model_validate(payload)
            report = None
            if job.report_id:
                report_payload = campaign_payload_by_id.get(job.report_id)
                if report_payload:
                    report = ResearchCampaignReport.model_validate(report_payload)
            if job.status == ResearchCampaignJobStatus.QUEUED:
                stage = "queued"
                tier = "unreviewed"
            elif job.status == ResearchCampaignJobStatus.RUNNING:
                stage = "validation"
                tier = "unreviewed"
            elif job.status == ResearchCampaignJobStatus.FAILED:
                stage = "rejected"
                tier = "rejected"
            elif report and report.execution_certificate:
                stage = "admitted"
                tier = "sandbox_mature"
            elif report and any(candidate.cross_market_passed for candidate in report.candidates):
                stage = "canary"
                tier = "sandbox_canary"
            else:
                stage = "rejected"
                tier = "rejected"
            total = max(job.total_cells, 1)
            views.append(
                ReviewJobView(
                    id=job.id,
                    status=str(job.status),
                    stage=stage,
                    evidence_tier=tier,
                    progress_percent=min(100, int(job.completed_cells * 100 / total)),
                    report_id=job.report_id,
                    candidate_count=len(report.candidates) if report else 0,
                    selected_strategy_id=(report.selected_library_entry_id if report else None),
                    rejection_reasons=(
                        report.selection_reasons[:8]
                        if report and not report.execution_certificate
                        else []
                    ),
                    error=job.error,
                    updated_at=job.updated_at,
                )
            )
        return views[:limit]

    def build_review_overview(self) -> ReviewOverview:
        all_strategy_jobs = [
            StrategyReviewJob.model_validate(item)
            for item in self.store.list_strategy_review_jobs(1000)
        ]
        strategy_jobs = [job for job in all_strategy_jobs if job.review_contract_version >= 2]
        candidate_payloads = self.store.list_strategy_candidates(500)
        candidate_summaries = [
            {
                "candidate_id": str(payload.get("id") or ""),
                "status": str(payload.get("status") or ""),
                "display_name": str(
                    (payload.get("spec") or {}).get("name")
                    or (payload.get("spec") or {}).get("label")
                    or payload.get("id")
                    or ""
                ),
            }
            for payload in candidate_payloads
        ]
        v2_candidate_payloads = [
            payload
            for payload in candidate_payloads
            if isinstance(payload.get("research_context"), dict)
        ]
        active_job_candidate_ids = {job.candidate_id for job in strategy_jobs}
        jobs = self.build_review_jobs(
            candidate_payloads=candidate_payloads,
            candidate_summaries=candidate_summaries,
        )
        return ReviewOverview(
            queued=sum(job.status == StrategyReviewJobStatus.QUEUED for job in strategy_jobs),
            unreviewed=sum(
                str(candidate.get("status") or "") == str(CandidateStatus.PROPOSED)
                and str(candidate.get("id") or "") not in active_job_candidate_ids
                for candidate in v2_candidate_payloads
            ),
            running=sum(job.status == StrategyReviewJobStatus.RUNNING for job in strategy_jobs),
            blocked=sum(job.status == StrategyReviewJobStatus.BLOCKED for job in strategy_jobs),
            paper_eligible=sum(
                str(candidate.get("status") or "")
                in {
                    str(CandidateStatus.VALIDATED),
                    str(CandidateStatus.CHALLENGER),
                    str(CandidateStatus.CHAMPION),
                }
                for candidate in v2_candidate_payloads
            ),
            sandbox_canary=sum(job.evidence_tier == "sandbox_canary" for job in strategy_jobs),
            sandbox_mature=sum(job.evidence_tier == "sandbox_mature" for job in strategy_jobs),
            live_capped=0,
            rejected=sum(job.status == StrategyReviewJobStatus.REJECTED for job in strategy_jobs)
            + sum(
                str(candidate.get("status") or "") == str(CandidateStatus.REJECTED)
                and str(candidate.get("id") or "") not in active_job_candidate_ids
                for candidate in v2_candidate_payloads
            ),
            legacy=sum(
                not isinstance(candidate.get("research_context"), dict)
                for candidate in candidate_payloads
            ),
            data_blocked=sum(
                job.status == StrategyReviewJobStatus.BLOCKED and job.stage == "data_required"
                for job in strategy_jobs
            ),
            jobs=jobs,
            pipeline={
                "compiler": "deterministic",
                "backtest": "deterministic",
                "comparison": "deterministic",
                "execution_llm_calls": 0,
                "worker_status": str(self.review_worker_health.get("status") or "stopped"),
                "worker_activity": str(self.review_worker_health.get("activity") or "unknown"),
                "worker_current_job_id": str(self.review_worker_health.get("current_job_id") or ""),
                "worker_last_error": str(self.review_worker_health.get("last_error") or ""),
            },
        )

    def build_review_evidence(
        self,
        *,
        status: str | None = None,
        search: str = "",
        limit: int = 200,
    ) -> list[ReviewEvidenceItem]:
        items: list[ReviewEvidenceItem] = []
        normalized_status = (status or "").strip().lower()
        normalized_search = search.strip().lower()
        tier_by_status = {
            str(CandidateStatus.PROPOSED): "unreviewed",
            str(CandidateStatus.VALIDATED): "paper_eligible",
            str(CandidateStatus.CHALLENGER): "sandbox_canary",
            str(CandidateStatus.CHAMPION): "sandbox_mature",
            str(CandidateStatus.REJECTED): "rejected",
            str(CandidateStatus.RETIRED): "retired",
        }
        review_jobs_by_candidate: dict[str, StrategyReviewJob] = {}
        for payload in self.store.list_strategy_review_jobs(1000):
            job = StrategyReviewJob.model_validate(payload)
            review_jobs_by_candidate.setdefault(job.candidate_id, job)
        for payload in self.store.list_strategy_candidates(min(max(limit, 1), 500)):
            try:
                candidate = StrategyCandidate.model_validate(payload)
            except ValidationError as exc:
                candidate_id = str(payload.get("id") or "legacy-invalid-candidate")
                spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
                item = ReviewEvidenceItem(
                    id=candidate_id,
                    name=str(spec.get("name") or spec.get("label") or candidate_id),
                    status="invalid",
                    evidence_tier="rejected",
                    source=str(payload.get("source") or spec.get("source") or "legacy"),
                    family=str(spec.get("family") or spec.get("strategy_kind") or "unknown"),
                    summary="候选与当前 StrategySpec 合约不兼容，已隔离且不可交易。",
                    reasons=[
                        "候选结构、字段范围或指纹无法通过当前 StrategySpec 校验",
                        "历史记录继续保留，但不会进入策略库、审核队列或执行层",
                    ],
                    evidence_refs=[candidate_id],
                    technical_details=str(exc)[:2000],
                    review_contract_version=1,
                    updated_at=datetime.fromisoformat(str(payload.get("updated_at")))
                    if payload.get("updated_at")
                    else datetime.now(UTC),
                )
            else:
                candidate_status = str(candidate.status)
                review_job = review_jobs_by_candidate.get(candidate.id)
                is_legacy = candidate.research_context is None
                backtest = candidate.backtest
                reasons = list(candidate.decision_reasons)
                if backtest:
                    reasons.extend(backtest.validation_reasons)
                metrics: dict[str, str | int | float | bool | None] = {}
                if backtest:
                    metrics = {
                        "return_percent": str(backtest.return_percent),
                        "max_drawdown_percent": str(backtest.max_drawdown_percent),
                        "profit_factor": str(backtest.profit_factor),
                        "win_rate_percent": str(backtest.win_rate_percent),
                        "total_trades": backtest.total_trades,
                        "robustness_passed": backtest.robustness_passed,
                    }
                if review_job:
                    metrics.update(review_job.metrics)
                    reasons.extend(review_job.rejection_reasons)
                evidence_tier = tier_by_status.get(candidate_status, "unreviewed")
                display_status = candidate_status
                if is_legacy and candidate.status == CandidateStatus.PROPOSED:
                    display_status = "legacy"
                    evidence_tier = "legacy"
                    reasons.insert(
                        0,
                        "历史审核 v1：只读保留，不进入活动审核队列，也不会自动重审。",
                    )
                elif review_job:
                    evidence_tier = review_job.evidence_tier
                context = candidate.research_context
                item = ReviewEvidenceItem(
                    id=candidate.id,
                    name=str(
                        candidate.spec.get("name") or candidate.spec.get("label") or candidate.id
                    ),
                    status=display_status,
                    evidence_tier=evidence_tier,
                    source=candidate.source,
                    family=str(candidate.spec.get("family") or "unknown"),
                    summary=(reasons[0] if reasons else "候选已登记，等待确定性审核证据。"),
                    reasons=list(dict.fromkeys(reasons))[:40],
                    metrics=metrics,
                    evidence_refs=[
                        reference
                        for reference in (
                            candidate.id,
                            candidate.dataset_id,
                            candidate.spec_fingerprint,
                            review_job.id if review_job else None,
                        )
                        if reference
                    ],
                    review_contract_version=(
                        review_job.review_contract_version
                        if review_job
                        else (1 if is_legacy else 2)
                    ),
                    origin_scope=(
                        {
                            "symbol": context.symbol,
                            "timeframe": context.timeframe,
                        }
                        if context
                        else {}
                    ),
                    scope_results=(
                        [result.model_dump(mode="json") for result in review_job.scope_results]
                        if review_job
                        else []
                    ),
                    updated_at=candidate.updated_at,
                )
            if normalized_status and normalized_status not in {
                item.status.lower(),
                item.evidence_tier.lower(),
            }:
                continue
            if (
                normalized_search
                and normalized_search
                not in " ".join(
                    [item.id, item.name, item.family, item.source, item.summary, *item.reasons]
                ).lower()
            ):
                continue
            items.append(item)
        return sorted(items, key=lambda item: item.updated_at, reverse=True)

    async def review_agent_overview(self):
        return await asyncio.to_thread(self.build_review_overview)

    async def review_agent_jobs(self, limit: int = Query(default=100, ge=1, le=500)):
        return await asyncio.to_thread(self.build_review_jobs, limit)

    async def review_agent_job(self, job_id: str):
        item = next((job for job in self.build_review_jobs(500) if job.id == job_id), None)
        if item is None:
            raise HTTPException(404, "review job not found")
        return item

    async def reconcile_review_backlog(self):
        result = await asyncio.to_thread(self.reconcile_strategy_review_backlog)
        self.review_worker_wakeup.set()
        return {
            **result,
            "worker_status": self.review_worker_health.get("status"),
            "worker_activity": self.review_worker_health.get("activity"),
        }

    async def review_evidence(
        self,
        status: str | None = None,
        search: str = "",
        limit: int = Query(default=200, ge=1, le=500),
    ):
        return await asyncio.to_thread(
            self.build_review_evidence,
            status=status,
            search=search,
            limit=limit,
        )

    async def retry_review_job(self, job_id: str):
        strategy_payload = await asyncio.to_thread(self.store.load_strategy_review_job, job_id)
        if strategy_payload:
            strategy_job = StrategyReviewJob.model_validate(strategy_payload)
            if strategy_job.review_contract_version <= 1:
                raise HTTPException(409, "v1 review evidence is read-only")
            if strategy_job.status != StrategyReviewJobStatus.BLOCKED:
                raise HTTPException(409, "only a blocked v2 review can be retried")
            strategy_job.status = StrategyReviewJobStatus.QUEUED
            strategy_job.stage = "queued"
            strategy_job.error = None
            strategy_job.completed_at = None
            strategy_job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_strategy_review_job, strategy_job)
            self.review_worker_wakeup.set()
            return next(item for item in self.build_review_jobs(500) if item.id == strategy_job.id)
        payload = await asyncio.to_thread(self.store.load_research_campaign_job, job_id)
        if not payload:
            raise HTTPException(404, "review job not found")
        job = ResearchCampaignJob.model_validate(payload)
        if job.status != ResearchCampaignJobStatus.FAILED:
            raise HTTPException(409, "only a failed review job can be retried")
        job.status = ResearchCampaignJobStatus.QUEUED
        job.error = None
        job.started_at = None
        job.completed_at = None
        job.completed_cells = 0
        job.failed_cells = 0
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(self.store.save_research_campaign_job, job)
        self.schedule_research_campaign_job(job)
        return next(item for item in self.build_review_jobs() if item.id == job.id)
