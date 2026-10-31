from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from .api_contracts import (
    CandidatePromotionRequest,
    CandidateRequest,
    CandidateRevisionRequest,
    CandidateRevisionResponse,
    StrategyGovernanceReport,
)
from .domain import Candle, FundingRatePoint, OpenInterestPoint
from .ml_challenger import train_ml_challenger
from .research import BacktestJob, ResearchDataset, ResearchJobStatus
from .research_campaign import ResearchCampaignReport
from .research_hypothesis import generate_failure_driven_revisions
from .research_lifecycle import (
    CandidateStatus,
    attach_backtest,
    compare_for_promotion,
    compile_champion,
    create_candidate,
)
from .strategy_portfolio import StrategyHealthReport, assess_strategy_health
from .strategy_spec import validate_strategy_spec
from .timeframes import closed_candles

logger = __import__("logging").getLogger(__name__)


class ResearchCandidatesService:
    async def research_dataset(self, dataset_id: str):
        payload = await asyncio.to_thread(self.store.load_research_dataset, dataset_id)
        if not payload:
            raise HTTPException(404, "research dataset not found")
        return ResearchDataset.model_validate(payload)

    async def queue_research_backtest(self, plan_id: str):
        if plan_id not in self.plans:
            raise HTTPException(404, "strategy plan not found")
        recent = await asyncio.to_thread(self.store.list_research_jobs, 50)
        for payload in recent:
            existing = BacktestJob.model_validate(payload)
            if existing.plan_id == plan_id and existing.status in {
                ResearchJobStatus.QUEUED,
                ResearchJobStatus.RUNNING,
            }:
                return existing
        job = BacktestJob(id=f"btj_{uuid4().hex[:16]}", plan_id=plan_id)
        await asyncio.to_thread(self.store.save_research_job, job)
        task = asyncio.create_task(self.run_backtest_job(job), name=f"backtest:{job.id}")
        self.backtest_job_tasks.add(task)
        task.add_done_callback(self.backtest_job_tasks.discard)
        return job

    async def research_backtest_job(self, job_id: str):
        payload = await asyncio.to_thread(self.store.load_research_job, job_id)
        if not payload:
            raise HTTPException(404, "research backtest job not found")
        return BacktestJob.model_validate(payload)

    async def research_backtest_jobs(self, limit: int = 50):
        bounded_limit = min(max(limit, 1), 200)
        payloads = await asyncio.to_thread(self.store.list_research_jobs, bounded_limit)
        return [BacktestJob.model_validate(payload) for payload in payloads]

    async def propose_strategy_candidate(self, request: CandidateRequest):
        try:
            validated = validate_strategy_spec(request.strategy_spec)
            configuration = self.configuration_service.current_configuration()
            context = await asyncio.to_thread(
                self.build_candidate_research_context,
                validated,
                symbol=configuration.default_symbol,
                timeframe=configuration.default_timeframe,
                market_as_of=datetime.now(UTC),
                summary_material={
                    "source": "operator",
                    "spec_fingerprint": validated.fingerprint,
                },
            )
            candidate = create_candidate(
                validated,
                parent_candidate_id=request.parent_candidate_id,
                research_context=context,
            )
        except ValueError as exc:
            raise HTTPException(422, f"candidate strategy spec is invalid: {exc}") from exc
        await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
        self.review_worker_wakeup.set()
        self.add_audit(
            "research",
            "受约束策略候选已登记，尚不可执行",
            candidate_id=candidate.id,
            source=candidate.source,
        )
        return candidate

    async def research_candidates(self, limit: int = 100):
        bounded_limit = min(max(limit, 1), 200)
        payloads = await asyncio.to_thread(self.store.list_strategy_candidates, bounded_limit)
        return [
            candidate
            for payload in payloads
            if (
                candidate := self.parse_strategy_candidate_payload(
                    payload,
                    audit_context="research_candidate_list",
                )
            )
            is not None
        ]

    async def review_strategy_governance(self):
        candidate_payloads, campaign_payloads = await asyncio.gather(
            asyncio.to_thread(self.store.list_strategy_candidates, 500),
            asyncio.to_thread(self.store.list_research_campaigns, 50),
        )
        campaigns = [
            ResearchCampaignReport.model_validate(payload) for payload in campaign_payloads
        ]
        health_reports: list[StrategyHealthReport] = []
        retired: list[str] = []
        for payload in candidate_payloads:
            candidate = self.parse_strategy_candidate_payload(
                payload,
                audit_context="strategy_governance",
            )
            if (
                candidate is None
                or candidate.backtest is None
                or candidate.status not in {CandidateStatus.CHAMPION, CandidateStatus.CHALLENGER}
            ):
                continue
            campaign_candidate = next(
                (
                    aggregate
                    for campaign in campaigns
                    for aggregate in campaign.candidates
                    if aggregate.strategy_fingerprint == candidate.spec_fingerprint
                ),
                None,
            )
            health = assess_strategy_health(
                candidate,
                campaign_candidate=campaign_candidate,
            )
            health_reports.append(health)
            if health.retirement_recommended:
                candidate.status = CandidateStatus.RETIRED
                candidate.execution_artifact = None
                candidate.updated_at = datetime.now(UTC)
                candidate.decision_reasons.extend(health.reasons)
                await asyncio.to_thread(
                    self.store.save_strategy_candidate,
                    candidate,
                )
                retired.append(candidate.id)
        if retired:
            self.add_audit(
                "research",
                "策略治理已自动退休失去多项独立证据的 Champion",
                severity="warning",
                retired_candidate_ids=",".join(retired),
            )
        return StrategyGovernanceReport(
            reviewed=len(health_reports),
            retired_candidate_ids=retired,
            health=health_reports,
        )

    async def evaluate_strategy_candidate(self, candidate_id: str, dataset_id: str):
        candidate_payload, dataset_payload = await asyncio.gather(
            asyncio.to_thread(self.store.load_strategy_candidate, candidate_id),
            asyncio.to_thread(self.store.load_research_dataset, dataset_id),
        )
        if not candidate_payload:
            raise HTTPException(404, "strategy candidate not found")
        if not dataset_payload:
            raise HTTPException(404, "research dataset not found")
        candidate = self.parse_strategy_candidate_payload(
            candidate_payload,
            audit_context="candidate_evaluation",
        )
        if candidate is None:
            raise HTTPException(
                409,
                "strategy candidate uses a stale StrategySpec contract; create a new candidate",
            )
        dataset = ResearchDataset.model_validate(dataset_payload)
        if candidate.status not in {CandidateStatus.PROPOSED, CandidateStatus.REJECTED}:
            raise HTTPException(
                409,
                f"candidate status {candidate.status} cannot be evaluated again",
            )
        candle_payloads, funding_payloads, open_interest_payloads = await asyncio.gather(
            asyncio.to_thread(self.store.load_research_dataset_candles, dataset_id),
            asyncio.to_thread(self.store.load_research_dataset_funding, dataset_id),
            asyncio.to_thread(
                self.store.load_research_dataset_open_interest,
                dataset_id,
            ),
        )
        candles = [Candle.model_validate(payload) for payload in candle_payloads]
        funding_rates = [FundingRatePoint.model_validate(payload) for payload in funding_payloads]
        open_interest = [
            OpenInterestPoint.model_validate(payload) for payload in open_interest_payloads
        ]
        result = await self.backtest_candidate_on_dataset(
            candidate,
            dataset,
            candles,
            funding_rates,
            open_interest,
        )
        candidate = attach_backtest(candidate, dataset.id, result)
        await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
        return candidate

    async def revise_strategy_candidate(
        self,
        candidate_id: str,
        request: CandidateRevisionRequest,
    ):
        payload = await asyncio.to_thread(
            self.store.load_strategy_candidate,
            candidate_id,
        )
        if not payload:
            raise HTTPException(404, "strategy candidate not found")
        candidate = self.parse_strategy_candidate_payload(
            payload,
            audit_context="candidate_revision",
        )
        if candidate is None:
            raise HTTPException(409, "strategy candidate uses a stale contract")
        if candidate.backtest is None:
            raise HTTPException(
                409,
                "strategy candidate must be backtested before failure-driven revision",
            )
        attribution, revisions = generate_failure_driven_revisions(
            candidate,
            max_revisions=request.max_revisions,
        )
        for revision in revisions:
            await asyncio.to_thread(
                self.store.save_strategy_candidate,
                revision.candidate,
            )
        self.add_audit(
            "research",
            "候选策略已完成失败归因并生成单变量修订",
            candidate_id=candidate.id,
            failure_codes=",".join(attribution.codes),
            revision_count=len(revisions),
        )
        return CandidateRevisionResponse(
            parent_candidate_id=candidate.id,
            attribution=attribution,
            revisions=revisions,
        )

    async def promote_strategy_candidate(
        self,
        candidate_id: str,
        request: CandidatePromotionRequest,
    ):
        payload = await asyncio.to_thread(self.store.load_strategy_candidate, candidate_id)
        if not payload:
            raise HTTPException(404, "strategy candidate not found")
        candidate = self.parse_strategy_candidate_payload(
            payload,
            audit_context="candidate_promotion",
        )
        if candidate is None:
            raise HTTPException(
                409,
                "strategy candidate uses a stale StrategySpec contract and cannot be promoted",
            )
        if candidate.status != CandidateStatus.CHALLENGER:
            raise HTTPException(409, "only a validated Challenger can be promoted")
        if request.confirmation != f"PROMOTE {candidate.spec_fingerprint}":
            raise HTTPException(409, "promotion confirmation phrase does not match")
        all_candidates = [
            parsed
            for item in await asyncio.to_thread(self.store.list_strategy_candidates, 200)
            if (
                parsed := self.parse_strategy_candidate_payload(
                    item,
                    audit_context="promotion_champion_lookup",
                )
            )
            is not None
        ]
        champion = next(
            (
                item
                for item in all_candidates
                if item.status == CandidateStatus.CHAMPION
                and item.spec.get("family") == candidate.spec.get("family")
            ),
            None,
        )
        comparison_champion = champion
        comparison_candles: list[Candle] | None = None
        incumbent_retested = False
        drift = None
        if (
            champion
            and candidate.dataset_id
            and (champion.dataset_id != candidate.dataset_id or champion.backtest is None)
        ):
            dataset_payload = await asyncio.to_thread(
                self.store.load_research_dataset,
                candidate.dataset_id,
            )
            if not dataset_payload:
                raise HTTPException(409, "challenger research dataset is unavailable")
            dataset = ResearchDataset.model_validate(dataset_payload)
            candle_payloads, funding_payloads, open_interest_payloads = await asyncio.gather(
                asyncio.to_thread(
                    self.store.load_research_dataset_candles,
                    candidate.dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_funding,
                    candidate.dataset_id,
                ),
                asyncio.to_thread(
                    self.store.load_research_dataset_open_interest,
                    candidate.dataset_id,
                ),
            )
            comparison_candles = [Candle.model_validate(item) for item in candle_payloads]
            funding_rates = [FundingRatePoint.model_validate(item) for item in funding_payloads]
            open_interest = [
                OpenInterestPoint.model_validate(item) for item in open_interest_payloads
            ]
            incumbent_result = await self.backtest_candidate_on_dataset(
                champion,
                dataset,
                comparison_candles,
                funding_rates,
                open_interest,
            )
            comparison_champion = champion.model_copy(
                deep=True,
                update={
                    "dataset_id": dataset.id,
                    "backtest": incumbent_result,
                },
            )
            incumbent_retested = True
        decision = compare_for_promotion(candidate, comparison_champion)
        if incumbent_retested:
            decision.checks["incumbent_retested_on_challenger_dataset"] = True
            decision.reasons.append("现任 Champion 已在 Challenger 的同一不可变数据集上临时复测")
        if decision.approved and candidate.dataset_id:
            if comparison_candles is None:
                candle_payloads = await asyncio.to_thread(
                    self.store.load_research_dataset_candles, candidate.dataset_id
                )
                comparison_candles = [Candle.model_validate(item) for item in candle_payloads]
            try:
                drift = self.detect_return_drift(comparison_candles)
            except ValueError:
                drift = None
            decision.checks["dataset_drift_available"] = drift is not None
            decision.checks["dataset_drift_not_critical"] = (
                drift is not None and drift.status != "critical"
            )
            if drift is None:
                decision.approved = False
                decision.reasons.append("数据集不足以计算漂移，禁止晋升")
            elif drift.status == "critical":
                decision.approved = False
                decision.reasons.append("数据分布漂移达到 critical，禁止自动晋升")
        if decision.approved:
            if champion:
                champion.status = CandidateStatus.RETIRED
                champion.updated_at = datetime.now(UTC)
            candidate.status = CandidateStatus.CHAMPION
            candidate.updated_at = datetime.now(UTC)
            candidate.decision_reasons.extend(decision.reasons)
            if candidate.source in {"ai", "ml"}:
                dataset_payload = await asyncio.to_thread(
                    self.store.load_research_dataset,
                    candidate.dataset_id,
                )
                if not dataset_payload or drift is None:
                    raise HTTPException(
                        409,
                        "AI/ML Champion 缺少数据集或漂移证据，无法编译执行工件。",
                    )
                dataset = ResearchDataset.model_validate(dataset_payload)
                try:
                    candidate.execution_artifact = compile_champion(
                        candidate,
                        dataset_id=dataset.id,
                        data_fingerprint=dataset.data_fingerprint,
                        drift_status=drift.status,
                    )
                except ValueError as exc:
                    raise HTTPException(409, f"AI/ML Champion 执行工件编译失败：{exc}") from exc
            try:
                await asyncio.to_thread(
                    self.store.promote_strategy_candidate,
                    candidate,
                    champion,
                )
            except RuntimeError as exc:
                raise HTTPException(409, str(exc)) from exc
            self.add_audit(
                "research",
                "研究候选晋升为 Champion；仅作为离线研究证据保留",
                candidate_id=candidate.id,
            )
        return decision

    async def create_ml_challenger(self):
        configuration = self.configuration_service.current_configuration()
        try:
            market, history = await self.runtime.market_context(
                limit=configuration.strategy.research_bars
            )
        except Exception as exc:
            raise HTTPException(503, f"research market data unavailable: {exc}") from exc
        candles = closed_candles(history, configuration.default_timeframe, market.timestamp)
        report = await asyncio.to_thread(train_ml_challenger, candles)
        if report.accepted and report.candidate_spec:
            validated = validate_strategy_spec(report.candidate_spec)
            context = await asyncio.to_thread(
                self.build_candidate_research_context,
                validated,
                symbol=configuration.default_symbol,
                timeframe=configuration.default_timeframe,
                market_as_of=market.timestamp,
                summary_material={
                    "source": "ml",
                    "candle_count": len(candles),
                    "data_as_of": market.timestamp.isoformat(),
                    "spec_fingerprint": validated.fingerprint,
                },
            )
            candidate = create_candidate(validated, research_context=context)
            await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
            self.review_worker_wakeup.set()
        return report

    async def research_drift(self):
        configuration = self.configuration_service.current_configuration()
        try:
            market, history = await self.runtime.market_context(
                limit=configuration.strategy.research_bars
            )
        except Exception as exc:
            raise HTTPException(503, f"research market data unavailable: {exc}") from exc
        candles = closed_candles(history, configuration.default_timeframe, market.timestamp)
        try:
            return await asyncio.to_thread(self.detect_return_drift, candles)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
