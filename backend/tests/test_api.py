import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant import api
from cosmos_quant.configuration import (
    ConfigurationUpdate,
    CredentialInput,
    UserConfiguration,
)
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import PlanStatus, PriceZone
from cosmos_quant.risk_state import RiskStateService
from cosmos_quant.runtime import NetworkFreeRuntime
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy import ExplainableTrendStrategy
from httpx import ASGITransport, AsyncClient


def request(method: str, path: str, json=None):
    async def run():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json)

    return asyncio.run(run())


def mark_latest_backtest_robust(plan_id: str) -> None:
    payload = api.store.load_latest_backtest(plan_id)
    artifact = api.BacktestResult.model_validate(payload)
    artifact.robustness_passed = True
    artifact.validation_reasons = ["test fixture passed independent research review"]
    api.store.save_backtest(plan_id, artifact)


def test_structured_audit_diagnostics_are_serialized(monkeypatch):
    persisted = []
    monkeypatch.setattr(api.store, "append_audit", persisted.append)

    api.add_audit(
        "agent",
        "candidate pool",
        family_counts={"trend": 2, "breakout": 1},
        score=Decimal("0.75"),
    )

    event = persisted[0]
    assert event.data["family_counts"] == '{"breakout": 1, "trend": 2}'
    assert event.data["score"] == "0.75"
    api.audit.remove(event)


def test_startup_runtime_initialization_is_fail_closed_and_retryable(
    monkeypatch,
):
    attempts = 0
    audits = []

    async def unavailable():
        nonlocal attempts
        attempts += 1
        raise TimeoutError("temporary private API timeout")

    monkeypatch.setattr(api.runtime, "initialize", unavailable)
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: audits.append((args, kwargs)))

    initialized = asyncio.run(api.initialize_runtime_on_startup())

    assert initialized is False
    assert attempts == 1
    assert audits[0][0][0] == "runtime"
    assert audits[0][1]["severity"] == "warning"
    assert "temporary private API timeout" in audits[0][1]["error"]


def test_dashboard_contract(monkeypatch):
    candles = demo_candles(350)
    monkeypatch.setattr(api.runtime, "_candles", candles)
    monkeypatch.setattr(api.runtime, "_snapshot", snapshot_from_candles(candles))
    response = request("GET", "/api/dashboard")
    assert response.status_code == 200
    data = response.json()
    assert data["system"]["live_locked"] is True
    assert data["plan"]["human_summary"]
    assert data["risk"]["checks"]
    assert len(data["candles"]) == 300


def test_non_paper_strategy_sizing_uses_exchange_equity():
    configuration = UserConfiguration(trading_mode="sandbox")
    portfolio = api.PortfolioState(
        equity="12500",
        cash="10000",
        available_margin="10000",
        daily_start_equity="12500",
        gross_exposure="0",
    )
    assert api.strategy_sizing_equity(configuration, portfolio, Decimal("100000")) == Decimal(
        "12500"
    )


def test_default_configuration_is_contract_first():
    configuration = UserConfiguration()
    assert configuration.market_type == "swap"
    assert configuration.margin_mode == "cross"
    assert configuration.contract_leverage == Decimal("3")
    assert configuration.risk.max_gross_exposure == Decimal("2.0")

    with pytest.raises(ValueError, match="whole number"):
        UserConfiguration(contract_leverage="2.5")


