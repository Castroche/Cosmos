import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from cosmos_quant.agent.coordinator import AgentCoordinator
from cosmos_quant.agent.policy import AgentPolicy
from cosmos_quant.agent.schemas import (
    AgentAction,
    AgentAutonomyMode,
    AgentDecision,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentRunStatus,
    AgentState,
    AgentToolCall,
    ToolRiskClass,
)
from cosmos_quant.agent.store import AgentStore
from cosmos_quant.agent.tools import AgentTool, AgentToolExecutor, AgentToolRegistry
from cosmos_quant.storage import SqliteStore


class ScriptedReasoner:
    def __init__(self, decisions):
        self.decisions = decisions
        self.calls = 0

    async def decide(self, *, state, **_):
        factory = self.decisions[min(self.calls, len(self.decisions) - 1)]
        decision = factory(state.run_id, self.calls)
        self.calls += 1
        return decision


def _decision(run_id, action, step, tool=None):
    return AgentDecision(
        run_id=run_id,
        action=action,
        reason=f"执行第 {step} 步以收集证据并形成受限决策",
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
        tool_call=(
            AgentToolCall(
                name=tool,
                arguments={"symbol": "BTC/USDT", "timeframe": "1h"},
                idempotency_key=f"{run_id}:{step}:{tool}",
            )
            if tool
            else None
        ),
    )


def _environment(tmp_path, reasoner):
    store = AgentStore(SqliteStore(tmp_path / "coordinator.db"))
    goal = AgentGoal(
        objective="自主分析市场并在证据不足时保持等待",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        risk_policy_id="policy_research",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.CANDLE_CLOSED,
            source="test",
            goal_id=goal.id,
            dedupe_key="BTC/USDT:1h:closed:1",
            payload={"symbol": "BTC/USDT", "timeframe": "1h", "close": "100"},
        )
    )
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="analyze_market_regime",
            description="Analyze the deterministic market regime",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=lambda **_: {"regime": "uncertain"},
        )
    )
    executor = AgentToolExecutor(registry, store)
    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=executor,
        reasoner=reasoner,
    )
    policy = AgentPolicy(
        id="policy_research",
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )
    return store, goal, policy, coordinator


def test_coordinator_runs_multiple_reasoning_steps_then_waits(tmp_path):
    reasoner = ScriptedReasoner(
        [
            lambda run_id, step: _decision(
                run_id, AgentAction.ANALYZE, step, "analyze_market_regime"
            ),
            lambda run_id, step: _decision(run_id, AgentAction.WAIT, step),
        ]
    )
    store, goal, policy, coordinator = _environment(tmp_path, reasoner)

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.WAITING
    assert run.step_count == 2
    observations = store.observations_for_run(run.id)
    assert len(observations) == 1
    assert observations[0].data["regime"] == "uncertain"
    assert coordinator.store.claim_next_event("worker-2") is None


