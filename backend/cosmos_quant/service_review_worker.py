from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial

from anyio import to_process

from .backtest import BacktestResult
from .configuration import UserConfiguration
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .entry_profiles import DEFAULT_ENTRY_PROFILE_ID
from .operator_workspace import stable_fingerprint
from .research import ResearchDataset
from .research_lifecycle import CandidateStatus, StrategyCandidate, attach_backtest
from .review_pipeline import (
    ReviewScopeResult,
    StrategyReviewJob,
    StrategyReviewJobStatus,
    review_failure_detail,
    strategy_review_fingerprint,
)
from .timeframes import closed_candles

logger = __import__("logging").getLogger(__name__)


class ReviewWorkerService:
    def strategy_certificate_configuration_fingerprint(
        self,
        configuration: UserConfiguration,
    ) -> str:
        """Bind strategy evidence without making SANDBOX -> LIVE itself a mutation."""

        return stable_fingerprint(
            {
                "scheme": "full-strategy-certificate-config-v2",
                "exchange_id": configuration.exchange_id,
                "market_type": str(configuration.market_type),
                "margin_mode": str(configuration.margin_mode),
                "contract_leverage": str(configuration.contract_leverage),
                "fee_bps": str(configuration.fee_bps),
                "slippage_bps": str(configuration.slippage_bps),
                "risk": configuration.risk.model_dump(mode="json"),
            }
        )

    def reconcile_strategy_review_backlog(
        self, entry_profile: str = DEFAULT_ENTRY_PROFILE_ID
    ) -> dict:
        """Queue v2 candidates while preserving every legacy candidate and review."""

        payloads = self.store.list_strategy_review_candidate_payloads(1000)
        configuration = self.configuration_service.current_configuration()
        contract_version = configuration.strategy.validation.review_contract_version
        policy_fingerprint = self.review_policy_fingerprint(configuration)
        queued = migrated = rejected = existing = legacy = 0
        for payload in payloads:
            context_payload = payload.get("research_context")
            if not isinstance(context_payload, dict):
                legacy += 1
                continue
            candidate_id = str(payload.get("id") or f"legacy_{stable_fingerprint(payload)}")
            candidate = self.parse_strategy_candidate_payload(
                payload,
                audit_context="review_backlog_reconcile",
                emit_audit=False,
            )
            if candidate is None:
                raw_spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else {}
                legacy_fingerprint = str(
                    payload.get("spec_fingerprint") or stable_fingerprint(raw_spec or payload)
                )
                fingerprint = strategy_review_fingerprint(
                    legacy_fingerprint,
                    entry_profile=str(context_payload.get("entry_profile") or entry_profile),
                    candidate_id=candidate_id,
                    symbol=str(context_payload.get("symbol") or "unknown"),
                    timeframe=str(context_payload.get("timeframe") or "unknown"),
                    policy_fingerprint=policy_fingerprint,
                    review_contract_version=contract_version,
                )
                if self.store.load_strategy_review_job_by_fingerprint(fingerprint):
                    existing += 1
                    continue
                now = datetime.now(UTC)
                self.store.save_strategy_review_job(
                    StrategyReviewJob(
                        candidate_id=candidate_id,
                        candidate_fingerprint=legacy_fingerprint,
                        review_fingerprint=fingerprint,
                        review_contract_version=contract_version,
                        policy_fingerprint=policy_fingerprint,
                        origin_symbol=str(context_payload.get("symbol") or "unknown"),
                        origin_timeframe=str(context_payload.get("timeframe") or "unknown"),
                        entry_profile=str(context_payload.get("entry_profile") or entry_profile),
                        status=StrategyReviewJobStatus.REJECTED,
                        stage="contract_validation",
                        evidence_tier="rejected",
                        progress_percent=100,
                        rejection_reasons=["v2 候选无法通过当前 StrategySpec 合约校验"],
                        evidence_refs=[candidate_id],
                        completed_at=now,
                        updated_at=now,
                    )
                )
                rejected += 1
                continue

            if candidate.status in {CandidateStatus.REJECTED, CandidateStatus.RETIRED}:
                existing += 1
                continue

            context = candidate.research_context
            assert context is not None

            fingerprint = strategy_review_fingerprint(
                candidate.spec_fingerprint,
                entry_profile=context.entry_profile,
                candidate_id=candidate.id,
                symbol=context.symbol,
                timeframe=context.timeframe,
                policy_fingerprint=policy_fingerprint,
                review_contract_version=contract_version,
            )
            if self.store.load_strategy_review_job_by_fingerprint(fingerprint):
                existing += 1
                continue
            now = datetime.now(UTC)
            status = StrategyReviewJobStatus.QUEUED
            stage = "queued"
            tier = "unreviewed"
            progress = 0
            reasons: list[str] = []
            completed_at = None
            if candidate.status in {CandidateStatus.CHALLENGER, CandidateStatus.CHAMPION}:
                status = StrategyReviewJobStatus.ADMITTED
                stage = "admitted"
                tier = (
                    "sandbox_mature"
                    if candidate.status == CandidateStatus.CHAMPION
                    else "sandbox_canary"
                )
                progress = 100
                completed_at = now
            elif candidate.status in {CandidateStatus.REJECTED, CandidateStatus.RETIRED}:
                status = StrategyReviewJobStatus.REJECTED
                stage = "rejected"
                tier = "rejected"
                progress = 100
                reasons = candidate.decision_reasons[-8:]
                completed_at = now
            job = StrategyReviewJob(
                candidate_id=candidate.id,
                candidate_fingerprint=candidate.spec_fingerprint,
                review_fingerprint=fingerprint,
                review_contract_version=contract_version,
                policy_fingerprint=policy_fingerprint,
                origin_symbol=context.symbol,
                origin_timeframe=context.timeframe,
                trial_count=context.trial_count,
                entry_profile=context.entry_profile,
                status=status,
                stage=stage,
                evidence_tier=tier,
                progress_percent=progress,
                dataset_id=candidate.dataset_id,
                parent_candidate_id=candidate.parent_candidate_id,
                rejection_reasons=reasons,
                evidence_refs=[item for item in [candidate.dataset_id, candidate.id] if item],
                completed_at=completed_at,
                updated_at=now,
            )
            self.store.save_strategy_review_job(job)
            queued += status == StrategyReviewJobStatus.QUEUED
        if queued or migrated or rejected:
            self.add_audit(
                "review",
                "审核 Agent 已协调全部历史候选并建立持久化队列",
                queued=queued,
                migrated=migrated,
                contract_rejected=rejected,
                existing=existing,
            )
        return {
            "queued": queued,
            "migrated": migrated,
            "contract_rejected": rejected,
            "existing": existing,
            "legacy": legacy,
            "total_candidates": len(payloads),
        }

    def missing_review_dataset_features(
        self,
        dataset: ResearchDataset,
        required_features: list[str],
    ) -> list[str]:
        counts = {
            "funding_rate": dataset.funding_count,
            "open_interest": dataset.open_interest_count,
            "positioning": dataset.positioning_count,
            "basis": dataset.basis_count,
            "order_flow": dataset.order_flow_count,
            "liquidation_flow": dataset.liquidation_flow_count,
        }
        return [
            feature for feature in required_features if feature != "ohlcv" and counts[feature] <= 0
        ]

    async def ensure_strategy_review_dataset(
        self,
        candidate: StrategyCandidate,
        *,
        symbol: str | None = None,
    ) -> ResearchDataset:
        context = candidate.research_context
        if context is None:
            raise RuntimeError("v1 候选只读保留，不能进入 v2 审核")
        review_symbol = symbol or context.symbol
        timeframe = context.timeframe
        fetch_features = any(feature != "ohlcv" for feature in context.required_features)
        existing = [
            ResearchDataset.model_validate(item)
            for item in await asyncio.to_thread(self.store.list_research_datasets, 1000)
            if item.get("symbol") == review_symbol
            and item.get("timeframe") == timeframe
            and item.get("exchange") == context.exchange
            and item.get("market_type") == context.market_type
        ]
        existing.sort(key=lambda item: item.candle_count, reverse=True)
        suitable = [
            item
            for item in existing
            if item.candle_count >= 20000
            and not self.missing_review_dataset_features(item, context.required_features)
        ]
        if suitable:
            return suitable[0]
        try:
            market, history = await self.runtime.market_context(
                limit=20000,
                symbol=review_symbol,
                timeframe=timeframe,
            )
            candles = closed_candles(history, timeframe, market.timestamp)
            if len(candles) < 500:
                raise RuntimeError(f"{timeframe} 审核数据不足 500 根，当前 {len(candles)} 根")
            dataset, *_ = await self.materialize_research_dataset(
                candles,
                exchange=context.exchange,
                market_type=context.market_type,
                symbol=review_symbol,
                timeframe=timeframe,
                quality=self.runtime.market_quality,
                fetch_derivative_features=fetch_features,
            )
            return dataset
        except Exception:
            if existing and existing[0].candle_count >= 500:
                return existing[0]
            raise

    def validate_strategy_review_inputs(
        self,
        candle_payloads: list[dict],
        funding_payloads: list[dict],
        open_interest_payloads: list[dict],
        positioning_payloads: list[dict],
        basis_payloads: list[dict],
    ) -> tuple[
        list[Candle],
        list[FundingRatePoint],
        list[OpenInterestPoint],
        list[PositioningPoint],
        list[BasisPoint],
    ]:
        """Validate large immutable review inputs outside the API event loop."""

        return (
            [Candle.model_validate(item) for item in candle_payloads],
            [FundingRatePoint.model_validate(item) for item in funding_payloads],
            [OpenInterestPoint.model_validate(item) for item in open_interest_payloads],
            [PositioningPoint.model_validate(item) for item in positioning_payloads],
            [BasisPoint.model_validate(item) for item in basis_payloads],
        )

    def review_result_metrics(self, result: BacktestResult, dataset: ResearchDataset) -> dict:
        raw_wins = Decimal("0")
        raw_losses = Decimal("0")
        for episode in result.trade_episodes:
            raw_pnl = episode.pnl + episode.fees + episode.slippage_cost + episode.funding_cost
            if raw_pnl > 0:
                raw_wins += raw_pnl
            elif raw_pnl < 0:
                raw_losses += abs(raw_pnl)
        raw_profit_factor = (
            raw_wins / raw_losses
            if raw_losses > 0
            else Decimal("999.99")
            if raw_wins > 0
            else Decimal("0")
        )
        return {
            "return_percent": str(result.return_percent),
            "max_drawdown_percent": str(result.max_drawdown_percent),
            "profit_factor": str(result.profit_factor),
            "raw_profit_factor": str(raw_profit_factor.quantize(Decimal("0.01"))),
            "sharpe": str(result.sharpe),
            "total_trades": result.total_trades,
            "total_fees": str(result.total_fees),
            "total_slippage": str(result.total_slippage),
            "total_funding": str(result.total_funding),
            "stress_return_percent": str(result.stress_return_percent or Decimal("0")),
            "round_trip_cost_r": str(result.cost_model.get("round_trip_cost_r") or "0"),
            "notional_cap_count": int(result.cost_model.get("notional_cap_count") or 0),
            "gross_exposure_cap_count": int(result.cost_model.get("gross_exposure_cap_count") or 0),
            "leverage_cap_count": int(result.cost_model.get("leverage_cap_count") or 0),
            "volume_cap_count": int(result.cost_model.get("volume_cap_count") or 0),
            "candle_count": dataset.candle_count,
            "robustness_passed": result.robustness_passed,
        }

    async def run_candidate_review_scope(
        self,
        candidate: StrategyCandidate,
        *,
        symbol: str,
        role: str,
        trial_count: int,
    ) -> tuple[BacktestResult | None, ReviewScopeResult]:
        context = candidate.research_context
        assert context is not None
        dataset = await self.ensure_strategy_review_dataset(candidate, symbol=symbol)
        missing = self.missing_review_dataset_features(dataset, context.required_features)
        if missing:
            reasons = [f"缺少审核所需历史特征：{', '.join(missing)}"]
            return None, ReviewScopeResult(
                symbol=symbol,
                timeframe=context.timeframe,
                role=role,
                status="blocked",
                stage="data_required",
                dataset_id=dataset.id,
                dataset_fingerprint=dataset.data_fingerprint,
                reasons=reasons,
                evidence_refs=[dataset.id, dataset.data_fingerprint],
            )
        (
            candle_payloads,
            funding_payloads,
            open_interest_payloads,
            positioning_payloads,
            basis_payloads,
            order_flow_payloads,
            liquidation_flow_payloads,
        ) = await asyncio.gather(
            asyncio.to_thread(self.store.load_research_dataset_candles, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_funding, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_open_interest, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_positioning, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_basis, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_order_flow, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_liquidation_flow, dataset.id),
        )
        candles, funding, open_interest, positioning, basis = await to_process.run_sync(
            partial(
                self.validate_strategy_review_inputs,
                candle_payloads,
                funding_payloads,
                open_interest_payloads,
                positioning_payloads,
                basis_payloads,
            ),
            cancellable=True,
            limiter=self.research_process_limiter,
        )
        order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
        liquidation_flow = [
            LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
        ]
        result = await self.backtest_candidate_on_dataset(
            candidate,
            dataset,
            candles,
            funding,
            open_interest,
            positioning=positioning,
            basis=basis,
            order_flow=order_flow,
            liquidation_flow=liquidation_flow,
            research_trial_count=trial_count,
        )
        return result, ReviewScopeResult(
            symbol=symbol,
            timeframe=context.timeframe,
            role=role,
            status="passed" if result.robustness_passed else "failed",
            stage=(
                "base_and_stress_passed" if result.robustness_passed else "base_or_stress_failed"
            ),
            dataset_id=dataset.id,
            dataset_fingerprint=dataset.data_fingerprint,
            metrics=self.review_result_metrics(result, dataset),
            reasons=result.validation_reasons[:12],
            evidence_refs=[dataset.id, dataset.data_fingerprint],
        )

    def cross_market_review_passed(
        self,
        results: list[BacktestResult],
        *,
        minimum_passes: int,
        minimum_profit_factor: Decimal,
        maximum_drawdown_percent: Decimal,
    ) -> tuple[bool, dict, list[str]]:
        wins = Decimal("0")
        losses = Decimal("0")
        for result in results:
            for episode in result.trade_episodes:
                if episode.pnl > 0:
                    wins += episode.pnl
                elif episode.pnl < 0:
                    losses += abs(episode.pnl)
        combined_pf = (
            wins / losses if losses > 0 else Decimal("999.99") if wins > 0 else Decimal("0")
        )
        passed_count = sum(result.robustness_passed for result in results)
        combined_stress = sum(
            (result.stress_return_percent or Decimal("0") for result in results),
            Decimal("0"),
        )
        maximum_drawdown = max(
            (result.max_drawdown_percent for result in results),
            default=Decimal("0"),
        )
        reasons: list[str] = []
        if passed_count < minimum_passes:
            reasons.append(
                f"跨市场仅 {passed_count}/{len(results)} 通过，要求至少 {minimum_passes}"
            )
        if combined_pf < minimum_profit_factor:
            reasons.append(f"跨市场组合 PF {combined_pf:.2f} 低于 {minimum_profit_factor}")
        if combined_stress <= 0:
            reasons.append("跨市场组合压力收益不为正")
        if maximum_drawdown > maximum_drawdown_percent:
            reasons.append(
                f"跨市场最大回撤 {maximum_drawdown:.2f}% 超过 {maximum_drawdown_percent}%"
            )
        metrics = {
            "cross_market_passed_count": passed_count,
            "cross_market_scope_count": len(results),
            "combined_profit_factor": str(combined_pf.quantize(Decimal("0.01"))),
            "combined_stress_return_percent": str(combined_stress.quantize(Decimal("0.01"))),
            "cross_market_max_drawdown_percent": str(maximum_drawdown),
        }
        return not reasons, metrics, reasons

    async def process_strategy_review_job(self, job: StrategyReviewJob) -> StrategyReviewJob:
        now = datetime.now(UTC)
        job.status = StrategyReviewJobStatus.RUNNING
        job.stage = "binding_dataset"
        job.progress_percent = 10
        job.started_at = job.started_at or now
        job.updated_at = now
        job.attempt_count += 1
        job.error = None
        await asyncio.to_thread(self.store.save_strategy_review_job, job)
        try:
            candidate_payload = await asyncio.to_thread(
                self.store.load_strategy_candidate,
                job.candidate_id,
            )
            if not candidate_payload:
                raise RuntimeError("候选记录不存在")
            candidate = StrategyCandidate.model_validate(candidate_payload)
            context = candidate.research_context
            if context is None or job.review_contract_version <= 1:
                raise RuntimeError("v1 审核记录只读保留，不允许进入 v2 worker")
            job.trial_count = max(
                job.trial_count,
                await asyncio.to_thread(
                    self.current_candidate_trial_count,
                    trial_group=context.trial_group,
                ),
            )
            job.stage = "origin_validation"
            job.progress_percent = 20
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_strategy_review_job, job)
            origin_result, origin_scope = await self.run_candidate_review_scope(
                candidate,
                symbol=context.symbol,
                role="origin",
                trial_count=job.trial_count,
            )
            job.scope_results = [origin_scope]
            job.dataset_id = origin_scope.dataset_id
            job.dataset_fingerprint = origin_scope.dataset_fingerprint
            if origin_result is None:
                job.status = StrategyReviewJobStatus.BLOCKED
                job.stage = "data_required"
                job.evidence_tier = "unreviewed"
                job.progress_percent = 20
                job.error = origin_scope.reasons[0]
                job.rejection_reasons = origin_scope.reasons
                job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_strategy_review_job, job)
                return job
            candidate = attach_backtest(candidate, origin_scope.dataset_id or "", origin_result)
            job.metrics = dict(origin_scope.metrics)
            job.evidence_refs = [candidate.id, *origin_scope.evidence_refs]
            if not origin_result.robustness_passed:
                job.status = StrategyReviewJobStatus.REJECTED
                job.stage = "origin_rejected"
                job.evidence_tier = "rejected"
                job.progress_percent = 100
                job.rejection_reasons = origin_result.validation_reasons[:12]
                job.completed_at = datetime.now(UTC)
                job.updated_at = job.completed_at
                await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
                await asyncio.to_thread(self.store.save_strategy_review_job, job)
                return job

            candidate = candidate.model_copy(
                deep=True,
                update={
                    "status": CandidateStatus.VALIDATED,
                    "updated_at": datetime.now(UTC),
                },
            )
            await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
            job.stage = "cross_market_validation"
            job.evidence_tier = "paper_eligible"
            job.progress_percent = 45
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_strategy_review_job, job)

            target_symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
            cross_results: list[BacktestResult] = []
            blocked_scopes: list[ReviewScopeResult] = []
            for index, target_symbol in enumerate(target_symbols, start=1):
                if target_symbol == context.symbol:
                    cross_results.append(origin_result)
                    continue
                result, scope = await self.run_candidate_review_scope(
                    candidate,
                    symbol=target_symbol,
                    role="cross_market",
                    trial_count=job.trial_count,
                )
                job.scope_results.append(scope)
                job.evidence_refs.extend(scope.evidence_refs)
                if result is None:
                    blocked_scopes.append(scope)
                else:
                    cross_results.append(result)
                job.progress_percent = min(90, 45 + index * 15)
                job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_strategy_review_job, job)

            if blocked_scopes:
                job.status = StrategyReviewJobStatus.BLOCKED
                job.stage = "data_required"
                job.error = "；".join(scope.reasons[0] for scope in blocked_scopes)
                job.rejection_reasons = [
                    reason for scope in blocked_scopes for reason in scope.reasons
                ]
                job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_strategy_review_job, job)
                return job

            validation = self.configuration_service.current_configuration().strategy.validation
            cross_passed, cross_metrics, cross_reasons = self.cross_market_review_passed(
                cross_results,
                minimum_passes=validation.cross_market_minimum_passes,
                minimum_profit_factor=validation.minimum_profit_factor,
                maximum_drawdown_percent=validation.maximum_drawdown_percent,
            )
            job.metrics.update(cross_metrics)
            job.progress_percent = 100
            job.completed_at = datetime.now(UTC)
            job.updated_at = job.completed_at
            job.status = StrategyReviewJobStatus.ADMITTED
            if cross_passed:
                job.status = StrategyReviewJobStatus.ADMITTED
                job.stage = "cross_market_admitted"
                job.evidence_tier = "sandbox_canary"
                candidate = candidate.model_copy(
                    deep=True,
                    update={
                        "status": CandidateStatus.CHALLENGER,
                        "decision_reasons": [
                            *candidate.decision_reasons,
                            "来源市场及跨市场净优势审核通过",
                        ],
                        "updated_at": datetime.now(UTC),
                    },
                )
                await asyncio.to_thread(self.store.save_strategy_candidate, candidate)
            else:
                job.stage = "cross_market_hold"
                job.evidence_tier = "paper_eligible"
                job.rejection_reasons = cross_reasons
        except asyncio.CancelledError:
            job.status = StrategyReviewJobStatus.QUEUED
            job.stage = "queued"
            job.error = "审核进程重启，作业已安全回队"
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_strategy_review_job, job)
            raise
        except Exception as exc:
            job.status = StrategyReviewJobStatus.BLOCKED
            job.stage = "infrastructure_blocked"
            job.error = review_failure_detail(exc)
            job.updated_at = datetime.now(UTC)
            logger.exception(
                "strategy review job %s blocked at stage %s",
                job.id,
                job.stage,
            )
        await asyncio.to_thread(self.store.save_strategy_review_job, job)
        return job

    async def strategy_review_worker_loop(self) -> None:
        self.review_worker_health.update(status="running", activity="starting")
        while True:
            self.review_worker_health["heartbeat_at"] = datetime.now(UTC)
            try:
                await asyncio.to_thread(
                    self.reconcile_strategy_review_backlog,
                    DEFAULT_ENTRY_PROFILE_ID,
                )
                queued_jobs = [
                    StrategyReviewJob.model_validate(item)
                    for item in await asyncio.to_thread(self.store.list_strategy_review_jobs, 1000)
                    if item.get("status") == str(StrategyReviewJobStatus.QUEUED)
                    and int(item.get("review_contract_version") or 1) > 1
                ]
                if queued_jobs:
                    self.review_worker_health.update(
                        activity="validating",
                        current_job_id=queued_jobs[0].id,
                        last_error=None,
                    )
                    await self.process_strategy_review_job(queued_jobs[0])
                else:
                    self.review_worker_health.update(
                        activity="backlog_idle",
                        current_job_id=None,
                        last_error=None,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.review_worker_health.update(
                    activity="degraded",
                    current_job_id=None,
                    last_error=str(exc)[:1000],
                )
                self.add_audit(
                    "review",
                    "离线审核后台循环发生可恢复错误，将自动重试",
                    severity="warning",
                    error=str(exc)[:500],
                )
            try:
                await asyncio.wait_for(self.review_worker_wakeup.wait(), timeout=5)
                self.review_worker_wakeup.clear()
            except TimeoutError:
                pass
