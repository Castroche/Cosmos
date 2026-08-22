from datetime import UTC, datetime
from decimal import Decimal

from cosmos_quant.agent.schemas import (
    AgentAutonomyMode,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentRun,
    AgentRunStatus,
    AgentToolCall,
    TradeEpisode,
    TradeEpisodeStatus,
)
from cosmos_quant.agent.store import AgentStore
from cosmos_quant.storage import SqliteStore


def _store(tmp_path) -> AgentStore:
    return AgentStore(SqliteStore(tmp_path / "agent-recovery.db"))


def _run(store: AgentStore) -> AgentRun:
    goal = AgentGoal(
        objective="持续验证进程中断后的幂等恢复与风险收口",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        risk_policy_id="policy_test",
    )
    store.save_goal(goal)
    event = store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key=f"restart:{goal.id}",
        )
    )
    run = AgentRun(goal_id=goal.id, trigger_event_id=event.id)
    store.save_run(run)
    return run


def test_restart_recovery_closes_read_only_tool_and_run(tmp_path):
    store = _store(tmp_path)
    run = _run(store)
    call = AgentToolCall(
        name="analyze_market_regime",
        arguments={"symbol": "BTC/USDT", "timeframe": "1h"},
        idempotency_key="restart-read-only-test",
    )
    store.begin_tool_call(run.id, call)

    recovered = store.recover_interrupted_work()

    assert recovered == [
        {
            "tool_call_id": call.id,
            "run_id": run.id,
            "tool_name": call.name,
            "external_effect_uncertain": False,
        }
    ]
    recovered_run = store.load_run(run.id)
    assert recovered_run and recovered_run.status == AgentRunStatus.FAILED
    observations = store.observations_for_run(run.id)
    assert len(observations) == 1
    assert observations[0].kind == "tool_interrupted"
    assert observations[0].data["external_effect_uncertain"] is False
    status, result, created = store.begin_tool_call(run.id, call)
    assert status == "failed"
    assert result["kind"] == "tool_interrupted"
    assert created is False
    assert store.recover_interrupted_work() == []


def test_restart_recovery_marks_execution_episode_uncertain(tmp_path):
    store = _store(tmp_path)
    run = _run(store)
    episode = TradeEpisode(
        goal_id=run.goal_id,
        policy_id="policy_test",
        run_id=run.id,
        plan_id="plan_test",
        symbol="BTC/USDT",
        side="long",
        status=TradeEpisodeStatus.OPEN,
        planned_risk=Decimal("10"),
        opened_at=datetime.now(UTC),
    )
    store.save_episode(episode)
    call = AgentToolCall(
        name="reduce_position",
        arguments={
            "plan_id": episode.plan_id,
            "symbol": episode.symbol,
            "fraction": "0.5",
        },
        idempotency_key="restart-execution-test",
    )
    store.begin_tool_call(run.id, call)

    recovered = store.recover_interrupted_work()

    assert recovered[0]["external_effect_uncertain"] is True
    recovered_episode = next(item for item in store.list_episodes() if item.id == episode.id)
    assert recovered_episode and recovered_episode.status == TradeEpisodeStatus.UNCERTAIN
    assert "reconciliation is required" in recovered_episode.summary
