import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cosmos_quant import api
from cosmos_quant.agent.policy import AgentPolicy
from cosmos_quant.agent.schemas import (
    AgentAction,
    AgentAutonomyMode,
    AgentDecision,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentObservation,
    AgentRun,
    TradeEpisode,
    TradeEpisodeStatus,
)
from cosmos_quant.ai_configuration import (
    AIConfiguration,
    AIConfigurationView,
    AIProvider,
    AIProviderCredentialState,
    AIProviderRuntimeState,
)
from cosmos_quant.configuration import (
    ConfigurationUpdate,
    ConfigurationView,
    CredentialState,
    UserConfiguration,
)
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import (
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    TradingMode,
)
from cosmos_quant.order_ledger import OrderRecord, ReconciliationReport
from cosmos_quant.risk_state import RiskControlState
from cosmos_quant.strategy import ExplainableTrendStrategy
from fastapi import HTTPException
from fastapi.testclient import TestClient

client = TestClient(api.app)


def test_agent_research_history_covers_one_year_for_core_timeframes():
    assert api.effective_research_bars(2000, "1h") == 8761
    assert api.effective_research_bars(2000, "4h") == 2191
    assert api.effective_research_bars(2000, "15m") == 2000
    assert api.effective_research_bars(10000, "1h") == 10000
    assert api.effective_research_bars(26281, "1h") == 26281
    assert api.effective_research_bars(60000, "1h") == 50000


