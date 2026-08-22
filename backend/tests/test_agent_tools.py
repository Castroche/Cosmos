import asyncio
from decimal import Decimal
from types import SimpleNamespace

from cosmos_quant.agent.policy import AgentPolicy, AgentPolicyEngine
from cosmos_quant.agent.runtime_tools import (
    _strategy_family_fit,
    build_core_tool_registry,
    classify_execution_context,
)
from cosmos_quant.agent.schemas import (
    AgentAutonomyMode,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentRun,
    AgentToolCall,
    ToolRiskClass,
)
from cosmos_quant.agent.store import AgentStore
from cosmos_quant.agent.tools import AgentTool, AgentToolExecutor, AgentToolRegistry
from cosmos_quant.configuration import StrategyKind, UserConfiguration
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.signals import MarketStateAssessment
from cosmos_quant.storage import SqliteStore


def _runtime(tmp_path):
    store = AgentStore(SqliteStore(tmp_path / "agent-tools.db"))
    goal = AgentGoal(
        objective="在固定风险预算内自动执行经过验证的交易计划",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        risk_policy_id="policy_live",
    )
    store.save_goal(goal)
    event = store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            dedupe_key="manual:test-agent-tools",
        )
    )
    run = AgentRun(goal_id=goal.id, trigger_event_id=event.id)
    store.save_run(run)
    return store, run


def _live_policy() -> AgentPolicy:
    return AgentPolicy(
        id="policy_live",
        autonomy_mode=AgentAutonomyMode.LIVE_CAPPED,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade=Decimal("0.0025"),
        max_daily_drawdown=Decimal("0.01"),
        max_open_positions=2,
        max_trades_per_day=4,
        max_leverage=Decimal("2"),
        allow_agent_entry=True,
    )


def test_policy_rejects_agent_entry_above_live_cap(tmp_path):
    store, run = _runtime(tmp_path)
    invoked = False

    def submit(**_):
        nonlocal invoked
        invoked = True
        return {"submitted": True}

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="execute_validated_trade_plan",
            description="Submit a risk-increasing protected plan",
            risk_class=ToolRiskClass.INCREASE_RISK,
            handler=submit,
        )
    )
    executor = AgentToolExecutor(registry, store)
    call = AgentToolCall(
        name="execute_validated_trade_plan",
        arguments={
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "risk_fraction": "0.003",
            "leverage": "2",
            "protected": True,
        },
        idempotency_key=f"{run.id}:entry:1",
    )

    observation = asyncio.run(executor.execute(run_id=run.id, call=call, policy=_live_policy()))

    assert observation.ok is False
    assert observation.kind == "policy_rejection"
    assert "exceeds cap" in " ".join(observation.data["reasons"])
    assert invoked is False


def test_tool_executor_replays_completed_result_without_duplicate_action(tmp_path):
    store, run = _runtime(tmp_path)
    invocation_count = 0

    def submit(**arguments):
        nonlocal invocation_count
        invocation_count += 1
        return {"submitted": True, "symbol": arguments["symbol"]}

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="execute_validated_trade_plan",
            description="Submit a risk-increasing protected plan",
            risk_class=ToolRiskClass.INCREASE_RISK,
            handler=submit,
        )
    )
    executor = AgentToolExecutor(registry, store)
    call = AgentToolCall(
        name="execute_validated_trade_plan",
        arguments={
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "risk_fraction": "0.002",
            "leverage": "2",
            "protected": True,
        },
        idempotency_key=f"{run.id}:entry:allowed",
    )

    first = asyncio.run(executor.execute(run_id=run.id, call=call, policy=_live_policy()))
    assert store.observations_for_run(run.id) == [first]
    with store.store.connect() as connection:
        connection.execute(
            "DELETE FROM agent_observations WHERE observation_id = ?",
            (first.id,),
        )
    duplicate = asyncio.run(
        executor.execute(
            run_id=run.id,
            call=call.model_copy(update={"id": "tool_replayed"}),
            policy=_live_policy(),
        )
    )

    assert first.ok is True
    assert duplicate.data == first.data
    assert invocation_count == 1
    assert store.observations_for_run(run.id) == [first]


