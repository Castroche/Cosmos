from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from .policy import AgentPolicy
from .reasoner import AgentReasoner
from .schemas import (
    AgentAction,
    AgentDecision,
    AgentEvent,
    AgentGoal,
    AgentGoalStatus,
    AgentRun,
    AgentRunStatus,
    AgentState,
    AgentToolCall,
    ToolRiskClass,
)
from .store import AgentStore
from .tools import AgentToolExecutor, AgentToolRegistry

StateBuilder = Callable[[AgentGoal, AgentEvent, AgentRun], AgentState | Awaitable[AgentState]]


async def default_state_builder(goal: AgentGoal, event: AgentEvent, run: AgentRun) -> AgentState:
    return AgentState(
        goal_id=goal.id,
        run_id=run.id,
        phase="triggered",
        market=event.payload if event.event_type.value.startswith("market") else {},
    )


class AgentCoordinator:
    """Durable, bounded Observe-Reason-Act loop for one claimed event."""

    MAX_AUTONOMOUS_REVISION_ROUNDS = 2
    MAX_CAMPAIGN_CELLS = 720

    TERMINAL_ACTIONS = {
        AgentAction.EXECUTE_ENTRY,
        AgentAction.CANCEL_ENTRY,
        AgentAction.REDUCE_POSITION,
        AgentAction.EXIT_POSITION,
        AgentAction.HALT,
        AgentAction.REVIEW,
    }

    def __init__(
        self,
        *,
        store: AgentStore,
        registry: AgentToolRegistry,
        executor: AgentToolExecutor,
        reasoner: AgentReasoner,
        state_builder: StateBuilder = default_state_builder,
        event_lease_seconds: float = 60,
        event_heartbeat_seconds: float | None = None,
    ):
        if event_lease_seconds <= 0:
            raise ValueError("event_lease_seconds must be positive")
        heartbeat_seconds = (
            min(15.0, event_lease_seconds / 3)
            if event_heartbeat_seconds is None
            else event_heartbeat_seconds
        )
        if heartbeat_seconds <= 0 or heartbeat_seconds >= event_lease_seconds:
            raise ValueError("event_heartbeat_seconds must be positive and shorter than the lease")
        self.store = store
        self.registry = registry
        self.executor = executor
        self.reasoner = reasoner
        self.state_builder = state_builder
        self.event_lease_seconds = event_lease_seconds
        self.event_heartbeat_seconds = heartbeat_seconds

    async def process_next(
        self,
        *,
        worker_id: str,
        goal: AgentGoal,
        policy: AgentPolicy,
    ) -> AgentRun | None:
        event = self.store.claim_next_event(
            worker_id,
            goal_id=goal.id,
            lease_seconds=self.event_lease_seconds,
        )
        if event is None:
            return None
        lease_stop = asyncio.Event()
        lease_errors: list[Exception] = []
        lease_task = asyncio.create_task(
            self._maintain_event_lease(
                event_id=event.id,
                worker_id=worker_id,
                stop=lease_stop,
                errors=lease_errors,
            )
        )
        run = self.store.load_open_run_for_event(event.id) or AgentRun(
            goal_id=goal.id,
            trigger_event_id=event.id,
        )
        try:
            self._validate_goal_and_policy(goal, policy)
            if run.state is None:
                built = self.state_builder(goal, event, run)
                if isinstance(built, Awaitable):
                    built = await built
                run.state = built
            self.store.save_run(run)
            observations = self.store.observations_for_run(run.id)
            while run.step_count < run.max_steps:
                self.store.renew_event_lease(event.id, worker_id)
                decision = self._deterministic_review_continuation(
                    run,
                    observations,
                )
                if decision is None:
                    decision = self._deterministic_position_continuation(
                        run,
                        observations,
                    )
                if decision is None:
                    decision = self._deterministic_entry_continuation(
                        run,
                        observations,
                    )
                if decision is None:
                    decision = self._deterministic_research_continuation(
                        run,
                        observations,
                        policy,
                    )
                if decision is None:
                    try:
                        decision = await self.reasoner.decide(
                            goal=goal,
                            event=event,
                            state=run.state,
                            observations=observations,
                            tools=self.registry.descriptions(),
                            step=run.step_count,
                        )
                        if decision.run_id != run.id:
                            raise RuntimeError("reasoner returned a decision for a different run")
                        if decision.valid_until <= datetime.now(UTC):
                            raise RuntimeError("reasoner returned an expired Agent decision")
                    except Exception as reasoner_error:
                        decision = self._deterministic_reasoner_fallback(
                            run,
                            observations,
                            policy,
                            reasoner_error,
                        )
                        if decision is None:
                            raise
                if decision.run_id != run.id:
                    raise RuntimeError("reasoner returned a decision for a different run")
                if decision.valid_until <= datetime.now(UTC):
                    raise RuntimeError("reasoner returned an expired Agent decision")
                self._validate_action_tool(decision.action, decision.tool_call)
                self.store.save_decision(decision)
                self.store.renew_event_lease(event.id, worker_id)
                run.step_count += 1
                if decision.action == AgentAction.WAIT:
                    run.status = AgentRunStatus.WAITING
                    run.completed_at = datetime.now(UTC)
                    self.store.save_run(run)
                    await self._stop_event_lease(lease_stop, lease_task)
                    self._raise_event_lease_error(lease_errors)
                    self.store.finish_event(event.id, worker_id)
                    return run
                assert decision.tool_call is not None
                observation = await self.executor.execute(
                    run_id=run.id,
                    call=decision.tool_call,
                    policy=policy,
                )
                self.store.renew_event_lease(event.id, worker_id)
                observations.append(observation)
                run.state.recent_observation_ids = [
                    *run.state.recent_observation_ids[-19:],
                    observation.id,
                ]
                run.state.memory_refs = list(
                    dict.fromkeys([*run.state.memory_refs, *decision.memory_refs])
                )
                run.state.updated_at = datetime.now(UTC)
                if decision.action in self.TERMINAL_ACTIONS and observation.ok:
                    run.status = (
                        AgentRunStatus.HALTED
                        if decision.action == AgentAction.HALT
                        else AgentRunStatus.COMPLETED
                    )
                    run.completed_at = datetime.now(UTC)
                    self.store.save_run(run)
                    await self._stop_event_lease(lease_stop, lease_task)
                    self._raise_event_lease_error(lease_errors)
                    self.store.finish_event(event.id, worker_id)
                    return run
                self.store.save_run(run)
            run.status = AgentRunStatus.WAITING
            run.last_error = "Agent step budget exhausted; fail-closed to WAIT"
            run.completed_at = datetime.now(UTC)
            self.store.save_run(run)
            await self._stop_event_lease(lease_stop, lease_task)
            self._raise_event_lease_error(lease_errors)
            self.store.finish_event(event.id, worker_id)
            return run
        except Exception as exc:
            await self._stop_event_lease(lease_stop, lease_task)
            if lease_errors:
                exc = RuntimeError(f"{exc}; event lease heartbeat failed: {lease_errors[0]}")
            run.status = AgentRunStatus.FAILED
            run.last_error = str(exc)[:2000]
            run.completed_at = datetime.now(UTC)
            self.store.save_run(run)
            try:
                self.store.finish_event(event.id, worker_id, failed=True)
            except RuntimeError as finish_error:
                run.last_error = (f"{run.last_error}; event finalization failed: {finish_error}")[
                    :2000
                ]
                self.store.save_run(run)
            return run
        finally:
            await self._stop_event_lease(lease_stop, lease_task)

    async def _maintain_event_lease(
        self,
        *,
        event_id: str,
        worker_id: str,
        stop: asyncio.Event,
        errors: list[Exception],
    ) -> None:
        """Keep a claimed event exclusive while reasoning or a tool is running."""

        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self.event_heartbeat_seconds,
                )
            except TimeoutError:
                try:
                    await asyncio.to_thread(
                        self.store.renew_event_lease,
                        event_id,
                        worker_id,
                    )
                except Exception as exc:
                    errors.append(exc)
                    return

    @staticmethod
    async def _stop_event_lease(
        stop: asyncio.Event,
        task: asyncio.Task[None],
    ) -> None:
        stop.set()
        await task

    @staticmethod
    def _raise_event_lease_error(errors: list[Exception]) -> None:
        if errors:
            raise RuntimeError(f"event lease heartbeat failed: {errors[0]}")

    def _deterministic_review_continuation(
        self,
        run: AgentRun,
        observations,
    ) -> AgentDecision | None:
        """Close the verified trade loop without relying on a model response."""

        review_episode = next(
            (item for item in run.state.active_episodes if item.get("status") == "awaiting_review"),
            None,
        )
        if not review_episode:
            return None
        if not observations:
            plan_id = str(review_episode["plan_id"])
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.REVIEW,
                reason=(
                    "交易所仓位与原生保护清理已验证，确定性归因成交、费用和风险倍数并写入回合记忆"
                ),
                evidence=[
                    f"episode_id={review_episode.get('episode_id')}",
                    f"plan_id={plan_id}",
                    "status=awaiting_review",
                ],
                uncertainty=0,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                tool_call=AgentToolCall(
                    name="review_closed_trade",
                    arguments={"plan_id": plan_id},
                    idempotency_key=(f"{run.id}:deterministic:review_closed_trade:{plan_id}"),
                ),
                expected_outcome="回合关闭并生成带成交证据引用的 Episode 记忆",
                abort_conditions=["成交归因不完整或交易所仍有同向仓位"],
            )
        decisions = self.store.decisions_for_run(run.id)
        if not decisions:
            return None
        previous = decisions[-1]
        observation = observations[-1]
        if (
            previous.tool_call is not None
            and previous.tool_call.name == "review_closed_trade"
            and observation.tool_call_id == previous.tool_call.id
            and not observation.ok
        ):
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.WAIT,
                reason=(
                    "平仓复盘证据暂不完整；保持 awaiting_review，"
                    "等待下一轮对账后重试，不伪造绩效记忆"
                ),
                evidence=[observation.summary],
                uncertainty=1,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        return None

    def _deterministic_position_continuation(
        self,
        run: AgentRun,
        observations,
    ) -> AgentDecision | None:
        """Assess an owned position before any same-symbol entry research."""

        if not observations:
            market_symbol = str(run.state.market.get("symbol") or "")
            episode = next(
                (
                    item
                    for item in run.state.active_episodes
                    if item.get("symbol") == market_symbol and item.get("status") == "open"
                ),
                None,
            )
            if not episode:
                return None
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.ANALYZE,
                reason=(
                    "确定性持仓状态机：该币种已有开放回合，"
                    "先复核信号、归因仓位与原生保护，禁止重复开仓"
                ),
                evidence=[
                    f"active_episode={episode.get('episode_id')}",
                    f"plan_id={episode.get('plan_id')}",
                ],
                uncertainty=0,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                tool_call=AgentToolCall(
                    name="assess_owned_position",
                    arguments={
                        "plan_id": str(episode["plan_id"]),
                        "symbol": market_symbol,
                    },
                    idempotency_key=(
                        f"{run.id}:deterministic:assess_owned_position:{episode['plan_id']}"
                    ),
                ),
                expected_outcome="返回持有、退出或停机的确定性持仓建议",
                abort_conditions=["持仓归因或原生保护无法验证"],
            )
        decisions = self.store.decisions_for_run(run.id)
        if not decisions:
            return None
        previous = decisions[-1]
        observation = observations[-1]
        if (
            previous.tool_call is not None
            and previous.tool_call.name == "assess_owned_position"
            and observation.tool_call_id == previous.tool_call.id
            and not observation.ok
        ):
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.WAIT,
                reason=("持仓评估工具暂时不可用；保持原生保护和现有仓位，本回合禁止新开仓"),
                evidence=[observation.summary],
                uncertainty=1,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        if (
            previous.tool_call is None
            or previous.tool_call.name != "assess_owned_position"
            or observation.tool_call_id != previous.tool_call.id
        ):
            return None
        recommendation = str(observation.data.get("recommendation") or "")
        if recommendation in {"hold", "wait"}:
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.WAIT,
                reason=("确定性持仓评估未发现反向信号，且原生保护完整；保持现有仓位，不重复开仓"),
                evidence=[
                    f"recommendation={recommendation}",
                    str(observation.data.get("reason") or ""),
                ],
                uncertainty=0,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        arguments = observation.data.get("execution_arguments")
        if not isinstance(arguments, dict):
            return None
        if recommendation == "exit":
            tool_name = "exit_position"
            action = AgentAction.EXIT_POSITION
        elif recommendation == "halt":
            tool_name = "halt_trading"
            action = AgentAction.HALT
        else:
            return None
        return AgentDecision(
            run_id=run.id,
            action=action,
            reason=(
                f"确定性持仓评估建议 {recommendation}；使用评估工具返回的执行参数，不由模型猜测"
            ),
            evidence=[
                f"recommendation={recommendation}",
                str(observation.data.get("reason") or ""),
            ],
            uncertainty=0,
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
            tool_call=AgentToolCall(
                name=tool_name,
                arguments=dict(arguments),
                idempotency_key=(
                    f"{run.id}:deterministic:{tool_name}:{arguments.get('plan_id', 'risk')}"
                ),
            ),
            expected_outcome=f"{tool_name} 返回可持久化的持仓管理结果",
            abort_conditions=["持仓、保护或执行状态在提交前发生变化"],
        )

    def _deterministic_entry_continuation(
        self,
        run: AgentRun,
        observations,
    ) -> AgentDecision | None:
        """Advance an already selected entry without another model round trip."""

        if not observations:
            return None
        decisions = self.store.decisions_for_run(run.id)
        if not decisions:
            return None
        previous = decisions[-1]
        observation = observations[-1]
        if (
            not observation.ok
            or previous.tool_call is None
            or observation.tool_call_id != previous.tool_call.id
        ):
            return None
        previous_tool = previous.tool_call.name
        data = observation.data
        next_tool: str
        next_action: AgentAction
        arguments: dict
        evidence: list[str]
        if (
            previous_tool == "screen_trade_candidates"
            and data.get("selection_status") in {"robust_candidate", "sandbox_probe_candidate"}
            and isinstance(data.get("selected_authorization"), dict)
        ):
            next_tool = "execute_validated_trade_plan"
            next_action = AgentAction.EXECUTE_ENTRY
            arguments = dict(data["selected_authorization"])
            evidence = [
                f"screen_trade_candidates.selection_status={data.get('selection_status')}",
                "使用筛选工具返回的 selected_authorization 原值",
            ]
        else:
            return None
        plan_id = str(arguments.get("plan_id") or "")
        return AgentDecision(
            run_id=run.id,
            action=next_action,
            reason=(
                f"确定性执行状态机：{previous_tool} 成功后继续 {next_tool}；"
                "不再次依赖模型猜测授权参数"
            ),
            evidence=evidence,
            uncertainty=0,
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
            tool_call=AgentToolCall(
                name=next_tool,
                arguments=arguments,
                idempotency_key=(f"{run.id}:deterministic:{next_tool}:{plan_id}"),
            ),
            expected_outcome=f"{next_tool} 返回可持久化的确定性结果",
            abort_conditions=[
                "工具参数与计划不一致",
                "风险或权限策略拒绝",
                "行情、信号或保护验证退化",
            ],
        )

    def _deterministic_research_continuation(
        self,
        run: AgentRun,
        observations,
        policy: AgentPolicy,
    ) -> AgentDecision | None:
        """Screen healthy actionable signals even when entry context says wait.

        Market analysis owns the current-context execution gate. Candidate
        screening is research-only, so a transient microstructure conflict or
        ambiguity should not prevent backtests and causal shadow evidence from
        being collected. The screening tool preserves the context gate and can
        return an authorization only when that gate is eligible.
        """

        if not observations:
            return None
        decisions = self.store.decisions_for_run(run.id)
        if not decisions:
            return None
        previous = decisions[-1]
        observation = observations[-1]
        available_tools = {item["name"] for item in self.registry.descriptions()}
        if (
            observation.ok
            and previous.tool_call is not None
            and previous.tool_call.name == "prepare_research_universe"
            and observation.tool_call_id == previous.tool_call.id
            and "seal_research_holdout" in available_tools
        ):
            cells = observation.data.get("cells")
            universe_snapshot_id = observation.data.get("universe_snapshot_id")
            dataset_ids = (
                [cell.get("dataset_id") for cell in cells]
                if isinstance(cells, list)
                and all(isinstance(cell, dict) for cell in cells)
                else []
            )
            valid_dataset_ids = all(
                isinstance(dataset_id, str) and dataset_id
                for dataset_id in dataset_ids
            )
            if (
                observation.data.get("materialized_scope_complete")
                and isinstance(universe_snapshot_id, str)
                and universe_snapshot_id
                and dataset_ids
                and valid_dataset_ids
            ):
                return AgentDecision(
                    run_id=run.id,
                    action=AgentAction.RESEARCH,
                    reason=(
                        "Deterministic research continuation: the exact frozen market "
                        "scope is fully materialized, so seal its development and blind "
                        "windows before any return-based strategy selection."
                    ),
                    evidence=[
                        f"universe_snapshot_id={universe_snapshot_id}",
                        f"source_markets={len(dataset_ids)}",
                        "holdout_state=not_yet_opened",
                    ],
                    uncertainty=0,
                    valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    tool_call=AgentToolCall(
                        name="seal_research_holdout",
                        arguments={
                            "dataset_ids": dataset_ids,
                            "universe_snapshot_id": universe_snapshot_id,
                            "development_ratio": 0.80,
                            "embargo_days": 7.0,
                            "common_window": True,
                        },
                        idempotency_key=(
                            f"{run.id}:deterministic:seal_research_holdout:"
                            f"{universe_snapshot_id}"
                        ),
                    ),
                    expected_outcome=(
                        "Return fingerprint-bound development ids and unopened, "
                        "single-use blind ids for the exact frozen universe"
                    ),
                    abort_conditions=[
                        "source datasets do not exactly cover the frozen universe",
                        "timestamps cannot be aligned without gaps",
                        "development, embargo, or blind windows are too short",
                    ],
                )
        if (
            observation.ok
            and previous.tool_call is not None
            and previous.tool_call.name == "seal_research_holdout"
            and observation.tool_call_id == previous.tool_call.id
            and "run_research_campaign" in available_tools
        ):
            dataset_ids = observation.data.get("development_dataset_ids")
            blind_dataset_ids = observation.data.get("blind_dataset_ids")
            universe_snapshot_id = observation.data.get("universe_snapshot_id")
            if (
                isinstance(dataset_ids, list)
                and dataset_ids
                and isinstance(blind_dataset_ids, list)
                and blind_dataset_ids
                and isinstance(universe_snapshot_id, str)
                and universe_snapshot_id
            ):
                return AgentDecision(
                    run_id=run.id,
                    action=AgentAction.RESEARCH,
                    reason=(
                        "Deterministic research continuation: the holdout was sealed "
                        "before strategy selection, so run corrected development research "
                        "and expose blind data only if one candidate clears every gate."
                    ),
                    evidence=[
                        f"blind_set_id={observation.data.get('blind_set_id')}",
                        f"development_markets={len(dataset_ids)}",
                        f"blind_markets={len(blind_dataset_ids)}",
                        f"universe_snapshot_id={universe_snapshot_id}",
                    ],
                    uncertainty=0,
                    valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    tool_call=AgentToolCall(
                        name="run_research_campaign",
                        arguments={
                            "dataset_ids": dataset_ids,
                            "blind_dataset_ids": blind_dataset_ids,
                            "universe_snapshot_id": universe_snapshot_id,
                            "max_backtests": self.MAX_CAMPAIGN_CELLS,
                        },
                        idempotency_key=(
                            f"{run.id}:deterministic:run_research_campaign:"
                            f"{observation.data.get('blind_set_id', 'sealed')}"
                        ),
                    ),
                    expected_outcome=(
                        "Return corrected development evidence and consume blind data "
                        "only for one fully qualified development winner"
                    ),
                    abort_conditions=[
                        "sealed set is already consumed",
                        "dataset integrity or role binding fails",
                        "development evidence cannot nominate one candidate",
                    ],
                )
        if (
            observation.ok
            and previous.tool_call is not None
            and previous.tool_call.name == "screen_trade_candidates"
            and observation.tool_call_id == previous.tool_call.id
        ):
            scope = observation.data.get("campaign_scope")
            scope = scope if isinstance(scope, dict) else {}
            dataset_ids = scope.get("dataset_ids")
            blind_dataset_ids = scope.get("blind_dataset_ids")
            library_entry_ids = observation.data.get("suggested_library_entry_ids")
            if (
                isinstance(dataset_ids, list)
                and dataset_ids
                and isinstance(blind_dataset_ids, list)
                and blind_dataset_ids
                and "run_research_campaign" in available_tools
            ):
                selected_library_ids = (
                    library_entry_ids if isinstance(library_entry_ids, list) else []
                )
                return AgentDecision(
                    run_id=run.id,
                    action=AgentAction.RESEARCH,
                    reason=(
                        "Deterministic research continuation: current-market screening "
                        "cannot authorize entry, so validate its preselected public priors "
                        "on the newest single-use sealed cross-market Campaign."
                    ),
                    evidence=[
                        f"blind_set_id={scope.get('blind_set_id')}",
                        f"development_markets={len(dataset_ids)}",
                        f"blind_markets={len(blind_dataset_ids)}",
                        f"preselected_library_entries={len(selected_library_ids)}",
                    ],
                    uncertainty=0,
                    valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    tool_call=AgentToolCall(
                        name="run_research_campaign",
                        arguments={
                            "dataset_ids": dataset_ids,
                            "blind_dataset_ids": blind_dataset_ids,
                            "library_entry_ids": selected_library_ids,
                            "max_backtests": self.MAX_CAMPAIGN_CELLS,
                        },
                        idempotency_key=(
                            f"{run.id}:deterministic:run_research_campaign:"
                            f"{scope.get('blind_set_id', 'sealed')}"
                        ),
                    ),
                    expected_outcome=(
                        "Return corrected development evidence and, only for one "
                        "qualified winner, a one-time blind PASS/HOLD"
                    ),
                    abort_conditions=[
                        "sealed set is already consumed",
                        "dataset integrity or role binding fails",
                        "development evidence cannot nominate one candidate",
                    ],
                )
        if (
            observation.ok
            and previous.tool_call is not None
            and previous.tool_call.name == "run_research_campaign"
            and observation.tool_call_id == previous.tool_call.id
        ):
            campaign = observation.data
            campaign_id = str(campaign.get("id") or "")
            if campaign.get("blind_consumption_id"):
                return AgentDecision(
                    run_id=run.id,
                    action=AgentAction.WAIT,
                    reason=(
                        "The one-time blind seal has been consumed. This Campaign is "
                        "terminal evidence, so the Agent may not tune or revise against it."
                    ),
                    evidence=[
                        f"campaign_id={campaign_id}",
                        "blind_result=PASS"
                        if campaign.get("blind_selected_candidate_passed")
                        else "blind_result=HOLD",
                        "blind_metrics_not_disclosed",
                    ],
                    uncertainty=0,
                    valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    expected_outcome=(
                        "Wait for a newly sealed independent holdout before any "
                        "successor lineage is researched"
                    ),
                    abort_conditions=[],
                )
            revision_rounds = sum(
                decision.tool_call is not None
                and decision.tool_call.name == "revise_failed_strategy"
                for decision in decisions
            )
            candidates = campaign.get("candidates")
            if (
                revision_rounds >= self.MAX_AUTONOMOUS_REVISION_ROUNDS
                or not campaign_id
                or not isinstance(candidates, list)
                or not candidates
                or "revise_failed_strategy"
                not in {item["name"] for item in self.registry.descriptions()}
            ):
                if revision_rounds >= self.MAX_AUTONOMOUS_REVISION_ROUNDS:
                    return AgentDecision(
                        run_id=run.id,
                        action=AgentAction.WAIT,
                        reason=(
                            "The bounded autonomous revision budget is exhausted. "
                            "Fail closed instead of mining the same development data."
                        ),
                        evidence=[
                            f"revision_rounds={revision_rounds}",
                            f"campaign_id={campaign_id}",
                            "new_independent_data_required",
                        ],
                        uncertainty=0,
                        valid_until=datetime.now(UTC) + timedelta(minutes=5),
                        expected_outcome="Wait for a fresh sealed research generation",
                        abort_conditions=[],
                    )
                return None
            top = candidates[0] if isinstance(candidates[0], dict) else {}
            library_entry_id = str(top.get("library_entry_id") or "")
            if not library_entry_id:
                return None
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.RESEARCH,
                reason=(
                    "The development Campaign rejected every hypothesis without "
                    "opening the blind seal. Attribute the top aggregate failure and "
                    "create bounded single-hypothesis revisions."
                ),
                evidence=[
                    f"campaign_id={campaign_id}",
                    f"library_entry_id={library_entry_id}",
                    f"revision_round={revision_rounds + 1}",
                    "blind_seal=unopened",
                ],
                uncertainty=0,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                tool_call=AgentToolCall(
                    name="revise_failed_strategy",
                    arguments={
                        "campaign_id": campaign_id,
                        "library_entry_id": library_entry_id,
                        "max_revisions": 3,
                    },
                    idempotency_key=(
                        f"{run.id}:deterministic:revise_failed_strategy:"
                        f"{campaign_id}:{library_entry_id}"
                    ),
                ),
                expected_outcome=(
                    "Return at most three explainable child hypotheses with no inherited validation"
                ),
                abort_conditions=[
                    "blind seal was consumed",
                    "campaign lineage is stale",
                    "failure attribution cannot produce a bounded revision",
                ],
            )
        if (
            observation.ok
            and previous.tool_call is not None
            and previous.tool_call.name == "revise_failed_strategy"
            and observation.tool_call_id == previous.tool_call.id
        ):
            revisions = observation.data.get("revisions")
            revision_candidate_ids = (
                [
                    str(item.get("candidate", {}).get("id") or "")
                    for item in revisions
                    if isinstance(item, dict) and isinstance(item.get("candidate"), dict)
                ]
                if isinstance(revisions, list)
                else []
            )
            revision_candidate_ids = [
                candidate_id for candidate_id in revision_candidate_ids if candidate_id
            ]
            campaign_observation = next(
                (
                    item
                    for item in reversed(observations[:-1])
                    if isinstance(item.data, dict)
                    and item.data.get("id")
                    and isinstance(item.data.get("research_dataset_ids"), list)
                    and isinstance(item.data.get("library_entry_ids"), list)
                ),
                None,
            )
            if not revision_candidate_ids or campaign_observation is None:
                return None
            parent_campaign = campaign_observation.data
            research_dataset_ids = list(parent_campaign["research_dataset_ids"])
            blind_dataset_ids = list(parent_campaign.get("blind_dataset_ids") or [])
            tested_entry_ids = list(parent_campaign["library_entry_ids"])
            library_entry_ids = [
                str(entry_id)
                for entry_id in tested_entry_ids
                if not str(entry_id).startswith("candidate:")
            ]
            candidate_ids = [
                str(entry_id).removeprefix("candidate:")
                for entry_id in tested_entry_ids
                if str(entry_id).startswith("candidate:")
            ]
            candidate_ids = list(dict.fromkeys([*candidate_ids, *revision_candidate_ids]))
            maximum_cells = (len(library_entry_ids) + len(candidate_ids)) * len(
                research_dataset_ids
            ) + len(blind_dataset_ids)
            if maximum_cells > self.MAX_CAMPAIGN_CELLS:
                return AgentDecision(
                    run_id=run.id,
                    action=AgentAction.WAIT,
                    reason=(
                        "The cumulative revision lineage exceeds the bounded Campaign "
                        "budget. Fail closed instead of dropping prior trials."
                    ),
                    evidence=[
                        f"required_cells={maximum_cells}",
                        f"maximum_cells={self.MAX_CAMPAIGN_CELLS}",
                        "prior_trials_must_not_be_forgotten",
                    ],
                    uncertainty=0,
                    valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    expected_outcome="Wait for a newly scoped research generation",
                    abort_conditions=[],
                )
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.RESEARCH,
                reason=(
                    "Re-run the complete original library plus every child revision. "
                    "Keeping all prior hypotheses in the matrix preserves cumulative "
                    "multiple-testing correction and PBO evidence."
                ),
                evidence=[
                    f"parent_campaign_id={parent_campaign.get('id')}",
                    f"public_priors={len(library_entry_ids)}",
                    f"cumulative_revisions={len(candidate_ids)}",
                    f"corrected_development_cells={maximum_cells - len(blind_dataset_ids)}",
                ],
                uncertainty=0,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                tool_call=AgentToolCall(
                    name="run_research_campaign",
                    arguments={
                        "dataset_ids": research_dataset_ids,
                        "blind_dataset_ids": blind_dataset_ids,
                        "library_entry_ids": library_entry_ids,
                        "candidate_ids": candidate_ids,
                        "max_backtests": maximum_cells,
                    },
                    idempotency_key=(
                        f"{run.id}:deterministic:run_research_campaign:revisions:"
                        f"{parent_campaign.get('id')}:{len(candidate_ids)}"
                    ),
                ),
                expected_outcome=(
                    "Return a fully corrected cumulative Campaign; open the blind seal "
                    "only if one development winner and PBO both pass"
                ),
                abort_conditions=[
                    "any prior hypothesis would be omitted",
                    "sealed set is already consumed",
                    "cumulative matrix exceeds the bounded budget",
                ],
            )
        if (
            not observation.ok
            or previous.tool_call is None
            or previous.tool_call.name != "analyze_market_regime"
            or observation.tool_call_id != previous.tool_call.id
        ):
            return None
        data = observation.data
        recommendation = data.get("recommendation")
        recommendation = recommendation if isinstance(recommendation, dict) else {}
        actionable = data.get("actionable_candidates")
        if (
            recommendation.get("data_quality_acceptable") is not True
            or not isinstance(actionable, list)
            or not actionable
        ):
            return None
        symbol = str(
            data.get("symbol") or (run.state.market.get("symbol") if run.state else "") or ""
        )
        timeframe = str(
            data.get("timeframe") or (run.state.market.get("timeframe") if run.state else "") or ""
        )
        if (
            symbol not in policy.allowed_symbols
            or timeframe not in policy.allowed_timeframes
            or "screen_trade_candidates"
            not in {item["name"] for item in self.registry.descriptions()}
        ):
            return None
        active_same_symbol = bool(
            run.state
            and any(episode.get("symbol") == symbol for episode in run.state.active_episodes)
        )
        if active_same_symbol:
            return None
        execution_context_eligible = recommendation.get("action") == "consider_plan"
        return AgentDecision(
            run_id=run.id,
            action=AgentAction.RESEARCH,
            reason=(
                "确定性研究状态机：健康行情存在可行动参数候选，"
                "进入统一回测与因果 shadow 验证；"
                + (
                    "当前执行上下文允许继续评估"
                    if execution_context_eligible
                    else "当前执行上下文仅允许研究，禁止授权和下单"
                )
            ),
            evidence=[
                f"actionable_candidates={len(actionable)}",
                f"execution_context_eligible={execution_context_eligible}",
                str(recommendation.get("reason") or ""),
            ],
            uncertainty=0,
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
            tool_call=AgentToolCall(
                name="screen_trade_candidates",
                arguments={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "risk_fraction": str(policy.max_risk_per_trade),
                },
                idempotency_key=(
                    f"{run.id}:deterministic:screen_trade_candidates:{symbol}:{timeframe}"
                ),
            ),
            expected_outcome=("返回同市场候选回测、shadow 证据和独立执行上下文闸门"),
            abort_conditions=[
                "市场数据质量退化",
                "可行动候选消失",
                "该品种出现非终态交易回合",
            ],
        )

    def _deterministic_reasoner_fallback(
        self,
        run: AgentRun,
        observations,
        policy: AgentPolicy,
        reasoner_error: Exception,
    ) -> AgentDecision | None:
        """Fail over to bounded research or WAIT, never directly to execution."""

        state = run.state
        symbol = str(state.market.get("symbol") or "")
        timeframe = str(state.market.get("timeframe") or "")
        error_evidence = f"reasoner_unavailable={str(reasoner_error)[:240]}"
        if state.risk.get("halted"):
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.WAIT,
                reason="AI 推理暂时不可用且全局风险闸已停机；保持等待",
                evidence=[error_evidence, f"risk={state.risk.get('reason', '')}"],
                uncertainty=1,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        active_same_symbol = next(
            (episode for episode in state.active_episodes if episode.get("symbol") == symbol),
            None,
        )
        if active_same_symbol:
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.WAIT,
                reason=(
                    "AI 推理暂时不可用且该品种已有非终态交易回合；"
                    "保持现有订单、仓位与保护，不尝试新开仓"
                ),
                evidence=[
                    error_evidence,
                    f"episode_id={active_same_symbol.get('episode_id')}",
                    f"status={active_same_symbol.get('status')}",
                ],
                uncertainty=1,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        decisions = self.store.decisions_for_run(run.id)
        previous = decisions[-1] if decisions else None
        latest = observations[-1] if observations else None
        if previous and previous.tool_call and latest:
            previous_tool = previous.tool_call.name
            if previous_tool == "screen_trade_candidates":
                return AgentDecision(
                    run_id=run.id,
                    action=AgentAction.WAIT,
                    reason=(
                        "候选筛选没有形成可确定性继续的稳健计划，且 AI 推理暂时不可用；保持等待"
                    ),
                    evidence=[
                        error_evidence,
                        f"selection_status={latest.data.get('selection_status')}",
                        str(latest.data.get("reason") or latest.summary),
                    ],
                    uncertainty=1,
                    valid_until=datetime.now(UTC) + timedelta(minutes=5),
                )
            if previous_tool == "analyze_market_regime":
                recommendation = latest.data.get("recommendation")
                recommendation = recommendation if isinstance(recommendation, dict) else {}
                if recommendation.get("action") != "consider_plan":
                    return AgentDecision(
                        run_id=run.id,
                        action=AgentAction.WAIT,
                        reason=("确定性市场分析未允许构建计划，且 AI 推理暂时不可用；保持等待"),
                        evidence=[
                            error_evidence,
                            str(recommendation.get("reason") or latest.summary),
                        ],
                        uncertainty=1,
                        valid_until=datetime.now(UTC) + timedelta(minutes=5),
                    )
        can_screen = (
            symbol in policy.allowed_symbols
            and timeframe in policy.allowed_timeframes
            and "screen_trade_candidates" in {item["name"] for item in self.registry.descriptions()}
        )
        if not can_screen:
            return AgentDecision(
                run_id=run.id,
                action=AgentAction.WAIT,
                reason=(
                    "AI 推理暂时不可用，当前授权不允许确定性候选筛选；保持等待且不增加账户风险"
                ),
                evidence=[error_evidence],
                uncertainty=1,
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        return AgentDecision(
            run_id=run.id,
            action=AgentAction.RESEARCH,
            reason=(
                "AI 推理暂时不可用；仅降级调用确定性多策略筛选，"
                "后续仍必须通过回测、授权、激活和硬风险闸"
            ),
            evidence=[
                error_evidence,
                f"symbol={symbol}",
                f"timeframe={timeframe}",
                "fallback_scope=research_only",
            ],
            uncertainty=1,
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
            tool_call=AgentToolCall(
                name="screen_trade_candidates",
                arguments={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "risk_fraction": str(policy.max_risk_per_trade),
                },
                idempotency_key=(f"{run.id}:fallback:screen_trade_candidates:{symbol}:{timeframe}"),
            ),
            expected_outcome="返回稳健候选或确定性 WAIT 证据",
            abort_conditions=[
                "市场数据不可用",
                "没有稳健候选",
                "任何权限或风险检查失败",
            ],
        )

    @staticmethod
    def _validate_goal_and_policy(goal: AgentGoal, policy: AgentPolicy) -> None:
        if goal.status != AgentGoalStatus.ACTIVE:
            raise RuntimeError("Agent goal is not active")
        if goal.valid_until and goal.valid_until <= datetime.now(UTC):
            raise RuntimeError("Agent goal has expired")
        if goal.risk_policy_id != policy.id:
            raise RuntimeError("Agent goal is bound to a different risk policy")
        if goal.autonomy_mode != policy.autonomy_mode:
            raise RuntimeError("Agent goal and policy autonomy modes do not match")

    def _validate_action_tool(self, action: AgentAction, call) -> None:
        if action == AgentAction.WAIT:
            return
        if call is None:
            raise RuntimeError(f"Agent action {action} did not include a tool call")
        allowed_tools = {
            AgentAction.OBSERVE: {
                "get_market_context",
                "get_portfolio_state",
                "get_open_orders",
                "search_memory",
            },
            AgentAction.ANALYZE: {
                "analyze_market_regime",
                "assess_owned_position",
                "detect_research_opportunity",
            },
            AgentAction.RESEARCH: {
                "build_trade_plan",
                "screen_trade_candidates",
                "inspect_strategy_library",
                "prepare_research_universe",
                "seal_research_holdout",
                "run_research_campaign",
                "run_relative_value_research",
                "run_relative_value_campaign",
                "revise_failed_strategy",
                "review_strategy_health",
                "propose_hypothesis",
                "propose_strategy_dsl",
                "propose_mutation",
                "review_candidate",
                "summarize_failure",
            },
            AgentAction.BACKTEST: {
                "run_plan_backtest",
                "backtest_causal_strategy_policy",
            },
            AgentAction.PROPOSE_ENTRY: set(),
            AgentAction.EXECUTE_ENTRY: {"execute_validated_trade_plan"},
            AgentAction.CANCEL_ENTRY: {"cancel_entry_order"},
            AgentAction.REDUCE_POSITION: {"reduce_position"},
            AgentAction.EXIT_POSITION: {"exit_position"},
            AgentAction.HALT: {"halt_trading"},
            AgentAction.REVIEW: {"review_closed_trade"},
        }
        if call.name not in allowed_tools[action]:
            raise RuntimeError(f"Agent action {action} cannot invoke tool {call.name}")
        risk_class = self.registry.get(call.name).risk_class
        if action in {AgentAction.OBSERVE, AgentAction.ANALYZE} and (
            risk_class != ToolRiskClass.READ_ONLY
        ):
            raise RuntimeError(f"Agent action {action} may only invoke read-only tools")
        if action in {AgentAction.RESEARCH, AgentAction.BACKTEST} and risk_class not in {
            ToolRiskClass.READ_ONLY,
            ToolRiskClass.RESEARCH,
        }:
            raise RuntimeError(f"Agent action {action} cannot increase account risk")
        if risk_class == ToolRiskClass.INCREASE_RISK and action not in {
            AgentAction.PROPOSE_ENTRY,
            AgentAction.EXECUTE_ENTRY,
        }:
            raise RuntimeError("risk-increasing tools require an entry action")