def test_research_candidate_endpoint_keeps_ai_output_non_executable():
    response = request(
        "POST",
        "/api/research/candidates",
        json={
            "strategy_spec": {
                "family": "trend",
                "source": "ai",
                "hypothesis": "仅提出候选，不生成订单",
                "parameters": {},
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "ai"
    assert payload["status"] == "proposed"
    assert payload["backtest"] is None

    direct = request(
        "POST",
        "/api/strategies/generate",
        json={"strategy_kind": "trend", "strategy_spec": payload["spec"]},
    )
    assert direct.status_code == 422


def test_contract_plan_is_shrunk_to_available_margin_before_review():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    plan.market_type = "swap"
    plan.margin_mode = "cross"
    plan.leverage = Decimal("3")
    portfolio = api.PortfolioState(
        equity="1000",
        cash="100",
        available_margin="100",
        daily_start_equity="1000",
        gross_exposure="0",
    )

    constrained = api.constrain_plan_to_portfolio(plan, portfolio, UserConfiguration())

    assert constrained.sizing.notional <= Decimal("294")
    assert constrained.sizing.risk_amount < Decimal("5")
    assert "可用保证金约束" in constrained.reasons[-1]


def test_existing_operator_draft_is_resized_but_agent_plan_is_not_reused(
    monkeypatch,
):
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    configuration = api.configuration_service.get().configuration
    selected_kind = api.configured_strategy_kind(
        configuration,
        api.closed_candles(
            candles,
            configuration.default_timeframe,
            market.timestamp,
        ),
    )

    def matching_plan():
        plan = ExplainableTrendStrategy().generate(
            candles,
            market,
            Decimal("100000"),
        )
        plan.algorithm = selected_kind.value
        plan.exchange = configuration.exchange_id
        plan.trading_mode = configuration.trading_mode
        plan.market_type = configuration.market_type
        plan.margin_mode = str(configuration.margin_mode)
        plan.leverage = configuration.contract_leverage
        plan.symbol = configuration.default_symbol
        plan.timeframe = configuration.default_timeframe
        plan.entry.expires_at = datetime.now(UTC) + timedelta(hours=1)
        return plan

    operator_plan = matching_plan()
    operator_plan.status = PlanStatus.DRAFT
    agent_plan = matching_plan()
    agent_plan.status = PlanStatus.ACTIVE
    agent_plan.decision_origin = "agent"
    agent_plan.agent_goal_id = "goal-owned-by-agent"
    monkeypatch.setattr(
        api,
        "plans",
        {
            operator_plan.id: operator_plan,
            agent_plan.id: agent_plan,
        },
    )
    saved = []
    monkeypatch.setattr(api.store, "save_strategy", saved.append)
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)
    portfolio = api.PortfolioState(
        equity="1000",
        cash="100",
        available_margin="100",
        daily_start_equity="1000",
        gross_exposure="0",
    )

    selected = api.ensure_plan(candles, market, portfolio)

    assert selected is operator_plan
    assert selected.sizing.notional <= Decimal("294")
    assert saved == [operator_plan]
    assert agent_plan.sizing.notional > selected.sizing.notional


def test_expired_uncommitted_operator_plan_is_archived_before_reuse(
    monkeypatch,
):
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    configuration = api.configuration_service.get().configuration
    selected_kind = api.configured_strategy_kind(
        configuration,
        api.closed_candles(
            candles,
            configuration.default_timeframe,
            market.timestamp,
        ),
    )

    def matching_plan(expires_at):
        plan = ExplainableTrendStrategy().generate(
            candles,
            market,
            Decimal("100000"),
        )
        plan.algorithm = selected_kind.value
        plan.exchange = configuration.exchange_id
        plan.trading_mode = configuration.trading_mode
        plan.market_type = configuration.market_type
        plan.margin_mode = str(configuration.margin_mode)
        plan.leverage = configuration.contract_leverage
        plan.symbol = configuration.default_symbol
        plan.timeframe = configuration.default_timeframe
        plan.status = PlanStatus.DRAFT
        plan.entry.expires_at = expires_at
        return plan

    expired = matching_plan(datetime.now(UTC) - timedelta(seconds=1))
    current = matching_plan(datetime.now(UTC) + timedelta(hours=1))
    monkeypatch.setattr(
        api,
        "plans",
        {expired.id: expired, current.id: current},
    )
    saved = []
    monkeypatch.setattr(api.store, "save_strategy", saved.append)
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)
    portfolio = api.PortfolioState(
        equity="100000",
        cash="100000",
        available_margin="100000",
        daily_start_equity="100000",
        gross_exposure="0",
    )

    selected = api.ensure_plan(candles, market, portfolio)

    assert expired.status == PlanStatus.ARCHIVED
    assert selected is current
    assert saved == [expired]


def test_market_window_supports_configured_timeframe_switching():
    response = request("GET", "/api/market?symbol=BTC/USDT&timeframe=4h&limit=120")
    assert response.status_code == 200
    data = response.json()
    assert data["timeframe"] == "4h"
    assert len(data["candles"]) == 120

    unsupported = request("GET", "/api/market?symbol=DOGE/USDT&timeframe=4h")
    assert unsupported.status_code == 400


def test_agent_state_labels_forming_candle_and_excludes_it_from_signal_evidence():
    candles = [
        api.Candle(
            timestamp=datetime(2026, 7, 28, hour, tzinfo=UTC),
            open="100",
            high="102",
            low="99",
            close=str(100 + hour),
            volume="10",
        )
        for hour in (8, 9, 10)
    ]
    market = snapshot_from_candles(candles).model_copy(
        update={"timestamp": datetime(2026, 7, 28, 10, 2, tzinfo=UTC)}
    )
    trigger = api.AgentEvent(
        event_type=api.AgentEventType.SCHEDULE_TRIGGERED,
        source="test",
        goal_id="goal",
        dedupe_key="test:forming-candle",
        payload={"symbol": "BTC/USDT", "timeframe": "1h"},
    )

    state = api.build_agent_market_state(
        market,
        candles,
        symbol="BTC/USDT",
        timeframe="1h",
        trigger=trigger,
    )

    assert [
        datetime.fromisoformat(item["timestamp"]).hour for item in state["recent_closed_candles"]
    ] == [8, 9]
    assert state["closed_candle_count"] == 2
    assert datetime.fromisoformat(state["signal_data_end"]).hour == 9
    assert datetime.fromisoformat(state["forming_candle"]["timestamp"]).hour == 10
    assert state["forming_candle"]["is_closed"] is False
    assert state["forming_candle"]["role"] == "live_context_only_not_signal_evidence"


def test_agent_memory_requires_every_declared_market_dimension_to_match():
    episodic = api.AgentMemory(
        memory_type=api.MemoryType.EPISODIC,
        content="ETH/USDT 1h mean-reversion outcome",
        tags=["ETH/USDT", "1h", "long", "mean_reversion"],
        confidence="1",
    )
    operational = api.AgentMemory(
        memory_type=api.MemoryType.OPERATIONAL,
        content="Reconciliation transport lesson",
        tags=["reconciliation"],
        confidence="1",
    )

    assert api.memory_relevant_to_market(
        episodic,
        symbol="ETH/USDT",
        timeframe="1h",
    )
    assert not api.memory_relevant_to_market(
        episodic,
        symbol="BTC/USDT",
        timeframe="1h",
    )
    assert not api.memory_relevant_to_market(
        episodic,
        symbol="ETH/USDT",
        timeframe="4h",
    )
    assert api.memory_relevant_to_market(
        operational,
        symbol="BTC/USDT",
        timeframe="4h",
    )


def test_signal_gate_reports_the_actual_lifecycle_failure():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(
        candles,
        market,
        Decimal("100000"),
    )
    plan.entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    signal = api.evaluate_current_plan_signal(plan, candles, market)

    decision = api.add_signal_gate(
        api.RiskDecision(approved=True, checks=[]),
        plan,
        signal,
    )

    check = decision.checks[-1]
    assert check.code == "signal_lifecycle"
    assert check.passed is False
    assert "过期" in check.message
    assert "禁止执行" not in check.message


def test_strategy_market_fit_explains_latest_closed_data_alignment():
    plan = request(
        "POST",
        "/api/strategies/generate",
        json={
            "strategy_kind": "trend",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "prompt": "验证固定策略与最新已收盘行情之间的适配状态。",
        },
    ).json()

    response = request("GET", f"/api/strategies/{plan['id']}/market-fit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy_fingerprint"] == plan["fingerprint"]
    assert payload["verdict"] in {"aligned", "wait", "conflict"}
    assert payload["assessment"]["selected_kind"] in {
        "trend",
        "mean_reversion",
        "breakout",
    }
    assert payload["signal"]["algorithm"] == plan["algorithm"]
    assert payload["summary"]


def test_execution_requires_activation():
    plan = request("POST", "/api/strategies/generate", json={}).json()
    response = request("POST", f"/api/strategies/{plan['id']}/execute")
    assert response.status_code == 409


def test_strategy_spec_contract_and_validation_are_exposed():
    contract = request("GET", "/api/research/strategy-spec")
    assert contract.status_code == 200
    assert set(contract.json()["defaults"]) == {
        "trend",
        "mean_reversion",
        "breakout",
    }

    invalid = request(
        "POST",
        "/api/strategies/generate",
        json={
            "strategy_kind": "trend",
            "strategy_spec": {
                "family": "breakout",
                "parameters": {},
            },
        },
    )
    assert invalid.status_code == 422
    assert "策略规格无效" in invalid.json()["detail"]


def test_cancel_order_rejects_unknown_and_terminal_orders():
    unknown = request("POST", "/api/orders/missing/cancel")
    assert unknown.status_code == 404

    plan = request("POST", "/api/strategies/generate", json={}).json()
    order = api.Order(
        id="terminal-order",
        client_order_id="terminal-client",
        plan_id=plan["id"],
        symbol="BTC/USDT",
        side="buy",
        order_type="limit",
        status="canceled",
        quantity="0.01",
        limit_price="60000",
    )
    assert api.runtime.order_ledger is not None
    api.runtime.order_ledger.record(order, protection_verified=True)

    terminal = request("POST", "/api/orders/terminal-order/cancel")

    assert terminal.status_code == 409
    assert "不允许撤销" in terminal.json()["detail"]


def test_cancel_order_confirms_exchange_state_and_updates_ledger():
    plan = request("POST", "/api/strategies/generate", json={}).json()
    order = api.Order(
        id="working-order",
        client_order_id="working-client",
        plan_id=plan["id"],
        symbol="BTC/USDT",
        side="buy",
        order_type="limit",
        status="open",
        quantity="0.01",
        limit_price="60000",
    )
    assert api.runtime.order_ledger is not None
    assert api.runtime.execution is not None
    api.runtime.order_ledger.record(order, protection_verified=True)
    api.runtime.execution.orders[order.id] = order

    response = request("POST", f"/api/orders/{order.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "canceled"
    assert api.runtime.order_ledger.records[order.id].order.status == "canceled"


def test_execution_rejects_strategy_from_another_environment():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    plan.status = PlanStatus.ACTIVE
    plan.exchange = "binance"
    api.plans[plan.id] = plan

    response = request("POST", f"/api/strategies/{plan.id}/execute")

    assert response.status_code == 409
    assert "执行环境不一致" in response.json()["detail"]
    api.plans.pop(plan.id, None)


def test_environment_switch_is_rejected_while_old_gateway_has_open_orders(monkeypatch):
    current = UserConfiguration(
        trading_mode="live",
        live_trading_enabled=True,
    )

    class GatewayWithOpenOrder:
        async def open_orders(self):
            return [object()]

    async def initialized(force=False):
        del force

    async def portfolio():
        return type("Portfolio", (), {"open_positions": []})()

    monkeypatch.setattr(api.configuration_service, "current_configuration", lambda: current)
    monkeypatch.setattr(api.runtime, "initialize", initialized)
    monkeypatch.setattr(api.runtime, "execution", GatewayWithOpenOrder())
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)

    with pytest.raises(api.HTTPException) as raised:
        asyncio.run(api.update_settings(ConfigurationUpdate(configuration=UserConfiguration())))

    assert raised.value.status_code == 409
    assert "仍有挂单" in raised.value.detail


def test_failed_runtime_switch_rolls_back_configuration_and_credentials(monkeypatch):
    previous = api.configuration_service.current_configuration()
    candidate = UserConfiguration(trading_mode="sandbox", market_type="swap")
    api.configuration_service.restore_credentials(
        candidate,
        {
            "api_key": "old-key",
            "secret": "old-secret",
            "passphrase": "old-passphrase",
        },
    )
    attempts = 0

    async def initialize(force=False):
        nonlocal attempts
        del force
        attempts += 1
        if attempts == 1:
            raise RuntimeError("candidate account mode is invalid")

    monkeypatch.setattr(api.runtime, "initialize", initialize)

    with pytest.raises(api.HTTPException, match="candidate account mode is invalid"):
        asyncio.run(
            api.update_settings(
                ConfigurationUpdate(
                    configuration=candidate,
                    credentials=CredentialInput(
                        api_key="new-key",
                        secret="new-secret",
                        passphrase="new-passphrase",
                    ),
                )
            )
        )

    assert api.configuration_service.current_configuration() == previous
    assert api.configuration_service.credentials_for(candidate) == {
        "api_key": "old-key",
        "secret": "old-secret",
        "passphrase": "old-passphrase",
    }


def test_strategy_requires_version_bound_backtest_before_activation(
    tmp_path,
    monkeypatch,
):
    candles = demo_candles()
    monkeypatch.setattr(api, "plans", {})
    monkeypatch.setattr(
        api,
        "risk_state_service",
        RiskStateService(SqliteStore(tmp_path / "strategy-activation.db")),
    )
    monkeypatch.setattr(
        api,
        "runtime",
        NetworkFreeRuntime(
            api.configuration_service,
            candles,
            snapshot_from_candles(candles),
        ),
    )

    draft = request(
        "POST",
        "/api/strategies/generate",
        json={"strategy_kind": "trend", "prompt": "仅在趋势确认后执行。"},
    ).json()

    activate_draft = request(
        "POST",
        f"/api/strategies/{draft['id']}/activate",
        json={"confirmation": f"ACTIVATE {draft['symbol']}"},
    )
    assert activate_draft.status_code == 409

    approve_without_test = request(
        "POST",
        f"/api/strategies/{draft['id']}/approve",
        json={"confirmation": f"APPROVE {draft['fingerprint']}"},
    )
    assert approve_without_test.status_code == 409

    backtest = request("POST", f"/api/strategies/{draft['id']}/backtest")
    assert backtest.status_code == 200
    artifact = backtest.json()
    assert artifact["dataset_id"].startswith("ds_")
    dataset = request("GET", f"/api/research/datasets/{artifact['dataset_id']}")
    assert dataset.status_code == 200
    assert dataset.json()["data_fingerprint"] == artifact["data_fingerprint"]
    assert artifact["strategy_fingerprint"] == draft["fingerprint"]
    assert artifact["data_fingerprint"]
    assert artifact["cost_model"]["direction_filter"] == draft["side"]
    assert all(trade["side"] == draft["side"] for trade in artifact["trades"])

    latest = request("GET", f"/api/strategies/{draft['id']}/backtest")
    assert latest.status_code == 200
    assert latest.json()["data_fingerprint"] == artifact["data_fingerprint"]

    approval = request(
        "POST",
        f"/api/strategies/{draft['id']}/approve",
        json={"confirmation": f"APPROVE {draft['fingerprint']}"},
    )
    assert approval.status_code == 409
    assert "稳健性验收未通过" in approval.json()["detail"]

    mark_latest_backtest_robust(draft["id"])
    approval = request(
        "POST",
        f"/api/strategies/{draft['id']}/approve",
        json={"confirmation": f"APPROVE {draft['fingerprint']}"},
    )
    assert approval.status_code == 200
    approved = approval.json()
    assert approved["status"] == "approved"
    assert approved["approved_fingerprint"] == approved["fingerprint"]

    activation = request(
        "POST",
        f"/api/strategies/{draft['id']}/activate",
        json={"confirmation": f"ACTIVATE {draft['symbol']}"},
    )
    assert activation.status_code == 200, activation.json()
    assert activation.json()["status"] == "active"

    revision = request(
        "POST",
        "/api/strategies/generate",
        json={
            "strategy_kind": "trend",
            "prompt": "提高突破确认要求后生成新版本。",
            "revision_of": draft["id"],
        },
    )
    assert revision.status_code == 200
    revised = revision.json()
    assert revised["version"] == draft["version"] + 1
    assert revised["lineage_id"] == draft["lineage_id"]
    assert revised["supersedes_plan_id"] == draft["id"]
    assert revised["status"] == "draft"


def test_activation_rechecks_current_contract_risk():
    draft = request(
        "POST",
        "/api/strategies/generate",
        json={
            "equity": "10000000",
            "risk_fraction": "0.03",
            "prompt": "使用超大纸面权益生成计划以验证激活阶段会重新执行组合风控。",
        },
    ).json()
    request("POST", f"/api/strategies/{draft['id']}/backtest")
    mark_latest_backtest_robust(draft["id"])
    approved = request(
        "POST",
        f"/api/strategies/{draft['id']}/approve",
        json={"confirmation": f"APPROVE {draft['fingerprint']}"},
    )
    assert approved.status_code == 200

    activation = request(
        "POST",
        f"/api/strategies/{draft['id']}/activate",
        json={"confirmation": f"ACTIVATE {draft['symbol']}"},
    )
    assert activation.status_code == 409
    assert "策略未通过激活风控" in activation.json()["detail"]


def test_automatic_strategy_scan_submits_once_and_pauses_signal(tmp_path, monkeypatch):
    candles = demo_candles()
    monkeypatch.setattr(api, "plans", {})
    monkeypatch.setattr(
        api,
        "risk_state_service",
        RiskStateService(SqliteStore(tmp_path / "automatic-scan.db")),
    )
    monkeypatch.setattr(
        api,
        "runtime",
        NetworkFreeRuntime(
            api.configuration_service,
            candles,
            snapshot_from_candles(candles),
        ),
    )

    async def scenario():
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(
            candles,
            market,
            Decimal("100000"),
            risk_fraction=Decimal("0.005"),
            minimum_reward_risk=Decimal("1.8"),
        )
        plan.status = PlanStatus.ACTIVE
        configuration = api.configuration_service.get().configuration
        plan.exchange = configuration.exchange_id
        plan.trading_mode = configuration.trading_mode
        plan.market_type = configuration.market_type
        plan.margin_mode = str(configuration.margin_mode)
        plan.leverage = configuration.contract_leverage
        plan.entry.zone = PriceZone(lower=market.bid, upper=market.ask)
        plan.entry.expires_at = datetime.now(UTC) + timedelta(hours=1)
        api.plans[plan.id] = plan
        await api.runtime.initialize()
        before = sum(order.plan_id == plan.id for order in api.runtime.execution.orders.values())

        await api.run_strategy_scan_once(True)

        assert plan.status == "paused"
        assert api.runtime.execution is not None
        after = sum(order.plan_id == plan.id for order in api.runtime.execution.orders.values())
        assert after == before + 1
        api.plans.pop(plan.id, None)

    asyncio.run(scenario())


def test_automatic_strategy_scan_does_not_submit_agent_owned_plan(tmp_path, monkeypatch):
    candles = demo_candles()
    monkeypatch.setattr(api, "plans", {})
    monkeypatch.setattr(
        api,
        "risk_state_service",
        RiskStateService(SqliteStore(tmp_path / "agent-scan-isolation.db")),
    )
    monkeypatch.setattr(
        api,
        "runtime",
        NetworkFreeRuntime(
            api.configuration_service,
            candles,
            snapshot_from_candles(candles),
        ),
    )

    async def scenario():
        market = snapshot_from_candles(candles)
        plan = ExplainableTrendStrategy().generate(
            candles,
            market,
            Decimal("100000"),
            risk_fraction=Decimal("0.005"),
            minimum_reward_risk=Decimal("1.8"),
        )
        plan.status = PlanStatus.ACTIVE
        plan.decision_origin = "agent"
        plan.agent_goal_id = "goal-owned-by-agent"
        configuration = api.configuration_service.get().configuration
        plan.exchange = configuration.exchange_id
        plan.trading_mode = configuration.trading_mode
        plan.market_type = configuration.market_type
        plan.margin_mode = str(configuration.margin_mode)
        plan.leverage = configuration.contract_leverage
        plan.entry.zone = PriceZone(lower=market.bid, upper=market.ask)
        plan.entry.expires_at = datetime.now(UTC) + timedelta(hours=1)
        api.plans[plan.id] = plan
        await api.runtime.initialize()
        assert api.runtime.execution is not None
        before = len(api.runtime.execution.orders)

        await api.run_strategy_scan_once(True)

        assert plan.status == PlanStatus.ACTIVE
        assert len(api.runtime.execution.orders) == before

    asyncio.run(scenario())


history = demo_candles()
api.runtime = NetworkFreeRuntime(api.configuration_service, history, snapshot_from_candles(history))
app = api.app