def test_observe_mode_can_read_but_cannot_resume_trading(tmp_path):
    store, run = _runtime(tmp_path)
    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="get_market_context",
            description="Read current market context",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=lambda **_: {"last": "100"},
        )
    )
    registry.register(
        AgentTool(
            name="resume_trading",
            description="Resume a halted account",
            risk_class=ToolRiskClass.CONTROL,
            handler=lambda **_: {"resumed": True},
        )
    )
    executor = AgentToolExecutor(registry, store)
    observe = AgentPolicy(
        id="observe",
        autonomy_mode=AgentAutonomyMode.OBSERVE,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )
    read = AgentToolCall(
        name="get_market_context",
        arguments={"symbol": "BTC/USDT", "timeframe": "1h"},
        idempotency_key=f"{run.id}:read:market",
    )
    resume = AgentToolCall(
        name="resume_trading",
        arguments={},
        idempotency_key=f"{run.id}:resume",
    )

    assert asyncio.run(executor.execute(run_id=run.id, call=read, policy=observe)).ok is True
    denied = asyncio.run(executor.execute(run_id=run.id, call=resume, policy=observe))
    assert denied.ok is False
    assert denied.kind == "policy_rejection"


def test_unknown_control_tool_is_denied_by_default():
    decision = AgentPolicyEngine().evaluate(
        _live_policy(),
        AgentToolCall(
            name="rotate_exchange_credentials",
            arguments={},
            idempotency_key="run:unknown-control",
        ),
        ToolRiskClass.CONTROL,
    )

    assert decision.allowed is False
    assert "denied by default" in decision.reasons[0]


def test_unknown_risk_decreasing_tool_is_denied_by_default():
    decision = AgentPolicyEngine().evaluate(
        _live_policy(),
        AgentToolCall(
            name="transfer_position_margin",
            arguments={},
            idempotency_key="run:unknown-decrease",
        ),
        ToolRiskClass.DECREASE_RISK,
    )

    assert decision.allowed is False
    assert "denied by default" in decision.reasons[0]


def test_unknown_risk_increasing_tool_is_denied_by_default():
    decision = AgentPolicyEngine().evaluate(
        _live_policy(),
        AgentToolCall(
            name="borrow_and_buy",
            arguments={
                "risk_fraction": "0.001",
                "leverage": "1",
                "protected": True,
            },
            idempotency_key="run:unknown-increase",
        ),
        ToolRiskClass.INCREASE_RISK,
    )

    assert decision.allowed is False
    assert "denied by default" in decision.reasons[0]


def test_registry_exposes_machine_readable_tool_parameter_schema():
    def bounded_market(symbol: str, limit: int = 120, *, _agent_run_id: str):
        del symbol, limit, _agent_run_id
        return {"ok": True}

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="bounded_market",
            description="Read a bounded market window",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=bounded_market,
            include_context=True,
        )
    )

    description = registry.descriptions()[0]
    parameters = description["parameters"]
    assert parameters["required"] == ["symbol"]
    assert parameters["properties"]["symbol"]["type"] == "string"
    assert parameters["properties"]["limit"] == {
        "type": "integer",
        "default": 120,
    }
    assert "_agent_run_id" not in parameters["properties"]
    assert parameters["additionalProperties"] is False


def test_timed_out_tool_is_recorded_and_never_replayed(tmp_path):
    store, run = _runtime(tmp_path)
    invocation_count = 0

    async def slow_market(**_):
        nonlocal invocation_count
        invocation_count += 1
        await asyncio.sleep(0.05)
        return {"last": "100"}

    registry = AgentToolRegistry()
    registry.register(
        AgentTool(
            name="get_market_context",
            description="Read current market context",
            risk_class=ToolRiskClass.READ_ONLY,
            handler=slow_market,
            timeout_seconds=0.01,
        )
    )
    executor = AgentToolExecutor(registry, store)
    call = AgentToolCall(
        name="get_market_context",
        arguments={"symbol": "BTC/USDT", "timeframe": "1h"},
        idempotency_key=f"{run.id}:timeout:market",
    )
    policy = AgentPolicy(
        id="observe",
        autonomy_mode=AgentAutonomyMode.OBSERVE,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )

    first = asyncio.run(executor.execute(run_id=run.id, call=call, policy=policy))
    replay = asyncio.run(
        executor.execute(
            run_id=run.id,
            call=call.model_copy(update={"id": "tool_timeout_replay"}),
            policy=policy,
        )
    )

    assert first.ok is False
    assert first.data["error_type"] == "TimeoutError"
    assert replay.data == first.data
    assert invocation_count == 1


