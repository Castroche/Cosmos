import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cosmos_quant.agent.policy import AgentPolicy
from cosmos_quant.agent.reasoner import AgentReasoningOutput, GatewayAgentReasoner
from cosmos_quant.agent.runtime_tools import scheduled_event_key
from cosmos_quant.agent.schemas import (
    AgentAction,
    AgentAutonomyMode,
    AgentDecision,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentGoalStatus,
    AgentMemory,
    AgentObservation,
    AgentRun,
    AgentRunStatus,
    AgentState,
    AgentToolCall,
    MemoryType,
    TradeEpisode,
    TradeEpisodeStatus,
)
from cosmos_quant.agent.store import AgentStore
from cosmos_quant.ai_configuration import AIProvider
from cosmos_quant.ai_gateway import AIErrorCode, AIProviderError
from cosmos_quant.storage import SqliteStore
from pydantic import ValidationError


def _agent_store(tmp_path) -> AgentStore:
    return AgentStore(SqliteStore(tmp_path / "agent.db"))


def _goal() -> AgentGoal:
    return AgentGoal(
        objective="持续寻找经过验证且风险受限的交易机会",
        symbols=["btc-usdt"],
        timeframes=["1H"],
        risk_policy_id="policy_test",
    )


def _event(key: str = "BTC/USDT:1h:2026-01-01T01:00:00Z") -> AgentEvent:
    return AgentEvent(
        event_type=AgentEventType.CANDLE_CLOSED,
        source="test",
        dedupe_key=key,
        payload={"symbol": "BTC/USDT", "timeframe": "1h"},
    )