def test_research_flow_deterministically_seals_before_campaign(tmp_path):
    reasoner = ScriptedReasoner(
        [
            lambda run_id, step: _decision(
                run_id,
                AgentAction.RESEARCH,
                step,
                "prepare_research_universe",
            )
        ]
    )
    store, goal, policy, coordinator = _environment(tmp_path, reasoner)
    calls = []

    def prepare_research_universe(**_):
        calls.append("prepare_research_universe")
        return {
            "materialized_scope_complete": True,
            "universe_snapshot_id": "universe_exact",
            "cells": [
                {
                    "exchange": "binance",
                    "symbol": "BTC/USDT",
                    "timeframe": "1h",
                    "dataset_id": "ds_source",
                }
            ],
        }

    def seal_research_holdout(**arguments):
        calls.append("seal_research_holdout")
        assert arguments["dataset_ids"] == ["ds_source"]
        assert arguments["universe_snapshot_id"] == "universe_exact"
        return {
            "blind_set_id": "blindset_exact",
            "universe_snapshot_id": "universe_exact",
            "development_dataset_ids": ["ds_development"],
            "blind_dataset_ids": ["ds_blind"],
        }

    def run_research_campaign(**arguments):
        calls.append("run_research_campaign")
        assert arguments["dataset_ids"] == ["ds_development"]
        assert arguments["blind_dataset_ids"] == ["ds_blind"]
        assert arguments["universe_snapshot_id"] == "universe_exact"
        return {
            "id": "campaign_exact",
            "blind_consumption_id": "blinduse_exact",
            "blind_selected_candidate_passed": False,
        }

    for name, handler in (
        ("prepare_research_universe", prepare_research_universe),
        ("seal_research_holdout", seal_research_holdout),
        ("run_research_campaign", run_research_campaign),
    ):
        coordinator.registry.register(
            AgentTool(
                name=name,
                description=name,
                risk_class=ToolRiskClass.RESEARCH,
                handler=handler,
            )
        )

    run = asyncio.run(
        coordinator.process_next(
            worker_id="research-worker",
            goal=goal,
            policy=policy,
        )
    )

    assert run and run.status == AgentRunStatus.WAITING
    assert reasoner.calls == 1
    assert calls == [
        "prepare_research_universe",
        "seal_research_holdout",
        "run_research_campaign",
    ]
    decisions = store.decisions_for_run(run.id)
    assert [
        decision.tool_call.name if decision.tool_call else str(decision.action)
        for decision in decisions
    ] == [
        "prepare_research_universe",
        "seal_research_holdout",
        "run_research_campaign",
        "wait",
    ]


def test_coordinator_heartbeats_lease_during_long_tool_call(tmp_path):
    async def scenario():
        tool_started = asyncio.Event()
        release_tool = asyncio.Event()

        async def slow_market_analysis(**_):
            tool_started.set()
            await release_tool.wait()
            return {"regime": "uncertain"}

        reasoner = ScriptedReasoner(
            [
                lambda run_id, step: _decision(
                    run_id,
                    AgentAction.ANALYZE,
                    step,
                    "analyze_market_regime",
                ),
                lambda run_id, step: _decision(run_id, AgentAction.WAIT, step),
            ]
        )
        store, goal, policy, coordinator = _environment(tmp_path, reasoner)
        coordinator.registry._tools["analyze_market_regime"] = AgentTool(
            name="analyze_market_regime",
            description="Analyze slowly while the event lease is renewed",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=slow_market_analysis,
        )
        coordinator.event_lease_seconds = 0.06
        coordinator.event_heartbeat_seconds = 0.01

        processing = asyncio.create_task(
            coordinator.process_next(
                worker_id="slow-worker",
                goal=goal,
                policy=policy,
            )
        )
        await asyncio.wait_for(tool_started.wait(), timeout=1)
        await asyncio.sleep(0.09)

        assert (
            store.claim_next_event(
                "competing-worker",
                goal_id=goal.id,
                lease_seconds=0.06,
            )
            is None
        )

        release_tool.set()
        run = await asyncio.wait_for(processing, timeout=1)
        assert run and run.status == AgentRunStatus.WAITING
        assert len(store.decisions_for_run(run.id)) == 2
        assert len(store.observations_for_run(run.id)) == 1

    asyncio.run(scenario())