def test_market_regime_tool_returns_multi_timeframe_strategy_evidence(tmp_path):
    candles = demo_candles(300)
    market = snapshot_from_candles(candles)

    class Runtime:
        def __init__(self):
            self.calls = []
            self.configuration_service = SimpleNamespace(
                current_configuration=lambda: UserConfiguration()
            )
            self.market_quality = None

        async def market_context(self, *, limit, symbol, timeframe):
            self.calls.append((limit, symbol, timeframe))
            return market, candles

    runtime = Runtime()
    store = AgentStore(SqliteStore(tmp_path / "market-evidence.db"))
    registry = build_core_tool_registry(runtime, SimpleNamespace(), store)
    handler = registry.get("analyze_market_regime").handler

    result = asyncio.run(handler(symbol="BTC/USDT", timeframe="1h"))

    assert runtime.calls == [
        (300, "BTC/USDT", "1h"),
        (300, "BTC/USDT", "4h"),
    ]
    assert result["confirmation"]["timeframe"] == "4h"
    assert set(result["signals"]) == {"trend", "mean_reversion", "breakout"}
    assert result["recommendation"]["action"] in {"consider_plan", "wait"}
    assert result["recommendation"]["direction"] in {"long", "short", "flat"}
    assert result["closed_candle_count"] >= 55
    assert result["confirmation"]["closed_candle_count"] >= 55
    assert result["market"]["spread_bps"] >= 0
    assert result["market_evidence"]["volatility_regime"] in {
        "low",
        "normal",
        "high",
    }
    assert result["actionable_candidates"]
    assert {item["adaptive_label"] for item in result["actionable_candidates"]}.issubset(
        {"baseline", "responsive", "selective", "cost_resilient"}
    )
    assert all(
        item["selection_basis"] == "current_market_only"
        and item["backtest_tuned"] is False
        and item["strategy_spec"]["family"] == item["strategy_kind"]
        for item in result["actionable_candidates"]
    )
    assert result["market_evidence"]["directional_bias"] in {
        "long",
        "short",
        "neutral",
    }
    assert "microstructure_conflict" in result["recommendation"]
    assert "derivatives_regime" in result["recommendation"]


def test_transient_microstructure_headwind_reduces_risk_without_hard_rejection():
    evidence = SimpleNamespace(
        directional_bias="long",
        directional_strength=Decimal("0.58"),
        derivatives_confirmation="neutral",
        derivatives_strength=Decimal("0"),
    )

    context = classify_execution_context("short", evidence)

    assert context["microstructure_headwind"] is True
    assert context["derivatives_opposition"] is False
    assert context["hard_conflict"] is False
    assert context["risk_multiplier"] == Decimal("0.60")
    assert context["class"] == "soft_microstructure_headwind"


def test_hard_execution_conflict_requires_independent_derivatives_opposition():
    evidence = SimpleNamespace(
        directional_bias="long",
        directional_strength=Decimal("0.72"),
        derivatives_confirmation="long",
        derivatives_strength=Decimal("0.31"),
    )

    context = classify_execution_context("short", evidence)

    assert context["microstructure_headwind"] is True
    assert context["derivatives_opposition"] is True
    assert context["hard_conflict"] is True
    assert context["risk_multiplier"] == Decimal("0")
    assert context["class"] == "multi_source_hard_conflict"


def test_family_fit_rejects_cross_family_signal_without_structure_support():
    assessment = MarketStateAssessment(
        selected_kind=StrategyKind.TREND,
        confidence=Decimal("0.984"),
        ambiguous=False,
        scores={
            "trend": Decimal("1.000"),
            "mean_reversion": Decimal("0.066"),
            "breakout": Decimal("0.686"),
        },
        reasons=[],
        features={},
    )

    trend = _strategy_family_fit(assessment, StrategyKind.TREND)
    mean_reversion = _strategy_family_fit(assessment, StrategyKind.MEAN_REVERSION)
    breakout = _strategy_family_fit(assessment, StrategyKind.BREAKOUT)

    assert trend[0] is True
    assert mean_reversion[0] is False
    assert mean_reversion[1] == Decimal("0.066")
    assert breakout[0] is True
