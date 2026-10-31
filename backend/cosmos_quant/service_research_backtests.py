from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial

from anyio import to_process

from .backtest import BacktestResult, run_walk_forward_backtest
from .configuration import StrategyKind
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .research import BacktestJob, ResearchDataset, ResearchJobStatus, verify_research_dataset
from .research_lifecycle import StrategyCandidate

logger = __import__("logging").getLogger(__name__)


class ResearchBacktestsService:
    async def execute_research_backtest(
        self,
        dataset: ResearchDataset,
        candles: list[Candle],
        *,
        cache_key: tuple | None = None,
        positioning: list[PositioningPoint] | None = None,
        basis: list[BasisPoint] | None = None,
        order_flow: list[OrderFlowBar] | None = None,
        liquidation_flow: list[LiquidationFlowPoint] | None = None,
        **kwargs,
    ) -> BacktestResult:
        if cache_key is not None:
            cached = self.dashboard_backtest_cache.get(cache_key)
            if cached is not None:
                return cached
        if positioning is None and dataset.positioning_count:
            positioning_payloads = await asyncio.to_thread(
                self.store.load_research_dataset_positioning,
                dataset.id,
            )
            positioning = [PositioningPoint.model_validate(item) for item in positioning_payloads]
        if basis is None and dataset.basis_count:
            basis_payloads = await asyncio.to_thread(
                self.store.load_research_dataset_basis,
                dataset.id,
            )
            basis = [BasisPoint.model_validate(item) for item in basis_payloads]
        if order_flow is None and dataset.order_flow_count:
            order_flow_payloads = await asyncio.to_thread(
                self.store.load_research_dataset_order_flow,
                dataset.id,
            )
            order_flow = [OrderFlowBar.model_validate(item) for item in order_flow_payloads]
        if liquidation_flow is None and dataset.liquidation_flow_count:
            liquidation_flow_payloads = await asyncio.to_thread(
                self.store.load_research_dataset_liquidation_flow,
                dataset.id,
            )
            liquidation_flow = [
                LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads
            ]
        result = await to_process.run_sync(
            partial(
                run_walk_forward_backtest,
                candles,
                positioning=positioning,
                basis=basis,
                order_flow=order_flow,
                liquidation_flow=liquidation_flow,
                **kwargs,
            ),
            cancellable=True,
            limiter=self.research_process_limiter,
        )
        result.dataset_id = dataset.id
        result.data_fingerprint = dataset.data_fingerprint
        result.data_start = dataset.data_start
        result.data_end = dataset.data_end
        dataset_verification = await to_process.run_sync(
            partial(
                verify_research_dataset,
                dataset,
                candles,
                kwargs.get("funding_rates"),
                kwargs.get("open_interest"),
                positioning,
                basis,
                order_flow,
                liquidation_flow,
            ),
            cancellable=True,
            limiter=self.research_process_limiter,
        )
        immutable_binding_ok = bool(
            result.sample_bars == dataset.candle_count
            and dataset.data_fingerprint
            and dataset.id == f"ds_{dataset.data_fingerprint}"
            and dataset_verification["verified"]
        )
        data_quality_ok = dataset.gap_count == 0 and dataset.invalid_count == 0
        result.integrity_checks.update(
            {
                "research_execution_isolation": "worker_process",
                "research_process_concurrency_limit": self.RESEARCH_PROCESS_CONCURRENCY,
                "immutable_dataset_binding": immutable_binding_ok,
                "dataset_candle_count": dataset.candle_count,
                "dataset_gap_count": dataset.gap_count,
                "dataset_invalid_count": dataset.invalid_count,
                "dataset_quality_acceptable": data_quality_ok,
                "strategy_fingerprint_bound": bool(result.strategy_fingerprint)
                if result.strategy_fingerprint is not None
                else "bound_after_plan_backtest",
                "dataset_fingerprint_recomputed": bool(dataset_verification["fingerprint_matches"]),
                "dataset_time_bounds_match": bool(dataset_verification["bounds_match"]),
                "dataset_strictly_ordered_unique": bool(
                    dataset_verification["strictly_ordered_unique"]
                ),
                "dataset_open_interest_count": dataset.open_interest_count,
                "dataset_open_interest_verified": bool(
                    dataset_verification["open_interest_count_matches"]
                    and dataset_verification["open_interest_strictly_ordered_unique"]
                ),
                "dataset_positioning_count": dataset.positioning_count,
                "dataset_positioning_verified": bool(
                    dataset_verification["positioning_count_matches"]
                    and dataset_verification["positioning_strictly_ordered_unique"]
                    and dataset_verification["positioning_causal"]
                ),
                "dataset_basis_count": dataset.basis_count,
                "dataset_basis_verified": bool(
                    dataset_verification["basis_count_matches"]
                    and dataset_verification["basis_strictly_ordered_unique"]
                    and dataset_verification["basis_causal"]
                ),
                "dataset_order_flow_count": dataset.order_flow_count,
                "dataset_order_flow_verified": bool(
                    dataset_verification["order_flow_count_matches"]
                    and dataset_verification["order_flow_strictly_ordered_unique"]
                    and dataset_verification["order_flow_causal"]
                ),
                "dataset_liquidation_flow_count": dataset.liquidation_flow_count,
                "dataset_liquidation_flow_verified": bool(
                    dataset_verification["liquidation_flow_count_matches"]
                    and dataset_verification["liquidation_flow_strictly_ordered_unique"]
                    and dataset_verification["liquidation_flow_causal"]
                ),
            }
        )
        integrity_reasons: list[str] = []
        if not immutable_binding_ok:
            integrity_reasons.append("不可变数据集指纹或 K 线数量绑定不一致")
        if not data_quality_ok:
            integrity_reasons.append(
                f"研究数据存在 {dataset.gap_count} 个缺口和 {dataset.invalid_count} 根非法 K 线"
            )
        if integrity_reasons:
            result.robustness_passed = False
            result.validation_reasons = list(
                dict.fromkeys([*result.validation_reasons, *integrity_reasons])
            )
        if cache_key is not None:
            if len(self.dashboard_backtest_cache) >= 32:
                self.dashboard_backtest_cache.pop(next(iter(self.dashboard_backtest_cache)))
            self.dashboard_backtest_cache[cache_key] = result
        return result

    async def backtest_candidate_on_dataset(
        self,
        candidate: StrategyCandidate,
        dataset: ResearchDataset,
        candles: list[Candle],
        funding_rates: list[FundingRatePoint],
        open_interest: list[OpenInterestPoint] | None = None,
        *,
        positioning: list[PositioningPoint] | None = None,
        basis: list[BasisPoint] | None = None,
        order_flow: list[OrderFlowBar] | None = None,
        liquidation_flow: list[LiquidationFlowPoint] | None = None,
        research_trial_count: int = 1,
    ) -> BacktestResult:
        """Evaluate one immutable StrategySpec against one immutable dataset."""

        configuration = self.configuration_service.current_configuration()
        result = await self.execute_research_backtest(
            dataset,
            candles,
            initial_equity=configuration.paper_equity,
            fee_bps=configuration.fee_bps,
            slippage_bps=configuration.slippage_bps,
            strategy_kind=StrategyKind(candidate.spec["family"]),
            symbol=dataset.symbol,
            timeframe=dataset.timeframe,
            exchange=dataset.exchange,
            risk_fraction=configuration.risk.risk_per_trade,
            minimum_reward_risk=configuration.risk.minimum_reward_risk,
            allow_short=dataset.market_type == "swap",
            require_confirmation=(
                configuration.strategy.validation.require_higher_timeframe_confirmation
            ),
            validation=configuration.strategy.validation,
            strategy_spec=candidate.spec,
            funding_rates=funding_rates,
            open_interest=open_interest or [],
            positioning=positioning,
            basis=basis,
            order_flow=order_flow,
            liquidation_flow=liquidation_flow,
            research_trial_count=research_trial_count,
            require_regime_family_fit=True,
            maximum_gross_exposure=configuration.risk.max_gross_exposure,
            maximum_leverage=min(
                configuration.risk.max_leverage,
                configuration.contract_leverage,
            ),
        )
        result.strategy_fingerprint = candidate.spec_fingerprint
        return result

    async def run_backtest_job(self, job: BacktestJob) -> None:
        async with self.backtest_job_semaphore:
            job.status = ResearchJobStatus.RUNNING
            job.started_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_research_job, job)
            try:
                job.result = await self.backtest_strategy(job.plan_id)
                job.status = ResearchJobStatus.COMPLETED
            except asyncio.CancelledError:
                job.status = ResearchJobStatus.FAILED
                job.error = "backtest worker interrupted before completion"
                raise
            except Exception as exc:
                job.status = ResearchJobStatus.FAILED
                job.error = str(getattr(exc, "detail", exc))[:500]
            finally:
                job.completed_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_research_job, job)