def test_healthy_actionable_analysis_continues_research_but_not_entry(
    tmp_path,
):
    reasoner = ScriptedReasoner(
        [
            lambda run_id, step: _decision(
                run_id,
                AgentAction.ANALYZE,
                step,
                "analyze_market_regime",
            ),
            lambda run_id, step: _decision(run_id, AgentAction.WAIT, step),
        ]
    )
    store = AgentStore(SqliteStore(tmp_path / "research-context-wait.db"))
    goal = AgentGoal(
        objective="Research healthy BTC candidates without bypassing context gates",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id="policy_sandbox",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key="BTC/USDT:1h:research-context-wait",
            payload={"symbol": "BTC/USDT", "timeframe": "1h"},
        )
    )
    calls = []
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="analyze_market_regime",
            description="Analyze market",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=lambda **_: {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "recommendation": {
                    "action": "wait",
                    "data_quality_acceptable": True,
                    "reason": "strong microstructure conflict",
                },
                "actionable_candidates": [
                    {"adaptive_label": "baseline"},
                    {"adaptive_label": "cost_resilient"},
                ],
            },
        )
    )

    def screen_trade_candidates(**arguments):
        calls.append(arguments)
        return {
            "selection_status": "context_wait",
            "selected_plan_id": None,
            "selected_authorization": None,
            "reason": "research completed; execution context remains blocked",
        }

    registry.register(
        AgentTool(
            name="screen_trade_candidates",
            description="Screen candidates",
            risk_class=ToolRiskClass.RESEARCH,
            handler=screen_trade_candidates,
        )
    )
    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
    )
    policy = AgentPolicy(
        id="policy_sandbox",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.0025",
        allow_agent_entry=True,
    )

    run = asyncio.run(
        coordinator.process_next(
            worker_id="research-context-worker",
            goal=goal,
            policy=policy,
        )
    )

    assert run and run.status == AgentRunStatus.WAITING
    assert run.step_count == 3
    assert reasoner.calls == 2
    assert calls == [
        {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "risk_fraction": "0.0025",
        }
    ]
    decisions = store.decisions_for_run(run.id)
    assert decisions[1].action == AgentAction.RESEARCH
    assert decisions[1].uncertainty == 0
    assert "禁止授权和下单" in decisions[1].reason
    assert decisions[2].action == AgentAction.WAIT


def test_coordinator_fails_closed_when_goal_and_policy_modes_differ(tmp_path):
    reasoner = ScriptedReasoner([lambda run_id, step: _decision(run_id, AgentAction.WAIT, step)])
    _, goal, policy, coordinator = _environment(tmp_path, reasoner)
    policy.autonomy_mode = AgentAutonomyMode.PAPER

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.FAILED
    assert "autonomy modes do not match" in run.last_error
    assert reasoner.calls == 0


def test_disabled_entry_permission_still_allows_sealed_campaign_research(tmp_path):
    reasoner = ScriptedReasoner(
        [
            lambda run_id, step: _decision(
                run_id,
                AgentAction.ANALYZE,
                step,
                "analyze_market_regime",
            ),
            lambda run_id, step: _decision(run_id, AgentAction.WAIT, step),
        ]
    )
    store = AgentStore(SqliteStore(tmp_path / "research-while-entry-disabled.db"))
    goal = AgentGoal(
        objective="Continue sealed research while all new account risk is disabled",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id="research-only-policy",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key="research-while-entry-disabled",
            payload={"symbol": "BTC/USDT", "timeframe": "1h"},
        )
    )
    calls = []
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="analyze_market_regime",
            description="Analyze market",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=lambda **_: {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "recommendation": {
                    "action": "wait",
                    "data_quality_acceptable": True,
                    "reason": "entry globally disabled",
                },
                "actionable_candidates": [{"adaptive_label": "trend"}],
            },
        )
    )

    def screen(**arguments):
        calls.append(("screen", arguments))
        return {
            "selection_status": "wait",
            "selected_authorization": None,
            "campaign_scope": {
                "blind_set_id": "blindset_test",
                "dataset_ids": ["dev_1", "dev_2", "dev_3"],
                "blind_dataset_ids": ["blind_1", "blind_2", "blind_3"],
            },
            "suggested_library_entry_ids": ["builtin_test"],
        }

    def campaign(**arguments):
        calls.append(("campaign", arguments))
        return {
            "id": "campaign_test",
            "autonomous_execution_eligible": False,
            "blind_selected_candidate_passed": False,
        }

    registry.register(
        AgentTool(
            name="screen_trade_candidates",
            description="Screen candidates",
            risk_class=ToolRiskClass.RESEARCH,
            handler=screen,
        )
    )
    registry.register(
        AgentTool(
            name="run_research_campaign",
            description="Run sealed campaign",
            risk_class=ToolRiskClass.RESEARCH,
            handler=campaign,
        )
    )
    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
    )
    policy = AgentPolicy(
        id="research-only-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.0025",
        allow_agent_entry=False,
    )

    run = asyncio.run(
        coordinator.process_next(
            worker_id="research-only-worker",
            goal=goal,
            policy=policy,
        )
    )

    assert run and run.status == AgentRunStatus.WAITING
    assert [name for name, _ in calls] == ["screen", "campaign"]
    assert calls[1][1]["blind_dataset_ids"] == ["blind_1", "blind_2", "blind_3"]
    decisions = store.decisions_for_run(run.id)
    assert decisions[1].tool_call.name == "screen_trade_candidates"
    assert decisions[2].tool_call.name == "run_research_campaign"
    assert reasoner.calls == 2