def test_agent_policy_goal_and_event_control_plane():
    suffix = uuid4().hex[:8]
    policy_id = f"policy_{suffix}"
    policy = {
        "id": policy_id,
        "autonomy_mode": "research",
        "allowed_symbols": ["BTC/USDT"],
        "allowed_timeframes": ["1h"],
        "max_risk_per_trade": "0",
        "max_daily_drawdown": "0",
        "max_open_positions": 0,
        "max_trades_per_day": 0,
        "max_leverage": "1",
        "allow_agent_entry": False,
    }
    saved = client.put(
        f"/api/agent/policies/{policy_id}",
        json={
            "policy": policy,
            "confirmation": f"AUTHORIZE AGENT {policy_id} research",
        },
    )
    assert saved.status_code == 200, saved.text

    goal = client.post(
        "/api/agent/goals",
        json={
            "objective": "自动研究市场并在证据不足时保持等待",
            "symbols": ["BTC/USDT"],
            "timeframes": ["1h"],
            "autonomy_mode": "research",
            "risk_policy_id": policy_id,
        },
    )
    assert goal.status_code == 200, goal.text
    goal_id = goal.json()["id"]

    event = client.post(
        f"/api/agent/goals/{goal_id}/trigger",
        json={
            "event_type": "manual_triggered",
            "payload": {"symbol": "BTC/USDT", "timeframe": "1h"},
            "dedupe_key": f"manual:{goal_id}:first",
        },
    )
    duplicate = client.post(
        f"/api/agent/goals/{goal_id}/trigger",
        json={
            "event_type": "manual_triggered",
            "payload": {"symbol": "BTC/USDT", "timeframe": "1h"},
            "dedupe_key": f"manual:{goal_id}:first",
        },
    )
    assert event.status_code == 200
    assert duplicate.json()["id"] == event.json()["id"]

    updated = client.put(
        f"/api/agent/goals/{goal_id}",
        json={
            "objective": "持续分析授权市场并只处理当前指定周期",
            "symbols": ["BTC/USDT"],
            "timeframes": ["1h"],
            "poll_seconds": 120,
            "confirmation": f"UPDATE AGENT GOAL {goal_id}",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["poll_seconds"] == 120
    assert updated.json()["symbols"] == ["BTC/USDT"]

    state = client.get("/api/agent/state")
    assert state.status_code == 200
    assert any(item["id"] == goal_id for item in state.json()["goals"])
    assert "episodes" in state.json()
    assert "memories" in state.json()

    paused = client.put(
        f"/api/agent/goals/{goal_id}/status",
        json={"status": "paused", "confirmation": f"PAUSE AGENT {goal_id}"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    resumed = client.put(
        f"/api/agent/goals/{goal_id}/status",
        json={"status": "active", "confirmation": f"RESUME AGENT {goal_id}"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"


def test_agent_run_trace_exposes_reasoning_and_tool_evidence():
    goal = AgentGoal(
        objective="验证运行级模型理由、工具调用与观察证据的读取链路",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        risk_policy_id="policy_trace",
    )
    api.agent_store.save_goal(goal)
    event = api.agent_store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key=f"manual:trace:{uuid4().hex}",
        )
    )
    run = AgentRun(goal_id=goal.id, trigger_event_id=event.id)
    api.agent_store.save_run(run)
    decision = AgentDecision(
        run_id=run.id,
        action=AgentAction.WAIT,
        reason="确定性市场证据存在冲突，因此保持等待并避免扩大风险",
        evidence=["recommendation.action=wait"],
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
    )
    observation = AgentObservation(
        run_id=run.id,
        kind="tool_result",
        ok=True,
        summary="多周期市场状态分析完成",
        data={"recommendation": {"action": "wait"}},
    )
    api.agent_store.save_decision(decision)
    api.agent_store.save_observation(observation)

    response = client.get(f"/api/agent/runs/{run.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run"]["id"] == run.id
    assert payload["decisions"][0]["reason"] == decision.reason
    assert payload["observations"][0]["data"]["recommendation"]["action"] == "wait"


def test_agent_episode_runtime_exposes_fills_plan_protection_and_reconciliation(
    monkeypatch,
):
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(
        candles,
        market,
        Decimal("100000"),
    )
    episode = TradeEpisode(
        goal_id="runtime-visibility-goal",
        policy_id="runtime-visibility-policy",
        run_id="runtime-visibility-run",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.OPEN,
        entry_order_ids=["entry-visible"],
        planned_risk=Decimal("100"),
        opened_at=datetime.now(UTC),
    )
    order = Order(
        id="entry-visible",
        client_order_id="entry-visible-client",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.FILLED,
        quantity=Decimal("0.25"),
        filled_quantity=Decimal("0.25"),
        limit_price=Decimal("50000"),
        average_fill_price=Decimal("49990"),
        fee=Decimal("5"),
    )
    record = OrderRecord(
        exchange_id="okx:sandbox:swap",
        order=order,
        protection_verified=True,
    )
    monkeypatch.setattr(api, "plans", {plan.id: plan})
    monkeypatch.setattr(
        api.runtime,
        "order_ledger",
        SimpleNamespace(records={order.id: record}),
    )
    monkeypatch.setattr(
        api.runtime,
        "reconciliation_report",
        ReconciliationReport(),
    )

    view = api.build_agent_episode_runtime_views([episode])[0]

    assert view.entry_quantity == Decimal("0.25")
    assert view.average_entry == Decimal("49990")
    assert view.entry_fees == Decimal("5")
    assert view.stop_loss == plan.exit.stop_loss
    assert len(view.take_profits) == 2
    assert view.native_protection_status == "verified"
    assert view.protected_order_ids == [order.id]


def test_live_capped_policy_cannot_be_created_while_platform_is_not_live():
    policy_id = f"policy_{uuid4().hex[:8]}"
    response = client.put(
        f"/api/agent/policies/{policy_id}",
        json={
            "policy": {
                "id": policy_id,
                "autonomy_mode": "live_capped",
                "allowed_symbols": ["BTC/USDT"],
                "allowed_timeframes": ["1h"],
                "max_risk_per_trade": "0.001",
                "max_daily_drawdown": "0.01",
                "max_open_positions": 1,
                "max_trades_per_day": 2,
                "max_leverage": "1",
                "require_native_protection": True,
                "allow_agent_entry": True,
            },
            "confirmation": f"AUTHORIZE AGENT {policy_id} live_capped",
        },
    )

    assert response.status_code == 409
    assert "实盘模式" in response.json()["detail"]


def test_live_capped_entry_uses_strategy_evidence_not_elapsed_sandbox_time(
    monkeypatch,
):
    configuration = UserConfiguration(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
        contract_leverage="3",
    )
    monkeypatch.setattr(
        api,
        "configuration_service",
        SimpleNamespace(current_configuration=lambda: configuration),
    )
    monkeypatch.setattr(
        api,
        "agent_store",
        SimpleNamespace(
            list_policies=lambda _limit: [],
            list_episodes=lambda _limit: [],
        ),
    )
    policy = AgentPolicy(
        id="live-forward-locked",
        autonomy_mode=AgentAutonomyMode.LIVE_CAPPED,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.001",
        max_daily_drawdown="0.01",
        max_open_positions=1,
        max_trades_per_day=1,
        max_leverage="3",
        require_native_protection=True,
        allow_agent_entry=True,
    )

    api.validate_agent_policy_against_platform(policy)


def test_live_switch_prepares_agent_goal_promotion_from_forward_evidence(
    monkeypatch,
):
    previous = UserConfiguration(trading_mode=TradingMode.SANDBOX)
    source_policy = AgentPolicy(
        id="sandbox-promote-policy",
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
    source_goal = AgentGoal(
        id="sandbox-promote-goal",
        objective="Autonomously execute only fully validated BTC trading evidence",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id=source_policy.id,
    )
    episodes = []
    for index in range(30):
        opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        risk_multiple = Decimal("-0.4" if index % 3 == 0 else "0.5")
        episodes.append(
            TradeEpisode(
                goal_id=source_goal.id,
                policy_id=source_policy.id,
                run_id=f"run_{index}",
                plan_id=f"plan_{index}",
                symbol="BTC/USDT",
                side="long",
                status=TradeEpisodeStatus.CLOSED,
                planned_risk="100",
                realized_pnl=risk_multiple * Decimal("100"),
                fees="1",
                return_on_risk=risk_multiple,
                opened_at=opened,
                closed_at=opened + timedelta(hours=1),
            )
        )
    fake_store = SimpleNamespace(
        list_policies=lambda _limit: [source_policy],
        list_episodes=lambda _limit: episodes,
        load_goal=lambda goal_id: source_goal if goal_id == source_goal.id else None,
        load_policy=lambda policy_id: source_policy if policy_id == source_policy.id else None,
    )
    monkeypatch.setattr(
        api,
        "configuration_service",
        SimpleNamespace(current_configuration=lambda: previous),
    )
    monkeypatch.setattr(api, "agent_store", fake_store)
    target = previous.model_copy(
        update={
            "trading_mode": TradingMode.LIVE,
            "live_trading_enabled": True,
        }
    )
    update = ConfigurationUpdate(
        configuration=target,
        live_unlock_phrase="I ACCEPT LIVE TRADING RISK",
        promote_agent_goal_id=source_goal.id,
    )

    promotion = api.prepare_live_agent_promotion(update, previous)

    assert promotion is not None
    assert promotion.source_goal_id == source_goal.id
    assert promotion.live_policy.autonomy_mode == AgentAutonomyMode.LIVE_CAPPED
    assert promotion.live_policy.allow_agent_entry is True
    assert promotion.live_goal.autonomy_mode == AgentAutonomyMode.LIVE_CAPPED
    assert promotion.live_goal.risk_policy_id == promotion.live_policy.id


def test_live_switch_rejects_agent_promotion_with_open_episode(monkeypatch):
    previous = UserConfiguration(trading_mode=TradingMode.SANDBOX)
    source_policy = AgentPolicy(
        id="sandbox-open-policy",
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
    source_goal = AgentGoal(
        id="sandbox-open-goal",
        objective="Autonomously execute validated BTC evidence with bounded risk",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id=source_policy.id,
    )
    open_episode = TradeEpisode(
        goal_id=source_goal.id,
        policy_id=source_policy.id,
        run_id="run_open",
        plan_id="plan_open",
        symbol="BTC/USDT",
        side="long",
        status=TradeEpisodeStatus.OPEN,
        planned_risk="100",
        opened_at=datetime.now(UTC),
    )
    fake_store = SimpleNamespace(
        list_episodes=lambda _limit: [open_episode],
        load_goal=lambda goal_id: source_goal if goal_id == source_goal.id else None,
        load_policy=lambda policy_id: source_policy if policy_id == source_policy.id else None,
    )
    monkeypatch.setattr(
        api,
        "sandbox_forward_performance",
        lambda: SimpleNamespace(qualified_for_live_review=True),
    )
    monkeypatch.setattr(api, "agent_store", fake_store)
    target = previous.model_copy(
        update={
            "trading_mode": TradingMode.LIVE,
            "live_trading_enabled": True,
        }
    )
    update = ConfigurationUpdate(
        configuration=target,
        live_unlock_phrase="I ACCEPT LIVE TRADING RISK",
        promote_agent_goal_id=source_goal.id,
    )

    with pytest.raises(HTTPException, match="未闭环交易回合"):
        api.prepare_live_agent_promotion(update, previous)


def test_agent_entry_grant_cannot_cap_below_fixed_execution_leverage(monkeypatch):
    class Configuration:
        @staticmethod
        def current_configuration():
            return UserConfiguration(
                trading_mode=TradingMode.SANDBOX,
                contract_leverage="3",
            )

    monkeypatch.setattr(api, "configuration_service", Configuration())
    policy = AgentPolicy(
        id="impossible-leverage-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.001",
        max_daily_drawdown="0.01",
        max_open_positions=1,
        max_trades_per_day=1,
        max_leverage="2",
        allow_agent_entry=True,
    )

    with pytest.raises(HTTPException, match="固定执行杠杆"):
        api.validate_agent_policy_against_platform(policy)


def test_exchange_agent_entry_requires_position_management_commissioning(monkeypatch):
    sandbox = UserConfiguration(trading_mode=TradingMode.SANDBOX)
    service = SimpleNamespace(
        current_configuration=lambda: sandbox,
        commissioning_for=lambda _configuration: None,
    )
    monkeypatch.setattr(api, "configuration_service", service)

    with pytest.raises(RuntimeError, match="position-management commissioning"):
        api.require_agent_position_management_commissioning()

    service.current_configuration = lambda: UserConfiguration(trading_mode=TradingMode.PAPER)
    api.require_agent_position_management_commissioning()


def test_agent_readiness_reports_complete_paper_trading_loop(monkeypatch):
    configuration = UserConfiguration(trading_mode=TradingMode.PAPER)
    configuration_view = ConfigurationView(
        configuration=configuration,
        credentials=CredentialState(),
        execution_ready=True,
    )
    ai_configuration = AIConfiguration()
    ai_configuration.providers[0].enabled = True
    credentials = {
        provider: AIProviderCredentialState(api_key_configured=provider == AIProvider.OPENAI)
        for provider in AIProvider
    }
    runtimes = {
        provider: AIProviderRuntimeState(
            status="ready" if provider == AIProvider.OPENAI else "untested",
            selected_model_available=True if provider == AIProvider.OPENAI else None,
        )
        for provider in AIProvider
    }
    ai_view = AIConfigurationView(
        configuration=ai_configuration,
        credentials=credentials,
        runtime=runtimes,
    )
    policy = AgentPolicy(
        id="ready-policy",
        autonomy_mode=AgentAutonomyMode.PAPER,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.001",
        max_daily_drawdown="0.01",
        max_open_positions=1,
        max_trades_per_day=2,
        allow_agent_entry=True,
    )
    goal = AgentGoal(
        objective="Autonomously trade only after every deterministic gate passes",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.PAPER,
        risk_policy_id=policy.id,
    )
    fake_store = SimpleNamespace(
        list_goals=lambda _limit: [goal],
        list_policies=lambda _limit: [policy],
        list_episodes=lambda _limit: [],
    )
    monkeypatch.setattr(
        api, "configuration_service", SimpleNamespace(get=lambda: configuration_view)
    )
    monkeypatch.setattr(api, "ai_configuration_service", SimpleNamespace(get=lambda: ai_view))
    monkeypatch.setattr(api, "risk_state_service", SimpleNamespace(get=lambda: RiskControlState()))
    monkeypatch.setattr(api, "agent_store", fake_store)

    readiness = asyncio.run(api.agent_trading_readiness())

    assert readiness.ready_for_autonomous_trading is True
    assert readiness.mode == AgentAutonomyMode.PAPER
    assert all(check.passed for check in readiness.checks)


def test_agent_readiness_names_every_blocking_gate(monkeypatch):
    configuration = UserConfiguration(trading_mode=TradingMode.SANDBOX)
    configuration_view = ConfigurationView(
        configuration=configuration,
        credentials=CredentialState(),
        execution_ready=False,
    )
    ai_view = AIConfigurationView(
        configuration=AIConfiguration(),
        credentials={provider: AIProviderCredentialState() for provider in AIProvider},
        runtime={provider: AIProviderRuntimeState() for provider in AIProvider},
    )
    fake_store = SimpleNamespace(
        list_goals=lambda _limit: [],
        list_policies=lambda _limit: [],
        list_episodes=lambda _limit: [],
    )
    monkeypatch.setattr(
        api, "configuration_service", SimpleNamespace(get=lambda: configuration_view)
    )
    monkeypatch.setattr(api, "ai_configuration_service", SimpleNamespace(get=lambda: ai_view))
    monkeypatch.setattr(
        api,
        "risk_state_service",
        SimpleNamespace(
            get=lambda: RiskControlState(
                halted=True,
                reason="reconciliation required",
                source="test",
                halted_at=datetime.now(UTC),
            )
        ),
    )
    monkeypatch.setattr(api, "agent_store", fake_store)

    readiness = asyncio.run(api.agent_trading_readiness())
    checks = {check.code: check for check in readiness.checks}

    assert readiness.ready_for_autonomous_trading is False
    assert checks["ai_reasoner"].passed is False
    assert checks["execution_gateway"].passed is False
    assert checks["position_management"].passed is False
    assert checks["risk_gate"].passed is False
    assert checks["entry_policy"].passed is False
    assert checks["active_goal"].passed is False
    assert checks["episode_reconciliation"].passed is True


def test_agent_forward_performance_endpoint_scopes_sandbox_policies(monkeypatch):
    policy = AgentPolicy(
        id="sandbox-forward-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade="0.001",
        max_daily_drawdown="0.01",
        max_open_positions=1,
        max_trades_per_day=2,
        allow_agent_entry=True,
    )
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    episode = TradeEpisode(
        goal_id="sandbox-forward-goal",
        policy_id=policy.id,
        run_id="sandbox-forward-run",
        plan_id="sandbox-forward-plan",
        symbol="BTC/USDT",
        side="long",
        status=TradeEpisodeStatus.CLOSED,
        planned_risk="100",
        realized_pnl="50",
        fees="1",
        return_on_risk="0.5",
        opened_at=opened,
        closed_at=opened + timedelta(hours=1),
    )
    fake_store = SimpleNamespace(
        list_policies=lambda _limit: [policy],
        list_episodes=lambda _limit: [episode],
    )
    monkeypatch.setattr(
        api,
        "configuration_service",
        SimpleNamespace(current_configuration=lambda: UserConfiguration()),
    )
    monkeypatch.setattr(api, "agent_store", fake_store)

    report = asyncio.run(api.agent_forward_performance())

    assert report.scope_mode == AgentAutonomyMode.SANDBOX
    assert report.closed_episodes == 1
    assert report.attributable_episodes == 1
    assert report.status == "collecting"


def test_live_agent_readiness_requires_qualified_sandbox_forward_evidence(
    monkeypatch,
):
    configuration = UserConfiguration(
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
    )
    configuration_view = ConfigurationView(
        configuration=configuration,
        credentials=CredentialState(),
        execution_ready=True,
    )
    ai_view = AIConfigurationView(
        configuration=AIConfiguration(),
        credentials={provider: AIProviderCredentialState() for provider in AIProvider},
        runtime={provider: AIProviderRuntimeState() for provider in AIProvider},
    )
    fake_store = SimpleNamespace(
        list_goals=lambda _limit: [],
        list_policies=lambda _limit: [],
        list_episodes=lambda _limit: [],
    )
    monkeypatch.setattr(
        api, "configuration_service", SimpleNamespace(get=lambda: configuration_view)
    )
    monkeypatch.setattr(api, "ai_configuration_service", SimpleNamespace(get=lambda: ai_view))
    monkeypatch.setattr(api, "risk_state_service", SimpleNamespace(get=lambda: RiskControlState()))
    monkeypatch.setattr(api, "agent_store", fake_store)

    readiness = asyncio.run(api.agent_trading_readiness())
    checks = {check.code: check for check in readiness.checks}

    assert checks["sandbox_forward_validation"].passed is False
    assert "0 个完整回合" in checks["sandbox_forward_validation"].message
    assert readiness.ready_for_autonomous_trading is False
