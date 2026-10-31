from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from functools import partial
from hashlib import sha256

from fastapi import HTTPException
from pydantic import BaseModel

from .api_contracts import TradingIntelligenceDecisionRequest, TradingIntelligenceExecuteRequest
from .trading_intelligence import DecisionEpisode, TradingModeV2, validate_market_state_time
from .trading_intelligence import PlanState as TradingIntelligencePlanState
from .trading_intelligence_automation import is_expected_automated_entry_rejection
from .trading_intelligence_engine import (
    DeterministicRiskEngine,
    ExistingPositionRisk,
    resolve_trading_mode,
)
from .trading_intelligence_engine import RiskContext as TradingIntelligenceRiskContext
from .trading_intelligence_engine import RiskLimits as TradingIntelligenceRiskLimits
from .trading_intelligence_engine import configuration_fingerprint as v2_configuration_fingerprint
from .v2_execution import EntryRiskBlocked, V2ExecutionEngine, V2ExecutionResult

logger = __import__("logging").getLogger(__name__)


class IntelligenceExecutionService:
    @asynccontextmanager
    async def v2_submission_guard(self, generation: int, configuration_fingerprint: str):
        async with self.v2_submission_lock:
            if not self.critical_workers or any(
                task.done() for task in self.critical_workers.values()
            ):
                raise EntryRiskBlocked("critical trading workers are not running")
            if (
                generation != self.risk_state_service.entry_generation
                or self.risk_state_service.get().halted
            ):
                raise EntryRiskBlocked("entry invalidated by a risk stop")
            current = self.configuration_service.current_configuration()
            if v2_configuration_fingerprint(current) != configuration_fingerprint:
                raise EntryRiskBlocked("configuration changed before entry submission")
            yield

    async def execute_trading_intelligence_plan(
        self,
        plan_id: str,
        request: TradingIntelligenceExecuteRequest,
    ):
        # Serialize only account allocation/submission. Symbol analysis remains independent.
        # A submitted entry is durable before the next symbol reads its risk reservation.
        token = None
        if self.decision_entry_generation.get() is None:
            token = self.decision_entry_generation.set(self.risk_state_service.entry_generation)
        try:
            async with self.v2_entry_allocation_lock:
                return await self._execute_trading_intelligence_plan(plan_id, request)
        finally:
            if token is not None:
                self.decision_entry_generation.reset(token)

    async def _record_entry_rejection(
        self,
        plan_id: str,
        episode: DecisionEpisode | None,
        *,
        reason: str,
        code: str = "ENTRY_REJECTED",
    ) -> None:
        """Make an entry refusal terminal and keep the decision audit truthful."""

        current_plan = await asyncio.to_thread(self.trading_intelligence_store.get_plan, plan_id)
        rejected_plan = current_plan
        if current_plan and current_plan.state not in {
            TradingIntelligencePlanState.COMPLETED,
            TradingIntelligencePlanState.EXPIRED,
            TradingIntelligencePlanState.INVALIDATED,
            TradingIntelligencePlanState.CANCELLED,
        }:
            rejected_plan = await asyncio.to_thread(
                self.trading_intelligence_store.transition_plan,
                plan_id,
                expected_version=current_plan.version,
                target=TradingIntelligencePlanState.CANCELLED,
            )
        if episode is not None:
            rejected_episode = episode.model_copy(deep=True)
            rejected_episode.status = "ENTRY_REJECTED"
            rejected_episode.outcome = {
                "error_code": code,
                "summary": reason[:2000],
                "plan_state": str(rejected_plan.state) if rejected_plan else None,
            }
            await asyncio.to_thread(self.trading_intelligence_store.save_episode, rejected_episode)
        await asyncio.to_thread(
            self.trading_intelligence_store.append_event,
            dedupe_key=(
                f"entry-execution-rejected:{plan_id}:"
                f"{rejected_plan.version if rejected_plan else 0}:"
                f"{sha256(reason.encode()).hexdigest()}"
            ),
            event_type="ENTRY_EXECUTION_REJECTED",
            aggregate_type="plan",
            aggregate_id=plan_id,
            aggregate_version=rejected_plan.version if rejected_plan else 1,
            payload={
                "reason": reason,
                "error_code": code,
                "episode_id": episode.episode_id if episode else None,
                "plan_state": str(rejected_plan.state) if rejected_plan else None,
            },
        )

    async def _execute_trading_intelligence_plan(
        self,
        plan_id: str,
        request: TradingIntelligenceExecuteRequest,
    ):
        if request.mode == TradingModeV2.LIVE_AUTHORIZED:
            raise HTTPException(
                409, "Live execution is disabled until Live reconciliation is available"
            )
        entry_generation = self.decision_entry_generation.get()
        if entry_generation is None:
            entry_generation = self.risk_state_service.entry_generation
        if entry_generation != self.risk_state_service.entry_generation:
            raise HTTPException(409, "entry invalidated by a risk stop")
        risk_control = self.risk_state_service.get()
        if risk_control.halted:
            raise HTTPException(409, f"global risk halt is active: {risk_control.source}")
        plan = await asyncio.to_thread(self.trading_intelligence_store.get_plan, plan_id)
        if not plan:
            raise HTTPException(404, "Trading Intelligence plan not found")
        immediate = plan.order_type is not None
        episodes = await asyncio.to_thread(
            self.trading_intelligence_store.list_episodes,
            symbol=plan.symbol,
            limit=500,
        )
        episode = next((item for item in episodes if item.episode_id == request.episode_id), None)
        if not episode or episode.plan_id != plan_id:
            raise HTTPException(409, "DecisionEpisode does not own this plan")
        if immediate:
            existing_entry = await asyncio.to_thread(
                self.trading_intelligence_store.entry_for_plan, plan_id
            )
            if existing_entry is not None:
                if existing_entry.episode_id != request.episode_id:
                    raise HTTPException(409, "DecisionEpisode does not own this entry order")
                return {
                    "mode": str(existing_entry.mode),
                    "status": str(existing_entry.status),
                    "plan_id": plan_id,
                    "entry_order": existing_entry.model_dump(mode="json"),
                }
            if request.plan_version != plan.version:
                raise HTTPException(409, "exact current plan_version is required")
        allowed_states = {
            TradingIntelligencePlanState.WAITING_TRIGGER,
            TradingIntelligencePlanState.ARMED,
        }
        if immediate:
            allowed_states = {TradingIntelligencePlanState.ACTIVE}
        if plan.state not in allowed_states:
            raise HTTPException(409, "plan is not waiting for an entry trigger")
        existing_positions = await asyncio.to_thread(
            self.trading_intelligence_store.list_positions,
            symbol=plan.symbol,
        )
        same_direction = [item for item in existing_positions if item.side == plan.side]
        if immediate and same_direction:
            if len(same_direction) != 1 or not any(
                item.position_id == plan.position_id and item.version == plan.position_version
                for item in same_direction
            ):
                raise HTTPException(
                    409, "same-direction add requires exact position_id and position_version"
                )
        elif immediate and plan.position_id is not None:
            raise HTTPException(409, "target position is no longer open")
        elif not immediate and any(
            position.plan_id != plan.plan_id for position in existing_positions
        ):
            raise HTTPException(
                409,
                "an attributable position already exists for this symbol; "
                "a second plan cannot share the venue's aggregate position",
            )
        events = await asyncio.to_thread(
            self.trading_intelligence_store.list_events,
            aggregate_id=plan_id,
            limit=1000,
        )
        wake_event = next(
            (
                item
                for item in events
                if item["event_id"] == request.wake_event_id
                and item["event_type"] == "WAKE_TRIGGERED"
            ),
            None,
        )
        if not immediate and not wake_event:
            raise HTTPException(409, "a durable WakeCondition firing is required")
        state = await asyncio.to_thread(
            self.trading_intelligence_store.latest_market_state, plan.symbol
        )
        if not state:
            raise HTTPException(409, "current MarketState v2 is unavailable")
        try:
            validate_market_state_time(state)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        quant = await asyncio.to_thread(
            self.trading_intelligence_store.latest_quant_prediction, plan.symbol
        )
        configuration_view = self.configuration_service.get()
        configuration = configuration_view.configuration
        fingerprint = v2_configuration_fingerprint(configuration)
        grant = await asyncio.to_thread(self.trading_intelligence_store.active_grant)
        demo_credentials = configuration_view.credential_scopes.get("okx:sandbox")
        demo_available = bool(
            demo_credentials
            and demo_credentials.api_key_configured
            and demo_credentials.secret_configured
            and demo_credentials.passphrase_configured
        )
        actual_mode = resolve_trading_mode(
            requested=request.mode,
            symbol=plan.symbol,
            configuration_fingerprint_value=fingerprint,
            live_grant=grant,
            demo_credentials_available=demo_available,
        )
        adapter = None
        if actual_mode != TradingModeV2.SHADOW:
            credential_scope = (
                "okx:live" if actual_mode == TradingModeV2.LIVE_AUTHORIZED else "okx:sandbox"
            )
            credentials = self.secret_store.get_credentials(credential_scope)
            adapter = self.CcxtExchangeAdapter(
                "okx",
                credentials,
                sandbox=actual_mode == TradingModeV2.OKX_DEMO,
                live_enabled=actual_mode == TradingModeV2.LIVE_AUTHORIZED,
                public_only=False,
                market_type=configuration.market_type,
                margin_mode=configuration.margin_mode,
                contract_leverage=configuration.contract_leverage,
                store=self.store,
                account_id=f"okx:{str(actual_mode).lower()}:swap:v2",
                proxy_url=configuration.network_proxy_url,
                entry_submission_guard=partial(
                    self.v2_submission_guard, entry_generation, fingerprint
                ),
            )
        try:
            if adapter is not None:
                portfolio = await adapter.portfolio()
                market = await adapter.market_snapshot(plan.symbol.removesuffix(":USDT"))
                execution_adapter = adapter
            else:
                market, _ = await self.runtime.market_context(
                    limit=50,
                    symbol=plan.symbol.removesuffix(":USDT"),
                    timeframe="5m",
                )
                portfolio = await self.runtime.portfolio()
                execution_adapter = self.runtime.execution
                if execution_adapter is None:
                    raise RuntimeError("shadow execution runtime is unavailable")
            available_margin = (
                portfolio.available_margin
                if portfolio.available_margin is not None
                else portfolio.equity
            )
            # The MarketState slippage feature is a size-agnostic stress estimate
            # and can sit at its 100 bps cap before RiskAllocation exists. Using it
            # here rejected small Demo orders despite a tight executable spread.
            # Depth remains bounded independently by liquidity_notional_cap.
            estimated_slippage_bps = market.spread_bps
            from .portfolio_risk_v2 import portfolio_risk_snapshot

            managed_positions = await asyncio.to_thread(
                self.trading_intelligence_store.list_positions
            )
            pending_entries = await asyncio.to_thread(
                self.trading_intelligence_store.list_entry_orders,
                active_only=True,
            )
            portfolio_risk = portfolio_risk_snapshot(
                portfolio,
                managed_positions,
                existing_position_stress=configuration.risk.existing_position_stress,
                pending_entries=pending_entries,
                mode=str(actual_mode),
            )
            existing_position_risk = None
            if plan.position_id is not None:
                target = next(
                    (
                        item
                        for item in portfolio_risk["positions"]
                        if item.get("position_id") == plan.position_id
                        and item.get("position_version") == plan.position_version
                    ),
                    None,
                )
                if target is None:
                    raise HTTPException(409, "target position risk context is unavailable")
                existing_position_risk = ExistingPositionRisk(
                    position_id=plan.position_id,
                    position_version=plan.position_version,
                    symbol=plan.symbol,
                    side=plan.side,
                    quantity=target["quantity"],
                    mark_price=target["mark_price"],
                    risk_amount=target["risk_amount"],
                )
            risk_context = TradingIntelligenceRiskContext(
                equity=portfolio.equity,
                available_margin=max(available_margin, Decimal("0.00000001")),
                portfolio_risk_used=portfolio_risk["risk_fraction"],
                daily_drawdown=portfolio.daily_drawdown_fraction,
                open_positions=portfolio_risk["open_positions"],
                estimated_slippage_bps=estimated_slippage_bps,
                existing_position=existing_position_risk,
                liquidity_notional_cap=max(
                    available_margin * configuration.risk.max_leverage * Decimal("0.25"),
                    Decimal("10"),
                ),
                correlation_risk_multiplier=(
                    Decimal("0.5")
                    if any(item["side"] == plan.side for item in portfolio_risk["positions"])
                    else Decimal("1")
                ),
            )
            risk_limits = TradingIntelligenceRiskLimits(
                risk_per_trade=configuration.risk.risk_per_trade,
                max_portfolio_risk=configuration.risk.max_portfolio_risk,
                max_daily_drawdown=configuration.risk.max_daily_drawdown,
                max_open_positions=configuration.risk.max_open_positions,
                max_leverage=configuration.risk.max_leverage,
                max_slippage_bps=configuration.risk.max_spread_bps,
                minimum_reward_risk=configuration.risk.minimum_reward_risk,
            )
            allocation = DeterministicRiskEngine().allocate(
                plan,
                risk_context,
                risk_limits,
                grant=grant,
                live=actual_mode == TradingModeV2.LIVE_AUTHORIZED,
            )
            if adapter is not None:
                adapter.contract_leverage = allocation.leverage
            execution = V2ExecutionEngine(
                execution_adapter,
                self.trading_intelligence_store,
                margin_mode=str(configuration.margin_mode),
                execution_leverage=allocation.leverage,
                demo_credentials_available=demo_available,
            )
            try:
                preflight_mode = await execution.preflight(
                    plan=plan,
                    market_state=state,
                    requested_mode=request.mode,
                    configuration_fingerprint=fingerprint,
                    live_grant=grant,
                    quant=quant,
                )
            except EntryRiskBlocked as exc:
                await self._record_entry_rejection(
                    plan_id,
                    episode,
                    reason=str(exc),
                    code="ENTRY_RISK_BLOCKED",
                )
                await asyncio.to_thread(
                    self.trading_intelligence_store.append_event,
                    dedupe_key=f"entry-execution-blocked:{request.wake_event_id}",
                    event_type="ENTRY_EXECUTION_BLOCKED",
                    aggregate_type="plan",
                    aggregate_id=plan_id,
                    aggregate_version=plan.version,
                    payload={
                        "wake_event_id": request.wake_event_id,
                        "reason": str(exc),
                        "risk_halted": False,
                    },
                )
                return V2ExecutionResult(
                    mode=actual_mode,
                    status="REJECTED",
                    plan_id=plan.plan_id,
                    allocation_id=allocation.allocation_id,
                    reason=str(exc),
                )
            if immediate:
                result = await execution.execute(
                    plan=plan,
                    allocation=allocation,
                    market_state=state,
                    market=market,
                    episode=episode,
                    requested_mode=request.mode,
                    configuration_fingerprint=fingerprint,
                    live_grant=grant,
                    quant=quant,
                    preflight_mode=preflight_mode,
                )
                if actual_mode == TradingModeV2.OKX_DEMO:
                    await asyncio.to_thread(self.reconciliation_protected_plan_ids)
                return result
            armed_plan = plan
            if plan.state == TradingIntelligencePlanState.WAITING_TRIGGER:
                armed_plan = await asyncio.to_thread(
                    self.trading_intelligence_store.transition_plan,
                    plan_id,
                    expected_version=plan.version,
                    target=TradingIntelligencePlanState.ARMED,
                )
            _claim_event_id, claimed = await asyncio.to_thread(
                self.trading_intelligence_store.append_event,
                dedupe_key=f"entry-execution:{request.wake_event_id}",
                event_type="ENTRY_EXECUTION_CLAIMED",
                aggregate_type="plan",
                aggregate_id=plan_id,
                aggregate_version=armed_plan.version,
                payload={
                    "wake_event_id": request.wake_event_id,
                    "episode_id": request.episode_id,
                    "mode": str(actual_mode),
                },
            )
            if not claimed:
                raise RuntimeError("this WakeCondition execution has already been claimed")
            triggered_plan = await asyncio.to_thread(
                self.trading_intelligence_store.transition_plan,
                plan_id,
                expected_version=armed_plan.version,
                target=TradingIntelligencePlanState.TRIGGERED,
            )
            result = await execution.execute(
                plan=triggered_plan,
                allocation=allocation,
                market_state=state,
                market=market,
                episode=episode,
                requested_mode=request.mode,
                configuration_fingerprint=fingerprint,
                live_grant=grant,
                quant=quant,
                preflight_mode=preflight_mode,
            )
            if actual_mode == TradingModeV2.OKX_DEMO:
                await asyncio.to_thread(self.reconciliation_protected_plan_ids)
            return result
        except HTTPException as exc:
            await self._record_entry_rejection(plan_id, episode, reason=str(exc.detail))
            raise
        except (PermissionError, ValueError, RuntimeError) as exc:
            current_plan = await asyncio.to_thread(
                self.trading_intelligence_store.get_plan, plan_id
            )
            if (
                current_plan
                and current_plan.order_type is not None
                and await asyncio.to_thread(
                    self.trading_intelligence_store.entry_for_plan,
                    plan_id,
                )
                is None
            ):
                await self._record_entry_rejection(
                    plan_id,
                    episode,
                    reason=str(exc),
                    code="EXCHANGE_OR_EXECUTION_REJECTED",
                )
                raise HTTPException(409, str(exc)) from exc
            if current_plan and current_plan.state == TradingIntelligencePlanState.TRIGGERED:
                await asyncio.to_thread(
                    self.trading_intelligence_store.append_event,
                    dedupe_key=f"entry-execution-failed:{request.wake_event_id}",
                    event_type="ENTRY_EXECUTION_FAILED",
                    aggregate_type="plan",
                    aggregate_id=plan_id,
                    aggregate_version=current_plan.version,
                    payload={
                        "wake_event_id": request.wake_event_id,
                        "reason_hash": sha256(str(exc).encode()).hexdigest(),
                        "risk_halted": True,
                    },
                )
                self.risk_state_service.halt(
                    "Trading Intelligence v2 entry failed after the durable trigger claim",
                    source="trading_intelligence_v2",
                )
            raise HTTPException(409, str(exc)) from exc
        finally:
            if adapter is not None:
                await adapter.close()

    async def invoke_automated_trading_intelligence_decision(
        self,
        symbol: str,
        trigger: str,
        trigger_event_id: str | None,
        mode: TradingModeV2,
    ) -> dict:
        result = await self.invoke_trading_intelligence(
            TradingIntelligenceDecisionRequest(
                symbol=symbol,
                trigger=trigger,
                trigger_event_id=trigger_event_id,
                mode=mode,
            )
        )
        return result

    async def execute_automated_trading_intelligence_plan(
        self,
        plan_id: str,
        episode_id: str,
        wake_event_id: str | None,
        mode: TradingModeV2,
        *,
        plan_version: int | None = None,
    ) -> dict:
        try:
            result = await self.execute_trading_intelligence_plan(
                plan_id,
                TradingIntelligenceExecuteRequest(
                    episode_id=episode_id,
                    wake_event_id=wake_event_id,
                    plan_version=plan_version,
                    mode=mode,
                ),
            )
        except HTTPException as exc:
            reason = str(exc.detail)
            if not is_expected_automated_entry_rejection(exc.status_code, reason):
                raise
            plan = await asyncio.to_thread(self.trading_intelligence_store.get_plan, plan_id)
            await asyncio.to_thread(
                self.trading_intelligence_store.append_event,
                dedupe_key=f"entry-execution-blocked:{plan_id}:{plan_version}:{episode_id}:{wake_event_id}",
                event_type="ENTRY_EXECUTION_BLOCKED",
                aggregate_type="plan",
                aggregate_id=plan_id,
                aggregate_version=plan.version if plan else 1,
                payload={
                    "wake_event_id": wake_event_id,
                    "reason": reason,
                    "risk_halted": False,
                },
            )
            return {
                "mode": str(mode),
                "status": "REJECTED",
                "plan_id": plan_id,
                "reason": reason,
            }
        return result.model_dump(mode="json") if isinstance(result, BaseModel) else result