def test_failed_campaign_revisions_keep_all_prior_trials_in_cumulative_rerun(tmp_path):
    reasoner = ScriptedReasoner(
        [
            lambda run_id, step: _decision(
                run_id,
                AgentAction.ANALYZE,
                step,
                "analyze_market_regime",
            ),
            lambda run_id, step: _decision(run_id, AgentAction.WAIT, step),
        ]
    )
    store = AgentStore(SqliteStore(tmp_path / "cumulative-revision.db"))
    goal = AgentGoal(
        objective="Revise failed research without forgetting prior trials",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        risk_policy_id="cumulative-policy",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key="cumulative-revision",
            payload={"symbol": "BTC/USDT", "timeframe": "1h"},
        )
    )
    calls = []
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="analyze_market_regime",
            description="Analyze market",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=lambda **_: {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "recommendation": {
                    "action": "wait",
                    "data_quality_acceptable": True,
                    "reason": "research only",
                },
                "actionable_candidates": [{"adaptive_label": "trend"}],
            },
        )
    )

    def screen(**arguments):
        calls.append(("screen", arguments))
        return {
            "selection_status": "wait",
            "campaign_scope": {
                "blind_set_id": "blindset_test",
                "dataset_ids": ["dev_1", "dev_2", "dev_3"],
                "blind_dataset_ids": ["blind_1", "blind_2", "blind_3"],
            },
            "suggested_library_entry_ids": ["builtin_test"],
        }

    campaign_calls = 0

    def campaign(**arguments):
        nonlocal campaign_calls
        campaign_calls += 1
        calls.append(("campaign", arguments))
        return {
            "id": f"campaign_{campaign_calls}",
            "research_dataset_ids": ["dev_1", "dev_2", "dev_3"],
            "blind_dataset_ids": ["blind_1", "blind_2", "blind_3"],
            "library_entry_ids": (
                ["builtin_test"]
                if campaign_calls == 1
                else ["builtin_test", "candidate:cand_revision_1"]
            ),
            "candidates": ([{"library_entry_id": "builtin_test"}] if campaign_calls == 1 else []),
            "blind_consumption_id": None,
            "autonomous_execution_eligible": False,
            "blind_selected_candidate_passed": False,
        }

    def revise(**arguments):
        calls.append(("revise", arguments))
        return {
            "parent_candidate_id": "cand_builtin_test",
            "attribution": {"codes": ["low_win_rate"]},
            "revisions": [{"candidate": {"id": "cand_revision_1"}}],
        }

    for tool in (
        AgentTool(
            name="screen_trade_candidates",
            description="Screen candidates",
            risk_class=ToolRiskClass.RESEARCH,
            handler=screen,
        ),
        AgentTool(
            name="run_research_campaign",
            description="Run sealed campaign",
            risk_class=ToolRiskClass.RESEARCH,
            handler=campaign,
        ),
        AgentTool(
            name="revise_failed_strategy",
            description="Revise failed aggregate",
            risk_class=ToolRiskClass.RESEARCH,
            handler=revise,
        ),
    ):
        registry.register(tool)
    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
    )
    policy = AgentPolicy(
        id="cumulative-policy",
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )

    run = asyncio.run(
        coordinator.process_next(
            worker_id="cumulative-worker",
            goal=goal,
            policy=policy,
        )
    )

    assert run and run.status == AgentRunStatus.WAITING
    assert [name for name, _ in calls] == [
        "screen",
        "campaign",
        "revise",
        "campaign",
    ]
    rerun = calls[-1][1]
    assert rerun["library_entry_ids"] == ["builtin_test"]
    assert rerun["candidate_ids"] == ["cand_revision_1"]
    assert rerun["max_backtests"] == 9
    assert reasoner.calls == 2