def test_agent_decision_requires_structured_tool_call():
    with pytest.raises(ValidationError, match="requires a structured tool call"):
        AgentDecision(
            run_id="run_test",
            action=AgentAction.ANALYZE,
            reason="需要分析最新市场状态再决定是否交易",
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
        )

    waiting = AgentDecision(
        run_id="run_test",
        action=AgentAction.WAIT,
        reason="当前没有足够证据支持增加风险，因此继续等待",
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert waiting.tool_call is None


def test_agent_reasoning_output_normalizes_null_tool_arguments():
    output = AgentReasoningOutput.model_validate(
        {
            "action": "wait",
            "reason": "There is not enough evidence to justify another bounded action.",
            "tool_name": None,
            "tool_arguments": None,
        }
    )

    assert output.tool_arguments == {}


@pytest.mark.parametrize(
    ("model_action", "tool_name", "expected_action"),
    [
        ("observe", "analyze_market_regime", AgentAction.ANALYZE),
        ("analyze", "search_memory", AgentAction.OBSERVE),
        ("research", "run_plan_backtest", AgentAction.BACKTEST),
        ("build_trade_plan", "build_trade_plan", AgentAction.RESEARCH),
    ],
)
def test_agent_reasoning_output_derives_action_from_known_tool(
    model_action, tool_name, expected_action
):
    output = AgentReasoningOutput.model_validate(
        {
            "action": model_action,
            "reason": "The selected deterministic tool is the next bounded action.",
            "tool_name": tool_name,
            "tool_arguments": {},
        }
    )

    assert output.action == expected_action


def test_gateway_agent_reasoner_retries_one_invalid_json_response():
    class FlakyGateway:
        def __init__(self):
            self.calls = 0

        async def generate_json(self, **_):
            self.calls += 1
            if self.calls == 1:
                raise AIProviderError(
                    AIProvider.DEEPSEEK,
                    AIErrorCode.INVALID_RESPONSE,
                    "invalid JSON",
                )
            return (
                SimpleNamespace(
                    data={
                        "action": "wait",
                        "reason": "There is not enough evidence to increase account risk.",
                        "tool_name": None,
                        "tool_arguments": None,
                    }
                ),
                [AIProvider.DEEPSEEK],
            )

    gateway = FlakyGateway()
    decision = asyncio.run(
        GatewayAgentReasoner(gateway).decide(
            goal=_goal(),
            event=_event(),
            state=AgentState(goal_id="goal_test", run_id="run_retry"),
            observations=[],
            tools=[],
            step=0,
        )
    )

    assert gateway.calls == 2
    assert decision.action == AgentAction.WAIT
    assert decision.tool_call is None


def test_gateway_agent_reasoner_builds_cacheable_static_prefix_and_compact_context():
    class CapturingGateway:
        def __init__(self):
            self.kwargs = None

        async def generate_json(self, **kwargs):
            self.kwargs = kwargs
            return (
                SimpleNamespace(
                    data={
                        "action": "wait",
                        "reason": "There is not enough evidence to increase account risk.",
                        "tool_name": None,
                        "tool_arguments": {},
                    }
                ),
                [AIProvider.DEEPSEEK],
            )

    gateway = CapturingGateway()
    state = AgentState(goal_id="goal_test", run_id="run_cacheable")
    tools = [
        {"name": "z_tool", "description": "last"},
        {"description": "first", "name": "a_tool"},
    ]

    asyncio.run(
        GatewayAgentReasoner(gateway).decide(
            goal=_goal(),
            event=_event(),
            state=state,
            observations=[],
            tools=tools,
            step=0,
        )
    )

    assert gateway.kwargs is not None
    assert gateway.kwargs["system"].index('"name":"a_tool"') < gateway.kwargs[
        "system"
    ].index('"name":"z_tool"')
    assert "available_tools" not in gateway.kwargs["prompt"]
    assert "run_cacheable" not in gateway.kwargs["prompt"]
    assert gateway.kwargs["operation"] == "agent_decision"
    assert gateway.kwargs["prompt_version"] == "agent_reasoner_v2"
    assert gateway.kwargs["schema_version"] == "agent_reasoning_output_v2"
    assert gateway.kwargs["max_tokens"] == 1200


def test_agent_reasoning_output_rejects_unbounded_verbose_content():
    with pytest.raises(ValidationError):
        AgentReasoningOutput.model_validate(
            {
                "action": "wait",
                "reason": "x" * 801,
                "evidence": [],
                "tool_name": None,
                "tool_arguments": {},
            }
        )

    with pytest.raises(ValidationError):
        AgentReasoningOutput.model_validate(
            {
                "action": "wait",
                "reason": "There is not enough evidence to increase account risk.",
                "evidence": [f"item-{index}" for index in range(9)],
                "tool_name": None,
                "tool_arguments": {},
            }
        )


def test_agent_event_store_deduplicates_and_enforces_lease_owner(tmp_path):
    store = _agent_store(tmp_path)
    first = store.append_event(_event())
    duplicate = store.append_event(_event())

    assert duplicate.id == first.id
    claimed = store.claim_next_event("worker-a")
    assert claimed and claimed.id == first.id
    assert store.claim_next_event("worker-b") is None
    store.renew_event_lease(first.id, "worker-a")
    with pytest.raises(RuntimeError, match="renewed"):
        store.renew_event_lease(first.id, "worker-b")
    with pytest.raises(RuntimeError, match="lease"):
        store.finish_event(first.id, "worker-b")
    store.finish_event(first.id, "worker-a")
    assert store.claim_next_event("worker-a") is None


def test_agent_store_coalesces_only_stale_scheduler_events(tmp_path):
    store = _agent_store(tmp_path)
    old_schedule = AgentEvent(
        event_type=AgentEventType.SCHEDULE_TRIGGERED,
        source="agent-scheduler",
        goal_id="goal_test",
        dedupe_key="schedule:goal_test:BTC/USDT:1h:3600:old",
    )
    active_schedule = old_schedule.model_copy(
        update={
            "id": "aevt_active",
            "dedupe_key": "schedule:goal_test:BTC/USDT:1h:3600:current",
        }
    )
    manual = AgentEvent(
        event_type=AgentEventType.MANUAL_TRIGGERED,
        source="operator",
        goal_id="goal_test",
        dedupe_key="manual:goal_test:current",
    )
    store.append_event(old_schedule)
    store.append_event(active_schedule)
    store.append_event(manual)

    superseded = store.supersede_stale_schedule_events(
        "goal_test",
        [active_schedule.dedupe_key],
    )

    assert superseded == 1
    assert store.claim_next_event("worker-a", goal_id="goal_test").id == active_schedule.id
    store.finish_event(active_schedule.id, "worker-a")
    assert store.claim_next_event("worker-a", goal_id="goal_test").id == manual.id


def test_agent_store_retries_only_pre_decision_scheduler_failures(tmp_path):
    store = _agent_store(tmp_path)
    store.save_goal(
        AgentGoal(
            id="goal_test",
            objective="Test bounded scheduler retries",
            symbols=["BTC/USDT"],
            timeframes=["4h"],
            risk_policy_id="policy_test",
        )
    )
    event = AgentEvent(
        event_type=AgentEventType.SCHEDULE_TRIGGERED,
        source="agent-scheduler",
        goal_id="goal_test",
        dedupe_key="schedule:goal_test:BTC/USDT:4h:14400:current",
        payload={"symbol": "BTC/USDT", "timeframe": "4h"},
    )
    store.append_event(event)
    claimed = store.claim_next_event("worker-a", goal_id="goal_test")
    assert claimed and claimed.id == event.id
    failed = AgentRun(
        goal_id="goal_test",
        trigger_event_id=event.id,
        status=AgentRunStatus.FAILED,
        step_count=0,
        last_error="transient exchange metadata request failed",
    )
    store.save_run(failed)
    store.finish_event(event.id, "worker-a", failed=True)

    assert store.requeue_safe_failed_schedule_event(
        event.dedupe_key,
        max_attempts=3,
        backoff_seconds=0,
    )
    retried = store.claim_next_event("worker-b", goal_id="goal_test")
    assert retried and retried.id == event.id

    second_failure = failed.model_copy(
        update={
            "id": "run_second_failure",
            "last_error": "transient exchange request failed again",
        }
    )
    store.save_run(second_failure)
    store.finish_event(event.id, "worker-b", failed=True)
    assert (
        store.requeue_safe_failed_schedule_event(
            event.dedupe_key,
            max_attempts=2,
            backoff_seconds=0,
        )
        is False
    )


def test_agent_store_never_retries_scheduler_failure_after_a_decision(tmp_path):
    store = _agent_store(tmp_path)
    store.save_goal(
        AgentGoal(
            id="goal_test",
            objective="Test scheduler failure after an Agent decision",
            symbols=["ETH/USDT"],
            timeframes=["1h"],
            risk_policy_id="policy_test",
        )
    )
    event = AgentEvent(
        event_type=AgentEventType.SCHEDULE_TRIGGERED,
        source="agent-scheduler",
        goal_id="goal_test",
        dedupe_key="schedule:goal_test:ETH/USDT:1h:3600:current",
        payload={"symbol": "ETH/USDT", "timeframe": "1h"},
    )
    store.append_event(event)
    claimed = store.claim_next_event("worker-a", goal_id="goal_test")
    assert claimed and claimed.id == event.id
    failed = AgentRun(
        goal_id="goal_test",
        trigger_event_id=event.id,
        status=AgentRunStatus.FAILED,
        step_count=1,
        last_error="failure after an Agent decision",
    )
    store.save_run(failed)
    store.finish_event(event.id, "worker-a", failed=True)

    assert (
        store.requeue_safe_failed_schedule_event(
            event.dedupe_key,
            backoff_seconds=0,
        )
        is False
    )
    assert store.claim_next_event("worker-b", goal_id="goal_test") is None


def test_agent_store_reports_recovered_market_schedule_coverage(tmp_path):
    store = _agent_store(tmp_path)
    store.save_goal(
        AgentGoal(
            id="goal_test",
            objective="Expose per-market scheduler recovery evidence",
            symbols=["SOL/USDT"],
            timeframes=["1h"],
            risk_policy_id="policy_test",
        )
    )
    event = AgentEvent(
        event_type=AgentEventType.SCHEDULE_TRIGGERED,
        source="agent-scheduler",
        goal_id="goal_test",
        dedupe_key="schedule:goal_test:SOL/USDT:1h:3600:current",
        payload={"symbol": "SOL/USDT", "timeframe": "1h"},
    )
    store.append_event(event)
    assert store.claim_next_event("worker-a", goal_id="goal_test")
    first = AgentRun(
        goal_id="goal_test",
        trigger_event_id=event.id,
        status=AgentRunStatus.FAILED,
        step_count=0,
        last_error="temporary market bootstrap failure",
    )
    store.save_run(first)
    store.finish_event(event.id, "worker-a", failed=True)
    assert store.requeue_safe_failed_schedule_event(
        event.dedupe_key,
        backoff_seconds=0,
    )
    assert store.claim_next_event("worker-b", goal_id="goal_test")
    recovered = AgentRun(
        goal_id="goal_test",
        trigger_event_id=event.id,
        status=AgentRunStatus.WAITING,
        step_count=3,
        completed_at=datetime.now(UTC),
    )
    store.save_run(recovered)
    store.finish_event(event.id, "worker-b")

    coverage = store.schedule_market_coverage(["goal_test"])

    assert len(coverage) == 1
    assert coverage[0]["symbol"] == "SOL/USDT"
    assert coverage[0]["timeframe"] == "1h"
    assert coverage[0]["attempt_count"] == 2
    assert coverage[0]["last_run_id"] == recovered.id
    assert coverage[0]["last_run_status"] == "waiting"
    assert coverage[0]["retry_recovered"] is True


def test_agent_run_decisions_observations_and_memory_are_durable(tmp_path):
    store = _agent_store(tmp_path)
    goal = _goal()
    store.save_goal(goal)
    event = store.append_event(_event())
    run = AgentRun(goal_id=goal.id, trigger_event_id=event.id)
    store.save_run(run)
    call = AgentToolCall(
        name="analyze_market_regime",
        arguments={"symbol": "BTC/USDT", "timeframe": "1h"},
        idempotency_key=f"{run.id}:analyze:1",
    )
    decision = AgentDecision(
        run_id=run.id,
        action=AgentAction.ANALYZE,
        reason="先调用确定性市场状态分析工具建立可验证证据",
        evidence=[event.id],
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
        tool_call=call,
    )
    store.save_decision(decision)
    observation = AgentObservation(
        run_id=run.id,
        kind="tool_result",
        ok=True,
        summary="市场状态分析完成",
        data={"regime": "trend"},
        tool_call_id=call.id,
    )
    store.save_observation(observation)
    memory = AgentMemory(
        memory_type=MemoryType.STRATEGY,
        content="趋势评分不足时，突破候选需要额外成交量确认",
        evidence_refs=[observation.id],
    )
    store.save_memory(memory)

    loaded = store.load_run(run.id)
    assert loaded and loaded.goal_id == goal.id
    assert store.load_goal(goal.id) == goal
    assert store.recent_memories()[0].id == memory.id


def test_tool_call_idempotency_returns_the_original_result(tmp_path):
    store = _agent_store(tmp_path)
    goal = _goal()
    store.save_goal(goal)
    event = store.append_event(_event())
    run = AgentRun(goal_id=goal.id, trigger_event_id=event.id)
    store.save_run(run)
    first = AgentToolCall(
        name="get_market_context",
        arguments={"symbol": "BTC/USDT"},
        idempotency_key=f"{run.id}:market:1",
    )

    status, result, created = store.begin_tool_call(run.id, first)
    assert status == "running" and result is None and created is True
    store.finish_tool_call(first, {"last": "100"})

    duplicate = first.model_copy(update={"id": "tool_duplicate"})
    status, result, created = store.begin_tool_call(run.id, duplicate)
    assert status == "completed"
    assert result == {"last": "100"}
    assert created is False


def test_entry_episode_budget_is_reserved_atomically(tmp_path):
    store = _agent_store(tmp_path)
    first = TradeEpisode(
        goal_id="goal_a",
        policy_id="policy_test",
        run_id="run_a",
        plan_id="plan_a",
        symbol="BTC/USDT",
        side="long",
        status=TradeEpisodeStatus.RESERVED,
        planned_risk=Decimal("10"),
        opened_at=datetime.now(UTC),
    )
    store.reserve_episode(first, max_trades_per_day=1, max_open_positions=1)

    duplicate_plan = first.model_copy(update={"id": "episode_duplicate"})
    with pytest.raises(RuntimeError, match="already has an entry episode"):
        store.reserve_episode(duplicate_plan, max_trades_per_day=2, max_open_positions=2)

    second = first.model_copy(
        update={
            "id": "episode_second",
            "run_id": "run_b",
            "plan_id": "plan_b",
        }
    )
    with pytest.raises(RuntimeError, match="daily trade budget"):
        store.reserve_episode(second, max_trades_per_day=1, max_open_positions=2)

    assert store.list_episodes()[0].id == first.id


def test_entry_episode_prevents_same_symbol_pyramiding_but_allows_other_symbol(
    tmp_path,
):
    store = _agent_store(tmp_path)
    first = TradeEpisode(
        goal_id="goal_a",
        policy_id="policy_test",
        run_id="run_a",
        plan_id="plan_btc",
        symbol="BTC/USDT",
        side="long",
        status=TradeEpisodeStatus.RESERVED,
        planned_risk=Decimal("10"),
        opened_at=datetime.now(UTC),
    )
    store.reserve_episode(first, max_trades_per_day=3, max_open_positions=2)
    first.status = TradeEpisodeStatus.OPEN
    store.save_episode(first)
    duplicate_symbol = first.model_copy(
        update={
            "id": "episode_duplicate_symbol",
            "run_id": "run_b",
            "plan_id": "plan_btc_second",
            "status": TradeEpisodeStatus.RESERVED,
        }
    )

    with pytest.raises(RuntimeError, match="already has an active entry episode"):
        store.reserve_episode(
            duplicate_symbol,
            max_trades_per_day=3,
            max_open_positions=2,
        )

    eth = first.model_copy(
        update={
            "id": "episode_eth",
            "run_id": "run_c",
            "plan_id": "plan_eth",
            "symbol": "ETH/USDT",
            "status": TradeEpisodeStatus.RESERVED,
        }
    )
    store.reserve_episode(eth, max_trades_per_day=3, max_open_positions=2)

    assert {item.symbol for item in store.list_episodes()} == {
        "BTC/USDT",
        "ETH/USDT",
    }


def test_sandbox_goal_is_atomically_promoted_to_live(tmp_path):
    store = _agent_store(tmp_path)
    sandbox_policy = AgentPolicy(
        id="sandbox-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.001",
        max_daily_drawdown="0.01",
        max_open_positions=1,
        max_trades_per_day=2,
        max_leverage="3",
        allow_agent_entry=True,
    )
    sandbox_goal = AgentGoal(
        id="sandbox-goal",
        objective="Autonomously trade validated BTC evidence in the authorized environment",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id=sandbox_policy.id,
    )
    live_policy = sandbox_policy.model_copy(
        update={
            "id": "sandbox-policy-live",
            "autonomy_mode": AgentAutonomyMode.LIVE_CAPPED,
        }
    )
    live_goal = sandbox_goal.model_copy(
        update={
            "id": "live-goal",
            "autonomy_mode": AgentAutonomyMode.LIVE_CAPPED,
            "risk_policy_id": live_policy.id,
        }
    )
    second_sandbox_goal = sandbox_goal.model_copy(update={"id": "second-sandbox-goal"})
    research_goal = sandbox_goal.model_copy(
        update={
            "id": "research-goal",
            "autonomy_mode": AgentAutonomyMode.RESEARCH,
            "risk_policy_id": "research-policy",
        }
    )
    store.save_policy(sandbox_policy)
    store.save_goal(sandbox_goal)
    store.save_goal(second_sandbox_goal)
    store.save_goal(research_goal)

    promoted, policy = store.promote_goal_to_live(
        sandbox_goal.id,
        live_policy,
        live_goal,
    )

    assert promoted.status == AgentGoalStatus.ACTIVE
    assert policy.autonomy_mode == AgentAutonomyMode.LIVE_CAPPED
    assert store.load_goal(sandbox_goal.id).status == AgentGoalStatus.PAUSED
    assert store.load_goal(second_sandbox_goal.id).status == AgentGoalStatus.PAUSED
    assert store.load_goal(research_goal.id).status == AgentGoalStatus.ACTIVE
    assert store.load_goal(live_goal.id).status == AgentGoalStatus.ACTIVE
    assert store.load_policy(live_policy.id).fingerprint == live_policy.fingerprint

    replayed, _ = store.promote_goal_to_live(
        sandbox_goal.id,
        live_policy,
        live_goal,
    )
    assert replayed.id == live_goal.id


def test_scheduled_events_are_scoped_per_market():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    btc = scheduled_event_key("goal", 60, now, symbol="BTC/USDT", timeframe="1h")
    eth = scheduled_event_key("goal", 60, now, symbol="ETH/USDT", timeframe="1h")

    assert btc != eth
    assert scheduled_event_key("goal", 60, now, symbol="BTC/USDT", timeframe="1h") == btc


def test_scheduled_market_event_waits_for_a_new_closed_bar():
    now = datetime(2026, 1, 1, 10, 5, tzinfo=UTC)
    current = scheduled_event_key("goal", 60, now, symbol="BTC/USDT", timeframe="1h")

    assert (
        scheduled_event_key(
            "goal",
            60,
            now + timedelta(minutes=50),
            symbol="BTC/USDT",
            timeframe="1h",
        )
        == current
    )
    assert (
        scheduled_event_key(
            "goal",
            60,
            now + timedelta(hours=1),
            symbol="BTC/USDT",
            timeframe="1h",
        )
        != current
    )


def test_operator_poll_interval_can_throttle_shorter_timeframes():
    now = datetime(2026, 1, 1, 10, 5, tzinfo=UTC)
    current = scheduled_event_key("goal", 3600, now, symbol="BTC/USDT", timeframe="15m")

    assert (
        scheduled_event_key(
            "goal",
            3600,
            now + timedelta(minutes=30),
            symbol="BTC/USDT",
            timeframe="15m",
        )
        == current
    )
