from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from .api_contracts import ResearchUniverseJob
from .entry_profiles import DEFAULT_ENTRY_PROFILE_ID, get_entry_profile
from .liquidation_stream import BinanceLiquidationCollector
from .research import BacktestJob, ResearchJobStatus
from .research_campaign import ResearchCampaignJob, ResearchCampaignJobStatus
from .review_pipeline import (
    StrategyReviewJob,
    StrategyReviewJobStatus,
    should_retry_review_after_restart,
)
from .strategy_library import list_strategy_library, seed_strategy_library_candidates
from .trading_intelligence_automation import TradingIntelligenceAutomation

logger = __import__("logging").getLogger(__name__)


class LifecycleService:
    async def initialize_runtime_on_startup(self) -> bool:
        try:
            await self.runtime.initialize()
            return True
        except Exception as exc:
            self.metrics.increment("runtime_startup_initialization_failures_total")
            self.add_audit(
                "runtime",
                "application startup could not initialize the trading runtime; "
                "entry remains fail-closed and background workers will retry",
                severity="warning",
                error=str(exc)[:300],
            )
            return False

    def ensure_default_entry_profile_timeframes(self) -> list[str]:
        """Add required internal timeframe roles to legacy user configurations."""

        configuration = self.configuration_service.current_configuration()
        required = get_entry_profile(DEFAULT_ENTRY_PROFILE_ID).context_timeframes
        missing = [item for item in required if item not in configuration.strategy.timeframes]
        if not missing:
            return []
        strategy = configuration.strategy.model_copy(
            update={"timeframes": [*configuration.strategy.timeframes, *missing]}
        )
        self.store.save_configuration(configuration.model_copy(update={"strategy": strategy}))
        return missing

    @asynccontextmanager
    async def lifespan(self, _: FastAPI):
        pass
        pass
        stored_configuration = self.configuration_service.current_configuration()
        if (
            stored_configuration.live_trading_enabled
            or stored_configuration.automatic_execution_enabled
        ):
            await asyncio.to_thread(
                self.store.save_configuration,
                stored_configuration.model_copy(
                    update={
                        "live_trading_enabled": False,
                        "automatic_execution_enabled": False,
                    }
                ),
            )
            self.add_audit(
                "trading_intelligence",
                "retired legacy automatic/live configuration switches during v2 startup",
                severity="warning",
            )
        previous_risk_state = self.risk_state_service.get()
        migrated_risk_state = self.risk_state_service.recover_legacy_strategy_governance_halt()
        if previous_risk_state.halted and not migrated_risk_state.halted:
            self.add_audit(
                "risk",
                "application startup migrated obsolete strategy-governance halt "
                "to a strategy-level entry gate",
                severity="warning",
                previous_reason=previous_risk_state.reason,
                previous_source=previous_risk_state.source,
            )
        recovered_position_overflows = await asyncio.to_thread(
            self.trading_intelligence_store.recover_legacy_position_quantity_overflows
        )
        if recovered_position_overflows:
            self.risk_state_service.halt(
                "Recovered invalid v2 position quantities; manual Demo account "
                "reconciliation is required before automated management can resume",
                source="position_integrity",
            )
            self.add_audit(
                "trading_intelligence",
                "Recovered legacy aggregate-position quantity overflow and halted automation",
                severity="critical",
                position_ids=[item["position_id"] for item in recovered_position_overflows],
            )
        seeded_library_entries = await asyncio.to_thread(
            seed_strategy_library_candidates,
            self.store,
        )
        if seeded_library_entries:
            self.add_audit(
                "research",
                "application startup seeded the non-empty public strategy library",
                seeded_entries=seeded_library_entries,
                total_entries=len(list_strategy_library()),
            )
        interrupted_jobs = await asyncio.to_thread(self.store.list_research_jobs, 200)
        for payload in interrupted_jobs:
            job = BacktestJob.model_validate(payload)
            if job.status in {ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING}:
                job.status = ResearchJobStatus.FAILED
                job.error = "application restarted before backtest completed"
                job.completed_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_research_job, job)
        interrupted_campaigns = await asyncio.to_thread(
            self.store.list_research_campaign_jobs,
            200,
        )
        for payload in interrupted_campaigns:
            campaign_job = ResearchCampaignJob.model_validate(payload)
            if campaign_job.status in {
                ResearchCampaignJobStatus.QUEUED,
                ResearchCampaignJobStatus.RUNNING,
            }:
                campaign_job.status = ResearchCampaignJobStatus.QUEUED
                campaign_job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(
                    self.store.save_research_campaign_job,
                    campaign_job,
                )
                self.schedule_research_campaign_job(campaign_job)
        interrupted_universe_jobs = await asyncio.to_thread(
            self.store.list_research_universe_jobs,
            200,
        )
        for payload in interrupted_universe_jobs:
            universe_job = ResearchUniverseJob.model_validate(payload)
            if universe_job.status in {
                ResearchJobStatus.QUEUED,
                ResearchJobStatus.RUNNING,
            }:
                universe_job.status = ResearchJobStatus.QUEUED
                universe_job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(
                    self.store.save_research_universe_job,
                    universe_job,
                )
                self.schedule_research_universe_job(universe_job)
        interrupted_reviews = await asyncio.to_thread(self.store.list_strategy_review_jobs, 1000)
        for payload in interrupted_reviews:
            review_job = StrategyReviewJob.model_validate(payload)
            if review_job.review_contract_version > 1 and should_retry_review_after_restart(
                review_job
            ):
                review_job.status = StrategyReviewJobStatus.QUEUED
                review_job.stage = "queued"
                review_job.error = "应用重启后已从持久化检查点恢复并自动重试"
                review_job.completed_at = None
                review_job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_strategy_review_job, review_job)
        await self.initialize_runtime_on_startup()
        configuration = self.configuration_service.current_configuration()
        self.trading_intelligence_automation = TradingIntelligenceAutomation(
            self.trading_intelligence_store,
            self.trading_intelligence_engine,
            decide=self.invoke_automated_trading_intelligence_decision,
            execute=self.execute_automated_trading_intelligence_plan,
            reconcile=self.reconcile_v2_before_resume,
        )
        if self.env_settings.cosmos_automation_start_paused:
            previous_automation = self.trading_intelligence_automation.runtime()
            self.trading_intelligence_automation.configure(
                enabled=False,
                requested_mode=previous_automation.requested_mode,
                symbols=previous_automation.symbols,
            )
        self.liquidation_collector = BinanceLiquidationCollector(
            self.store,
            symbols=configuration.strategy.watchlist,
            proxy_url=configuration.network_proxy_url,
        )
        self.liquidation_collector.start()
        maintenance = asyncio.create_task(
            self.maintenance_loop(), name="database-backup-maintenance"
        )
        catalog_worker = asyncio.create_task(
            self.catalog_journal_collection_loop(),
            name="market-catalog-journal-collection",
        )
        official_event_worker = asyncio.create_task(
            self.official_event_collection_loop(),
            name="trading-intelligence-official-events",
        )
        market_state_worker = asyncio.create_task(
            self.trading_intelligence_market_state_loop(),
            name="trading-intelligence-market-state",
        )
        self.v2_position_worker_task = asyncio.create_task(
            self.trading_intelligence_position_loop(),
            name="trading-intelligence-position-reconciliation",
        )
        self.review_worker_task = asyncio.create_task(
            self.strategy_review_worker_loop(),
            name="durable-strategy-review",
        )
        await asyncio.to_thread(self.store.learning.recover)
        self.learning_worker_task = asyncio.create_task(
            self.learning_refresh_loop(), name="learning-evidence-refresh"
        )
        if self.trading_intelligence_automation.runtime().enabled:
            self.learning_shadow.start()
        self.critical_workers.clear()
        for task in (official_event_worker, market_state_worker, self.v2_position_worker_task):
            self.critical_workers[task.get_name()] = task
            task.add_done_callback(self.on_critical_worker_exit)
        try:
            if not self.risk_state_service.get().halted:
                await self.trading_intelligence_automation.resume(evaluate_market_states=False)
            yield
        finally:
            await self.learning_shadow.stop()
            if self.learning_worker_task:
                self.learning_worker_task.cancel()
                await asyncio.gather(self.learning_worker_task, return_exceptions=True)
                self.learning_worker_task = None
            self.critical_workers.clear()
            maintenance.cancel()
            catalog_worker.cancel()
            official_event_worker.cancel()
            market_state_worker.cancel()
            if self.v2_position_worker_task is not None:
                self.v2_position_worker_task.cancel()
            if self.review_worker_task is not None:
                self.review_worker_task.cancel()
            for task in list(self.backtest_job_tasks):
                task.cancel()
            for task in list(self.campaign_job_tasks):
                task.cancel()
            for task in list(self.universe_job_tasks):
                task.cancel()
            await asyncio.gather(
                maintenance,
                catalog_worker,
                official_event_worker,
                market_state_worker,
                return_exceptions=True,
            )
            if self.v2_position_worker_task is not None:
                await asyncio.gather(self.v2_position_worker_task, return_exceptions=True)
                self.v2_position_worker_task = None
            if self.review_worker_task is not None:
                await asyncio.gather(self.review_worker_task, return_exceptions=True)
                self.review_worker_task = None
            self.review_worker_health.update(
                status="stopped",
                activity="shutdown",
                current_job_id=None,
            )
            if self.backtest_job_tasks:
                await asyncio.gather(*self.backtest_job_tasks, return_exceptions=True)
            if self.campaign_job_tasks:
                await asyncio.gather(*self.campaign_job_tasks, return_exceptions=True)
            if self.universe_job_tasks:
                await asyncio.gather(*self.universe_job_tasks, return_exceptions=True)
            if self.liquidation_collector is not None:
                await self.liquidation_collector.close()
                self.liquidation_collector = None
            self.trading_intelligence_automation = None
            await self.runtime.close()

    def on_critical_worker_exit(self, task: asyncio.Task) -> None:
        if self.critical_workers.get(task.get_name()) is not task:
            return
        error = None if task.cancelled() else task.exception()
        logger.error("Critical worker exited: %s (%s)", task.get_name(), type(error).__name__)
        self.risk_state_service.halt(
            f"critical background worker stopped: {task.get_name()}",
            source="worker_failure",
        )