def test_coordinator_rejects_read_only_tool_under_wrong_action(tmp_path):
    reasoner = ScriptedReasoner(
        [lambda run_id, step: _decision(run_id, AgentAction.ANALYZE, step, "review_closed_trade")]
    )
    store, goal, policy, coordinator = _environment(tmp_path, reasoner)
    coordinator.registry.register(
        AgentTool(
            name="review_closed_trade",
            description="Review a closed trade",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=lambda **_: {"reviewed": True},
        )
    )

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.FAILED
    assert "cannot invoke tool review_closed_trade" in run.last_error
    assert store.observations_for_run(run.id) == []


def test_coordinator_falls_back_to_wait_when_reasoner_decision_expired(tmp_path):
    def expired(run_id, step):
        created_at = datetime.now(UTC) - timedelta(minutes=2)
        return AgentDecision(
            run_id=run_id,
            action=AgentAction.ANALYZE,
            reason=f"第 {step} 步决策已在进入执行边界之前失效",
            created_at=created_at,
            valid_until=created_at + timedelta(minutes=1),
            tool_call=AgentToolCall(
                name="analyze_market_regime",
                arguments={"symbol": "BTC/USDT", "timeframe": "1h"},
                idempotency_key=f"{run_id}:{step}:expired",
            ),
        )

    reasoner = ScriptedReasoner([expired])
    store, goal, policy, coordinator = _environment(tmp_path, reasoner)

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.WAITING
    assert run.step_count == 1
    assert store.observations_for_run(run.id) == []
    decisions = store.decisions_for_run(run.id)
    assert decisions[-1].action == AgentAction.WAIT
    assert "expired Agent decision" in decisions[-1].evidence[0]


