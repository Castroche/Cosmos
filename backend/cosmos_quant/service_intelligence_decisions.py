from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import HTTPException

from .ai_gateway import AIProviderError
from .api_contracts import TradingIntelligenceDecisionRequest
from .position_decisions import resolve_position_decision
from .trading_intelligence import (
    DecisionEpisode,
    DeltaContext,
    MarketStateV2,
    ThesisState,
    TradingModeV2,
    WaitRuntimeV2,
    validate_market_state_time,
)
from .trading_intelligence import PlanState as TradingIntelligencePlanState

logger = __import__("logging").getLogger(__name__)


class IntelligenceDecisionsService:
    async def invoke_trading_intelligence(self, request: TradingIntelligenceDecisionRequest):
        self.active_decisions += 1
        token = self.decision_entry_generation.set(self.risk_state_service.entry_generation)
        try:
            return await self._invoke_trading_intelligence(request)
        finally:
            self.decision_entry_generation.reset(token)
            self.active_decisions -= 1

    async def _invoke_trading_intelligence(self, request: TradingIntelligenceDecisionRequest):
        decision_started = datetime.now(UTC)
        state = await asyncio.to_thread(
            self.trading_intelligence_store.latest_market_state, request.symbol
        )
        if not state:
            raise HTTPException(409, "a current MarketState v2 is required")
        try:
            validate_market_state_time(state)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        active_theses = await asyncio.to_thread(
            self.trading_intelligence_store.list_active_theses, request.symbol
        )
        if request.trigger == "NO_ACTIVE_THESIS" and active_theses:
            raise HTTPException(409, "an active thesis already exists")
        trigger_event = None
        if request.trigger != "NO_ACTIVE_THESIS":
            if not request.trigger_event_id:
                raise HTTPException(400, "this trigger requires a durable event id")
            trigger_event = await asyncio.to_thread(
                self.trading_intelligence_store.get_event,
                request.trigger_event_id,
            )
            if not trigger_event:
                raise HTTPException(409, "trigger event is not present in the durable ledger")
            if request.trigger == "WAKE_TRIGGERED" and trigger_event["event_type"] not in {
                "WAKE_TRIGGERED",
                "WAIT_PLAN_EXPIRED",
            }:
                raise HTTPException(409, "event is not a WakeCondition firing")
        quant = await asyncio.to_thread(
            self.trading_intelligence_store.latest_quant_prediction, request.symbol
        )
        quant_model_eligible = False
        if quant is not None:
            try:
                quant_model = await asyncio.to_thread(self.load_quant_v1_model, request.symbol)
                quant_model_eligible = bool(
                    quant_model and self.quant_model_is_eligible(quant_model.artifact)
                )
            except (OSError, ValueError, RuntimeError):
                quant_model_eligible = False
        # Demo/Shadow retain the prediction as counterfactual evidence. Only Live
        # treats model quality plus fresh directional edge as a hard entry gate.
        if request.mode == TradingModeV2.LIVE_AUTHORIZED and not quant_model_eligible:
            quant = None
        try:
            portfolio = await self.runtime.portfolio()
        except Exception as exc:
            raise HTTPException(503, f"portfolio state unavailable: {exc}") from exc
        configuration = self.configuration_service.current_configuration()
        risk_control = self.risk_state_service.get()
        reconciliation = self.runtime.reconciliation_report
        unresolved_reconciliation = bool(
            reconciliation
            and (
                reconciliation.uncertain_order_ids
                or reconciliation.unknown_open_order_ids
                or reconciliation.unknown_position_symbols
                or reconciliation.unprotected_order_ids
            )
        )
        quant_policy = "HARD_GATE" if request.mode == TradingModeV2.LIVE_AUTHORIZED else "ADVISORY"
        previous_plan = next(
            iter(
                await asyncio.to_thread(
                    self.trading_intelligence_store.list_open_plans, request.symbol
                )
            ),
            None,
        )
        previous_state = await asyncio.to_thread(
            self.trading_intelligence_store.previous_market_state,
            request.symbol,
            before_state_hash=state.state_hash,
            before_data_as_of=state.data_as_of,
        )
        previous_wait = None
        if trigger_event and trigger_event["aggregate_type"] in {"plan", "wait_runtime"}:
            artifact = await asyncio.to_thread(
                self.trading_intelligence_store.get_artifact,
                trigger_event["aggregate_id"],
            )
            if artifact and artifact["artifact_type"] == "WaitRuntimeV2":
                previous_wait = WaitRuntimeV2.model_validate(artifact["payload"])

        def changed_paths(before: MarketStateV2 | None, current: MarketStateV2) -> list[str]:
            if before is None:
                return []
            prior_payload = before.model_dump(mode="json", exclude_none=True)
            current_payload = current.model_dump(mode="json", exclude_none=True)
            changes: list[str] = []

            def walk(left, right, prefix: str = "") -> None:
                if len(changes) >= 100:
                    return
                if isinstance(left, dict) and isinstance(right, dict):
                    for key in sorted(set(left) | set(right)):
                        path = f"{prefix}.{key}" if prefix else key
                        if key not in left or key not in right:
                            changes.append(path)
                        else:
                            walk(left[key], right[key], path)
                    return
                if left != right:
                    changes.append(prefix)

            walk(prior_payload, current_payload)
            return changes

        delta = DeltaContext(
            prior_state_hash=previous_state.state_hash if previous_state else None,
            current_state_hash=state.state_hash,
            changed_paths=changed_paths(previous_state, state),
            material_changes=[
                f"event:{request.trigger}",
                f"quant_policy:{quant_policy.lower()}",
                f"quant_prediction:{'available' if quant is not None else 'unavailable'}",
                f"quant_model_live_eligible:{str(quant_model_eligible).lower()}",
            ],
            trigger_type=request.trigger,
            trigger_evidence=(
                {
                    "event_id": trigger_event["event_id"],
                    "event_type": trigger_event["event_type"],
                    "aggregate_type": trigger_event["aggregate_type"],
                    "aggregate_id": trigger_event["aggregate_id"],
                    "aggregate_version": trigger_event["aggregate_version"],
                    "payload": trigger_event["payload"],
                }
                if trigger_event
                else None
            ),
        )

        async def decision_error_response(
            message: str,
            decision_result=None,
        ) -> dict:
            error_message = message.removeprefix("CONTRACT_ERROR:").strip()
            if decision_result is not None:
                rejected = decision_result.episode.model_copy(deep=True)
                rejected.status = "DECISION_ERROR"
                rejected.trigger_event_id = request.trigger_event_id
                rejected.outcome = {
                    "error_code": "CONTRACT_OR_STATE_ERROR",
                    "summary": error_message[:2000],
                    "trigger": request.trigger,
                }
                rejected.output_hashes = {
                    **rejected.output_hashes,
                    "rejection": sha256(message.encode()).hexdigest(),
                }
                await asyncio.to_thread(
                    self.trading_intelligence_store.save_artifact,
                    decision_result.analysis.analysis_id,
                    "MarketAnalysis",
                    decision_result.analysis,
                )
                await asyncio.to_thread(
                    self.trading_intelligence_store.save_artifact,
                    decision_result.decision.decision_id,
                    "PortfolioTraderDecision",
                    decision_result.decision,
                )
            else:
                rejected = DecisionEpisode(
                    symbol=state.symbol,
                    status="DECISION_ERROR",
                    market_state_hash=state.state_hash,
                    delta=delta,
                    quant_prediction_id=quant.prediction_id if quant else None,
                    trigger_event_id=request.trigger_event_id,
                    prompt_versions={
                        "analyst": "market_analyst_v2",
                        "trader": "portfolio_trader_v2",
                        "repair": "schema_repair_once_v2",
                    },
                    outcome={
                        "error_code": "CONTRACT_OR_STATE_ERROR",
                        "summary": error_message[:2000],
                        "trigger": request.trigger,
                    },
                    output_hashes={"rejection": sha256(message.encode()).hexdigest()},
                )
            await asyncio.to_thread(self.trading_intelligence_store.save_episode, rejected)
            return {
                "analysis": decision_result.analysis if decision_result is not None else None,
                "decision": decision_result.decision if decision_result is not None else None,
                "episode": rejected,
                "decision_error": True,
            }

        decision_positions = await asyncio.to_thread(self.trading_intelligence_store.list_positions)
        try:
            from .trading_learning import learning_context

            try:
                learned_context = await asyncio.to_thread(
                    learning_context,
                    self.trading_intelligence_store,
                    request.symbol,
                    str(request.mode),
                    as_of=decision_started, regime=str(state.regime),
                )
            except Exception:
                logger.exception("Trading learning unavailable; continuing without advisory memory")
                learned_context = {"policy": "ADVISORY_ONLY", "groups": [], "status": "UNAVAILABLE"}
            result = await self.dual_llm_decision_service.decide(
                market_state=state,
                delta=delta,
                quant=quant,
                portfolio=self.v2_llm_portfolio_context(
                    portfolio,
                    symbol=request.symbol,
                    max_daily_drawdown=configuration.risk.max_daily_drawdown,
                    risk_halted=risk_control.halted,
                    unresolved_reconciliation=unresolved_reconciliation,
                    managed_positions=decision_positions,
                ),
                previous_thesis=(
                    active_theses[0].model_dump(mode="json") if active_theses else None
                ),
                previous_plan=(previous_plan.model_dump(mode="json") if previous_plan else None),
                previous_wait_plan=(
                    previous_wait.wait_plan.model_dump(mode="json") if previous_wait else None
                ),
                previous_market_state=previous_state,
                require_quant_edge=request.mode == TradingModeV2.LIVE_AUTHORIZED,
                minimum_reward_risk=configuration.risk.minimum_reward_risk,
                learning=learned_context,
            )
        except ValueError as exc:
            if str(exc).startswith("CONTRACT_ERROR:"):
                return await decision_error_response(str(exc))
            raise HTTPException(503, str(exc)) from exc
        except AIProviderError as exc:
            raise HTTPException(503, str(exc)) from exc
        try:
            await self.learning_shadow.offer(
                result, mode=str(request.mode), decision_started=decision_started
            )
        except Exception:
            logger.exception("Shadow admission failed; normal decision remains independent")
        if self.decision_entry_generation.get() != self.risk_state_service.entry_generation:
            return await decision_error_response(
                "decision invalidated by a stop while awaiting LLM", result
            )
        if result.decision.action == "WAIT":
            referenced_thesis_id = (
                result.decision.wait_plan.thesis_id if result.decision.wait_plan else None
            )
            valid_thesis_ids = {item.thesis_id for item in active_theses}
            if result.decision.thesis:
                valid_thesis_ids.add(result.decision.thesis.thesis_id)
            if referenced_thesis_id is not None and referenced_thesis_id not in valid_thesis_ids:
                return await decision_error_response(
                    "WAIT thesis_id, when provided, must reference an existing "
                    "or returned active Thesis",
                    result,
                )
        managed_position_context = None
        managed_plan_context = None
        if result.decision.action in {"MANAGE_EXISTING", "EXIT"}:
            active_positions = await asyncio.to_thread(
                self.trading_intelligence_store.list_positions,
                symbol=request.symbol,
            )
            target_id = result.decision.position_id
            target_version = result.decision.position_version
            try:
                managed_position_context = resolve_position_decision(
                    target_id, target_version, decision_positions, active_positions
                )
            except ValueError as exc:
                return await decision_error_response(
                    f"MANAGE_EXISTING/EXIT target rejected: {exc}",
                    result,
                )
            managed_plan_context = await asyncio.to_thread(
                self.trading_intelligence_store.get_plan,
                managed_position_context.plan_id,
            )
            if not managed_plan_context:
                return await decision_error_response(
                    "position references an unavailable TradePlan",
                    result,
                )
        decision_thesis = result.decision.thesis
        if decision_thesis:
            now = datetime.now(UTC)
            maximum_expiry = max(now, state.data_as_of) + timedelta(hours=4)
            if decision_thesis.expires_at <= now:
                return await decision_error_response(
                    "returned Thesis is already expired",
                    result,
                )
            existing_thesis = await asyncio.to_thread(
                self.trading_intelligence_store.get_thesis,
                decision_thesis.thesis_id,
            )
            decision_thesis = decision_thesis.model_copy(
                update={
                    "version": existing_thesis.version if existing_thesis else 1,
                    "state": existing_thesis.state if existing_thesis else ThesisState.CREATED,
                    "created_at": (existing_thesis.created_at if existing_thesis else now),
                    "expires_at": min(decision_thesis.expires_at, maximum_expiry),
                }
            )
            result.decision.thesis = decision_thesis
        if result.decision.trade_plan and await asyncio.to_thread(
            self.trading_intelligence_store.get_plan,
            result.decision.trade_plan.plan_id,
        ):
            return await decision_error_response(
                "returned TradePlan id already exists",
                result,
            )
        await asyncio.to_thread(self.trading_intelligence_store.cancel_active_waits, request.symbol)
        result.episode.trigger_event_id = request.trigger_event_id
        await asyncio.to_thread(
            self.trading_intelligence_store.save_artifact,
            result.analysis.analysis_id,
            "MarketAnalysis",
            result.analysis,
        )
        await asyncio.to_thread(
            self.trading_intelligence_store.save_artifact,
            result.decision.decision_id,
            "PortfolioTraderDecision",
            result.decision,
        )
        if result.decision.thesis:
            existing_thesis = await asyncio.to_thread(
                self.trading_intelligence_store.get_thesis,
                result.decision.thesis.thesis_id,
            )
            if existing_thesis:
                updated_thesis = await asyncio.to_thread(
                    self.trading_intelligence_store.update_thesis_content,
                    result.decision.thesis,
                    expected_version=existing_thesis.version,
                )
                result.decision.thesis = updated_thesis
            else:
                await asyncio.to_thread(
                    self.trading_intelligence_store.save_thesis,
                    result.decision.thesis,
                )
            if not existing_thesis and str(result.decision.thesis.state) == "CREATED":
                await asyncio.to_thread(
                    self.trading_intelligence_store.transition_thesis,
                    result.decision.thesis.thesis_id,
                    expected_version=result.decision.thesis.version,
                    target=ThesisState.ACTIVE,
                )
        if result.decision.trade_plan:
            await asyncio.to_thread(
                self.trading_intelligence_store.save_plan, result.decision.trade_plan
            )
            validated = await asyncio.to_thread(
                self.trading_intelligence_store.transition_plan,
                result.decision.trade_plan.plan_id,
                expected_version=result.decision.trade_plan.version,
                target=TradingIntelligencePlanState.VALIDATED,
            )
            activated = await asyncio.to_thread(
                self.trading_intelligence_store.transition_plan,
                result.decision.trade_plan.plan_id,
                expected_version=validated.version,
                target=TradingIntelligencePlanState.ACTIVE,
            )
            if activated.order_type is None:
                activated = await asyncio.to_thread(
                    self.trading_intelligence_store.transition_plan,
                    activated.plan_id,
                    expected_version=activated.version,
                    target=TradingIntelligencePlanState.WAITING_TRIGGER,
                )
            result.decision.trade_plan = activated
        if result.decision.action in {"MANAGE_EXISTING", "EXIT"}:
            async with self.v2_entry_allocation_lock:
                current_positions = await asyncio.to_thread(
                    self.trading_intelligence_store.list_positions, symbol=request.symbol
                )
                try:
                    managed_position_context = resolve_position_decision(
                        result.decision.position_id,
                        result.decision.position_version,
                        decision_positions,
                        current_positions,
                    )
                except ValueError as exc:
                    return await decision_error_response(
                        f"position changed before management: {exc}", result
                    )
                managed_plan_context = await asyncio.to_thread(
                    self.trading_intelligence_store.get_plan, managed_position_context.plan_id
                )
                assert managed_position_context is not None
                assert managed_plan_context is not None
                managed_position = managed_position_context
                managed_plan = managed_plan_context
                result.episode.position_id = managed_position.position_id
                result.episode.plan_id = managed_plan.plan_id
                result.episode.thesis_id = managed_plan.thesis_id
                if result.decision.action == "MANAGE_EXISTING":
                    proposed = result.decision.management_plan
                    assert proposed is not None
                    # Native TP/SL submitted at entry remain immutable fail-safe protection.
                    # The LLM may tighten deterministic runtime rules, but cannot silently
                    # replace venue protection levels without a commissioned amend protocol.
                    effective_management = proposed.model_copy(
                        update={
                            "hard_stop": managed_plan.management.hard_stop,
                            "take_profits": managed_plan.management.take_profits,
                        }
                    )
                    updated_plan = managed_plan.model_copy(
                        update={"management": effective_management}
                    )
                    updated_plan = await asyncio.to_thread(
                        self.trading_intelligence_store.update_plan_content,
                        updated_plan,
                        expected_version=managed_plan.version,
                    )
                    if effective_management != managed_plan.management:
                        managed_position = await asyncio.to_thread(
                            self.trading_intelligence_store.update_position,
                            managed_position.position_id,
                            expected_version=managed_position.version,
                            updates={"management_version": managed_position.management_version + 1},
                        )
                    await asyncio.to_thread(
                        self.trading_intelligence_store.append_event,
                        dedupe_key=(
                            f"position-management-updated:{managed_position.position_id}:"
                            f"{updated_plan.version}:{result.decision.decision_id}"
                        ),
                        event_type="POSITION_MANAGEMENT_UPDATED",
                        aggregate_type="position",
                        aggregate_id=managed_position.position_id,
                        aggregate_version=managed_position.version,
                        payload={
                            "plan_id": updated_plan.plan_id,
                            "plan_version": updated_plan.version,
                            "native_protection_levels_preserved": True,
                            "decision_position_version": result.decision.position_version,
                            "applied_position_version": managed_position.version,
                        },
                    )
                else:
                    exit_reason = "; ".join(
                        item.reference for item in result.decision.invalidation_evidence
                    )[:2000]
                    updated_position = await asyncio.to_thread(
                        self.trading_intelligence_store.update_position,
                        managed_position.position_id,
                        expected_version=managed_position.version,
                        updates={
                            "exit_requested_at": datetime.now(UTC),
                            "exit_reason": exit_reason or "LLM thesis invalidation",
                        },
                    )
                    await asyncio.to_thread(
                        self.trading_intelligence_store.append_event,
                        dedupe_key=(
                            f"position-exit-requested:{updated_position.position_id}:"
                            f"{updated_position.version}"
                        ),
                        event_type="POSITION_EXIT_REQUESTED",
                        aggregate_type="position",
                        aggregate_id=updated_position.position_id,
                        aggregate_version=updated_position.version,
                        payload={
                            "decision_id": result.decision.decision_id,
                            "invalidation_evidence": [
                                item.model_dump(mode="json")
                                for item in result.decision.invalidation_evidence
                            ],
                        },
                    )
        await asyncio.to_thread(self.trading_intelligence_store.save_episode, result.episode)
        if result.decision.wait_plan:
            await asyncio.to_thread(
                self.trading_intelligence_store.save_wait_runtime,
                WaitRuntimeV2(
                    episode_id=result.episode.episode_id,
                    symbol=request.symbol,
                    wait_plan=result.decision.wait_plan,
                ),
            )
        execution_result = None
        if result.decision.trade_plan and result.decision.trade_plan.order_type is not None:
            if self.decision_entry_generation.get() != self.risk_state_service.entry_generation:
                cancelled = await asyncio.to_thread(
                    self.trading_intelligence_store.transition_plan,
                    result.decision.trade_plan.plan_id,
                    expected_version=result.decision.trade_plan.version,
                    target=TradingIntelligencePlanState.CANCELLED,
                )
                result.decision.trade_plan = cancelled
                return await decision_error_response(
                    "decision invalidated before entry submission", result
                )
            execution_result = await self.execute_automated_trading_intelligence_plan(
                result.decision.trade_plan.plan_id,
                result.episode.episode_id,
                None,
                request.mode,
                plan_version=result.decision.trade_plan.version,
            )
        return {
            "analysis": result.analysis,
            "decision": result.decision,
            "episode": result.episode,
            "execution": execution_result,
        }
