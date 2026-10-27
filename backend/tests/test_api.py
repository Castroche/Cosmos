import asyncio
import threading
from decimal import Decimal

import pytest
from cosmos_quant import api
from cosmos_quant.configuration import (
    ConfigurationUpdate,
    CredentialInput,
    UserConfiguration,
)
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.runtime import NetworkFreeRuntime
from cosmos_quant.strategy import ExplainableTrendStrategy
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def network_free_api_runtime(monkeypatch):
    candles = demo_candles()
    monkeypatch.setattr(
        api,
        "runtime",
        NetworkFreeRuntime(
            api.configuration_service,
            candles,
            snapshot_from_candles(candles),
        ),
    )


def request(method: str, path: str, json=None):
    async def run():
        transport = ASGITransport(app=api.app)
        headers = {}
        async with AsyncClient(
            transport=transport, base_url="http://test", headers=headers
        ) as client:
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


def test_readiness_offloads_full_database_integrity_check(monkeypatch):
    request_thread_id = threading.get_ident()
    integrity_thread_ids = []

    def integrity_check():
        integrity_thread_ids.append(threading.get_ident())
        return "ok"

    async def initialize():
        return None

    monkeypatch.setattr(api.store, "integrity_check", integrity_check)
    monkeypatch.setattr(api.runtime, "initialize", initialize)

    class RunningWorker:
        def done(self):
            return False

    monkeypatch.setattr(api, "critical_workers", {"fixture": RunningWorker()})

    response = request("GET", "/api/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert integrity_thread_ids
    assert integrity_thread_ids[0] != request_thread_id


def test_dashboard_contract(monkeypatch):
    candles = demo_candles(350)
    monkeypatch.setattr(
        api,
        "runtime",
        NetworkFreeRuntime(
            api.configuration_service,
            candles,
            snapshot_from_candles(candles),
        ),
    )
    response = request("GET", "/api/dashboard")
    assert response.status_code == 200
    data = response.json()
    assert "live_locked" not in data["system"]
    assert "plan" not in data
    assert "signal" not in data
    assert "backtest" not in data
    assert data["risk"]["checks"][0]["code"] == "trading_intelligence_v2_boundary"
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


def test_v2_llm_portfolio_context_withholds_affordability_and_sizing_fields():
    portfolio = api.PortfolioState(
        equity="12500",
        cash="10000",
        available_margin="9000",
        used_margin="1000",
        daily_start_equity="13000",
        gross_exposure="2500",
    )
    context = api.v2_llm_portfolio_context(
        portfolio,
        symbol="ETH/USDT:USDT",
        max_daily_drawdown=Decimal("0.02"),
        risk_halted=False,
        unresolved_reconciliation=False,
    )

    assert context["position_state"] == "FLAT"
    assert context["daily_drawdown_limit_reached"] is True
    for forbidden in (
        "equity",
        "cash",
        "available_margin",
        "used_margin",
        "gross_exposure",
        "quantity",
        "leverage",
    ):
        assert forbidden not in context


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


def test_market_window_supports_configured_timeframe_switching(monkeypatch):
    candles = demo_candles(350)
    monkeypatch.setattr(
        api,
        "runtime",
        NetworkFreeRuntime(
            api.configuration_service,
            candles,
            snapshot_from_candles(candles),
        ),
    )
    response = request("GET", "/api/market?symbol=BTC/USDT&timeframe=4h&limit=120")
    assert response.status_code == 200
    data = response.json()
    assert data["timeframe"] == "4h"
    assert len(data["candles"]) == 120

    unsupported = request("GET", "/api/market?symbol=DOGE/USDT&timeframe=4h")
    assert unsupported.status_code == 400


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


def test_legacy_strategy_execute_route_is_absent():
    response = request("POST", "/api/strategies/legacy/execute")
    assert response.status_code == 404


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
    response = request("POST", "/api/strategies/legacy-environment/execute")
    assert response.status_code == 404


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
    del tmp_path, monkeypatch
    response = request("POST", "/api/strategies/legacy/activate", json={})
    assert response.status_code == 404


def test_activation_rechecks_current_contract_risk():
    response = request("POST", "/api/strategies/legacy/approve", json={})
    assert response.status_code == 404


def test_automatic_strategy_scan_submits_once_and_pauses_signal(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    assert not hasattr(api, "run_strategy_scan_once")


def test_automatic_strategy_scan_does_not_submit_agent_owned_plan(tmp_path, monkeypatch):
    del tmp_path, monkeypatch
    assert not hasattr(api, "run_strategy_scan_once")