@pytest.mark.parametrize(
    "selection_status",
    ["robust_candidate", "sandbox_probe_candidate"],
)
def test_entry_continues_deterministically_after_candidate_selection(
    tmp_path,
    selection_status,
):
    class SingleUseReasoner:
        def __init__(self):
            self.calls = 0

        async def decide(self, *, state, **_):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("entry continuation must not require another model response")
            return AgentDecision(
                run_id=state.run_id,
                action=AgentAction.RESEARCH,
                reason="筛选同市场候选，并只在全部稳健性证据通过后选择计划",
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
                tool_call=AgentToolCall(
                    name="screen_trade_candidates",
                    arguments={
                        "symbol": "ETH/USDT",
                        "timeframe": "1h",
                        "risk_fraction": "0.0025",
                    },
                    idempotency_key=f"{state.run_id}:screen",
                ),
            )

    reasoner = SingleUseReasoner()
    store = AgentStore(SqliteStore(tmp_path / "entry-state-machine.db"))
    goal = AgentGoal(
        objective="在测试网筛选稳健策略并以原生保护自动执行",
        symbols=["ETH/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id="policy_sandbox",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key="ETH/USDT:1h:deterministic-entry",
            payload={"symbol": "ETH/USDT", "timeframe": "1h"},
        )
    )
    calls = []
    plan = {
        "id": "stg_deterministic",
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "leverage": "3",
        "sizing": {"risk_fraction": "0.0025"},
    }

    def screen_trade_candidates(**arguments):
        calls.append(("screen_trade_candidates", arguments))
        return {
            "selection_status": selection_status,
            "selected_authorization": {
                "plan_id": plan["id"],
                "symbol": plan["symbol"],
                "timeframe": plan["timeframe"],
                "risk_fraction": plan["sizing"]["risk_fraction"],
                "leverage": plan["leverage"],
                "protected": True,
            },
        }

    def execute_validated_trade_plan(**arguments):
        calls.append(("execute_validated_trade_plan", arguments))
        return {"status": "submitted", "order_ids": ["ord_sandbox"]}

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="screen_trade_candidates",
            description="Screen candidates",
            risk_class=ToolRiskClass.RESEARCH,
            handler=screen_trade_candidates,
        )
    )
    registry.register(
        AgentTool(
            name="execute_validated_trade_plan",
            description="atomic validated entry",
            risk_class=ToolRiskClass.INCREASE_RISK,
            handler=execute_validated_trade_plan,
        )
    )
    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
    )
    policy = AgentPolicy(
        id="policy_sandbox",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["ETH/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.0025",
        max_open_positions=3,
        max_trades_per_day=6,
        max_leverage="3",
        require_native_protection=True,
        allow_agent_entry=True,
    )

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.COMPLETED
    assert run.step_count == 2
    assert reasoner.calls == 1
    assert [name for name, _ in calls] == [
        "screen_trade_candidates",
        "execute_validated_trade_plan",
    ]
    expected_entry_arguments = {
        "plan_id": "stg_deterministic",
        "symbol": "ETH/USDT",
        "timeframe": "1h",
        "risk_fraction": "0.0025",
        "leverage": "3",
        "protected": True,
    }
    assert calls[1][1] == expected_entry_arguments


def test_reasoner_failure_falls_back_only_to_deterministic_screen_then_wait(
    tmp_path,
):
    class FailingReasoner:
        calls = 0

        async def decide(self, **_):
            self.calls += 1
            raise RuntimeError("provider returned invalid structured JSON")

    reasoner = FailingReasoner()
    store = AgentStore(SqliteStore(tmp_path / "reasoner-fallback.db"))
    goal = AgentGoal(
        objective="Continue bounded deterministic research when the AI provider fails",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id="fallback-policy",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.SCHEDULE_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key="BTC/USDT:1h:reasoner-fallback",
            payload={"symbol": "BTC/USDT", "timeframe": "1h"},
        )
    )
    calls = []

    def screen_trade_candidates(**arguments):
        calls.append(arguments)
        return {
            "selection_status": "wait",
            "selected_plan_id": None,
            "reason": "no candidate passed deterministic robustness evidence",
        }

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="screen_trade_candidates",
            description="Screen deterministic candidates",
            risk_class=ToolRiskClass.RESEARCH,
            handler=screen_trade_candidates,
        )
    )

    def state_builder(selected_goal, event, run):
        del selected_goal
        return AgentState(
            goal_id=goal.id,
            run_id=run.id,
            market={
                "symbol": event.payload["symbol"],
                "timeframe": event.payload["timeframe"],
            },
            risk={"halted": False},
        )

    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
        state_builder=state_builder,
    )
    policy = AgentPolicy(
        id="fallback-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.0025",
        max_daily_drawdown="0.015",
        max_open_positions=3,
        max_trades_per_day=6,
        max_leverage="3",
        require_native_protection=True,
        allow_agent_entry=True,
    )

    run = asyncio.run(
        coordinator.process_next(
            worker_id="fallback-worker",
            goal=goal,
            policy=policy,
        )
    )

    assert run and run.status == AgentRunStatus.WAITING
    assert run.step_count == 2
    assert reasoner.calls == 2
    assert calls == [
        {
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "risk_fraction": "0.0025",
        }
    ]
    decisions = store.decisions_for_run(run.id)
    assert decisions[0].tool_call.name == "screen_trade_candidates"
    assert decisions[0].uncertainty == 1
    assert decisions[1].action == AgentAction.WAIT
    assert "invalid structured JSON" in decisions[1].evidence[0]


def _position_management_environment(tmp_path, recommendation):
    class NeverReasoner:
        calls = 0

        async def decide(self, **_):
            self.calls += 1
            raise AssertionError("owned-position management must be deterministic")

    reasoner = NeverReasoner()
    store = AgentStore(SqliteStore(tmp_path / f"position-{recommendation}.db"))
    goal = AgentGoal(
        objective="Deterministically manage an attributed protected ETH position",
        symbols=["ETH/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id="position-policy",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.SCHEDULE_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key=f"ETH/USDT:1h:position-{recommendation}",
            payload={"symbol": "ETH/USDT", "timeframe": "1h"},
        )
    )
    calls = []

    def assess_owned_position(**arguments):
        calls.append(("assess_owned_position", arguments))
        return {
            "recommendation": recommendation,
            "reason": (
                "latest closed signal is still aligned"
                if recommendation == "hold"
                else "latest closed signal is opposite"
            ),
            "execution_arguments": (
                {} if recommendation == "hold" else {"plan_id": "plan_eth", "symbol": "ETH/USDT"}
            ),
        }

    def exit_position(**arguments):
        calls.append(("exit_position", arguments))
        return {"status": "closed"}

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="assess_owned_position",
            description="Assess owned position",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=assess_owned_position,
        )
    )
    registry.register(
        AgentTool(
            name="exit_position",
            description="Exit owned position",
            risk_class=ToolRiskClass.DECREASE_RISK,
            handler=exit_position,
        )
    )

    def state_builder(selected_goal, event, run):
        del selected_goal, event
        return AgentState(
            goal_id=goal.id,
            run_id=run.id,
            market={"symbol": "ETH/USDT", "timeframe": "1h"},
            active_episodes=[
                {
                    "episode_id": "episode_eth",
                    "plan_id": "plan_eth",
                    "symbol": "ETH/USDT",
                    "status": "open",
                }
            ],
        )

    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
        state_builder=state_builder,
    )
    policy = AgentPolicy(
        id="position-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["ETH/USDT"],
        allowed_timeframes=["1h"],
        allow_agent_exit=True,
    )
    return store, goal, policy, coordinator, reasoner, calls


def test_open_position_is_assessed_then_held_without_model_or_new_entry(tmp_path):
    _, goal, policy, coordinator, reasoner, calls = _position_management_environment(
        tmp_path, "hold"
    )

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.WAITING
    assert run.step_count == 2
    assert reasoner.calls == 0
    assert calls == [
        (
            "assess_owned_position",
            {"plan_id": "plan_eth", "symbol": "ETH/USDT"},
        )
    ]


def test_open_position_opposite_signal_exits_with_exact_assessment_arguments(
    tmp_path,
):
    _, goal, policy, coordinator, reasoner, calls = _position_management_environment(
        tmp_path, "exit"
    )

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.COMPLETED
    assert run.step_count == 2
    assert reasoner.calls == 0
    assert [name for name, _ in calls] == [
        "assess_owned_position",
        "exit_position",
    ]
    assert calls[1][1] == {
        "plan_id": "plan_eth",
        "symbol": "ETH/USDT",
    }


def test_awaiting_review_is_closed_deterministically_without_model(tmp_path):
    class NeverReasoner:
        calls = 0

        async def decide(self, **_):
            self.calls += 1
            raise AssertionError("closed-trade review must be deterministic")

    reasoner = NeverReasoner()
    store = AgentStore(SqliteStore(tmp_path / "deterministic-review.db"))
    goal = AgentGoal(
        objective="Review every verified closed trade and persist evidence memory",
        symbols=["ETH/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id="review-policy",
    )
    store.save_goal(goal)
    store.append_event(
        AgentEvent(
            event_type=AgentEventType.POSITION_CLOSED,
            source="test",
            goal_id=goal.id,
            dedupe_key="episode:eth:position-closed",
            payload={
                "episode_id": "episode_eth",
                "plan_id": "plan_eth",
                "symbol": "ETH/USDT",
            },
        )
    )
    calls = []

    def review_closed_trade(**arguments):
        calls.append(arguments)
        return {
            "episode": {"id": "episode_eth", "status": "closed"},
            "memory": {"id": "memory_eth"},
        }

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="review_closed_trade",
            description="Review closed trade",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=review_closed_trade,
        )
    )

    def state_builder(selected_goal, event, run):
        del selected_goal, event
        return AgentState(
            goal_id=goal.id,
            run_id=run.id,
            market={"symbol": "ETH/USDT"},
            active_episodes=[
                {
                    "episode_id": "episode_eth",
                    "plan_id": "plan_eth",
                    "symbol": "ETH/USDT",
                    "status": "awaiting_review",
                }
            ],
        )

    coordinator = AgentCoordinator(
        store=store,
        registry=registry,
        executor=AgentToolExecutor(registry, store),
        reasoner=reasoner,
        state_builder=state_builder,
    )
    policy = AgentPolicy(
        id="review-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["ETH/USDT"],
        allowed_timeframes=["1h"],
    )

    run = asyncio.run(coordinator.process_next(worker_id="worker-1", goal=goal, policy=policy))

    assert run and run.status == AgentRunStatus.COMPLETED
    assert run.step_count == 1
    assert reasoner.calls == 0
    assert calls == [{"plan_id": "plan_eth"}]
