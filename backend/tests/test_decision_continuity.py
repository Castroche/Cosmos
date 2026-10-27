"""Decision recovery uses temporary SQLite and simulated time, never a live venue."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cosmos_quant import api
from cosmos_quant.domain import PortfolioState, Position
from cosmos_quant.position_decisions import resolve_position_decision
from cosmos_quant.trading_intelligence import (
    AutomationRuntimeStatus,
    DecisionEpisode,
    DeltaContext,
    MarketAnalysis,
    PortfolioTraderDecision,
    PositionRuntimeV2,
    PositionStateV2,
    ThesisState,
    TradingModeV2,
    WaitPlan,
    WaitRuntimeV2,
    WakeAction,
    WakeCondition,
    WakeKind,
)
from cosmos_quant.trading_intelligence_automation import TradingIntelligenceAutomation
from cosmos_quant.trading_intelligence_engine import EventDrivenTradingEngine
from test_v2_automation_runtime import activate_plan, make_store, state


def position(**updates):
    return PositionRuntimeV2(
        position_id="owned-position", plan_id="plan_auto_test",
        symbol="BTC/USDT:USDT", side="LONG", state=PositionStateV2.OPEN,
        original_quantity="10", remaining_quantity="10", average_entry="100",
        initial_stop="98", current_stop="98", **updates,
    )


def account():
    return PortfolioState(
        equity="10000", cash="8000", daily_start_equity="10000", gross_exposure="1000",
        open_positions=[Position(
            symbol="BTC/USDT", side="long", quantity="10",
            average_entry="100", mark_price="101",
        )],
    )


def test_llm_context_contains_exact_owned_target_without_sizing_fields():
    owned = position(version=321)
    context = api.v2_llm_portfolio_context(
        account(), symbol=owned.symbol, max_daily_drawdown=Decimal("0.02"),
        risk_halted=False, unresolved_reconciliation=False, managed_positions=[owned],
    )
    row = context["symbol_positions"][0]
    assert row["position_id"] == owned.position_id
    assert row["position_version"] == 321
    assert row["side"] == owned.side
    assert row["symbol"] == owned.symbol
    assert not {"quantity", "equity", "cash", "leverage", "risk_amount"} & row.keys()


@pytest.mark.parametrize("updates", [
    {"remaining_quantity": Decimal("5")},
    {"average_entry": Decimal("101")},
    {"current_stop": Decimal("99")},
    {"targets_reached": 1},
    {"management_version": 2},
    {"state": PositionStateV2.CLOSING},
    {"exit_requested_at": datetime.now(UTC)},
    {"plan_id": "new-plan"},
])
def test_position_decision_rejects_material_changes(updates):
    snapshot = position()
    changed = snapshot.model_copy(update={"version": 2, **updates})
    with pytest.raises(ValueError):
        resolve_position_decision(snapshot.position_id, 1, [snapshot], [changed])


def test_position_decision_accepts_quote_updates_but_rejects_invented_version():
    snapshot = position()
    observed = snapshot.model_copy(update={
        "version": 19, "highest_mark": Decimal("104"),
        "lowest_mark": Decimal("99"), "updated_at": datetime.now(UTC),
    })
    assert resolve_position_decision(snapshot.position_id, 1, [snapshot], [observed]) == observed
    with pytest.raises(ValueError, match="snapshot"):
        resolve_position_decision(snapshot.position_id, 19, [snapshot], [observed])


@pytest.mark.parametrize("material_change", [False, True])
def test_management_api_revalidates_after_llm_latency(tmp_path, monkeypatch, material_change):
    store = make_store(tmp_path)
    plan = activate_plan(store)
    snapshot = position()
    store.save_position(snapshot)
    market = state(at=datetime.now(UTC))
    store.save_market_state(market)
    event_id, _ = store.append_event(
        dedupe_key="test-review", event_type="TEST_REVIEW", aggregate_type="symbol",
        aggregate_id=market.symbol, aggregate_version=1, payload={},
    )
    captured = []

    async def portfolio():
        return account()

    async def decide(**kwargs):
        target = kwargs["portfolio"]["symbol_positions"][0]
        captured.append(target)
        assert target["position_id"] == snapshot.position_id
        assert target["position_version"] == snapshot.version
        updates = {"highest_mark": Decimal("104")}
        if material_change:
            updates["remaining_quantity"] = Decimal("5")
        store.update_position(snapshot.position_id, expected_version=1, updates=updates)
        decision = PortfolioTraderDecision(
            action="MANAGE_EXISTING", rationale=["review existing position"],
            position_id=target["position_id"], position_version=target["position_version"],
            management_plan=plan.management.model_copy(update={"maximum_holding_minutes": 90}),
        )
        analysis = MarketAnalysis(
            market_state_hash=market.state_hash, data_as_of=market.data_as_of,
            regime=market.regime, summary="test market",
        )
        episode = DecisionEpisode(
            symbol=market.symbol, market_state_hash=market.state_hash,
            delta=DeltaContext(current_state_hash=market.state_hash),
        )
        return SimpleNamespace(decision=decision, analysis=analysis, episode=episode)

    monkeypatch.setattr(api, "trading_intelligence_store", store)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    monkeypatch.setattr(api.runtime, "reconciliation_report", None)
    monkeypatch.setattr(api.dual_llm_decision_service, "decide", decide)
    monkeypatch.setattr(api, "v2_entry_allocation_lock", asyncio.Lock())
    response = asyncio.run(api.invoke_trading_intelligence(api.TradingIntelligenceDecisionRequest(
        symbol=market.symbol, trigger="BOUNDED_RETRY", trigger_event_id=event_id,
    )))
    assert len(captured) == 1
    updated_plan = store.get_plan(plan.plan_id)
    if material_change:
        assert response["decision_error"] is True
        assert updated_plan.management.maximum_holding_minutes == 120
    else:
        assert not response.get("decision_error")
        assert updated_plan.management.maximum_holding_minutes == 90
        assert updated_plan.management.hard_stop == plan.management.hard_stop
        assert updated_plan.management.take_profits == plan.management.take_profits


def test_stranded_decision_recovers_after_repeated_errors_restart_and_72_hours(
    tmp_path, monkeypatch,
):
    clock = [datetime.now(UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0] if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr("cosmos_quant.trading_intelligence.datetime", Clock)
    monkeypatch.setattr("cosmos_quant.trading_intelligence_automation.datetime", Clock)
    store = make_store(tmp_path)
    plan = activate_plan(store)
    thesis = store.get_thesis(plan.thesis_id)
    store.transition_thesis(
        thesis.thesis_id, expected_version=thesis.version, target=ThesisState.CANCELLED
    )
    store.save_position(position())
    calls = []

    async def decide(symbol, *_):
        calls.append(clock[0])
        if len(calls) <= 2:
            return {"decision_error": True}
        return {"decision": {"action": "MANAGE_EXISTING"}}

    async def execute(*_):
        raise AssertionError("decision recovery must not manufacture an entry")

    def service():
        return TradingIntelligenceAutomation(
            store, EventDrivenTradingEngine(store), decide=decide, execute=execute,
        )

    automation = service()
    runtime = automation.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    store.save_automation_runtime(runtime.model_copy(update={
        "status": AutomationRuntimeStatus.DEGRADED, "consecutive_failures": 2,
        "symbol_failures": {"BTC/USDT:USDT": 2},
        "symbol_error_codes": {"BTC/USDT:USDT": "decision:DecisionContractError"},
        "symbol_next_retry_at": {"BTC/USDT:USDT": clock[0] - timedelta(minutes=1)},
    }))
    asyncio.run(automation.on_market_state(state(at=clock[0])))
    assert not calls
    assert len(store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)) == 1
    # Repeated failures keep a durable cooldown; restarting retains the next wake.
    for _ in range(3):
        waits = store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)
        clock[0] = waits[0].wait_plan.expires_at + timedelta(seconds=1)
        automation = service()
        asyncio.run(automation.on_market_state(state(at=clock[0])))
        assert len(store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)) == 1
    assert len(calls) == 3
    assert automation.runtime().status == AutomationRuntimeStatus.RUNNING
    assert not automation.runtime().symbol_failures
    clock[0] += timedelta(minutes=1)
    asyncio.run(automation.on_market_state(state(at=clock[0])))
    assert len(calls) == 3  # Routine market updates cannot bypass the review interval.
    for _ in range(144):
        waits = store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)
        clock[0] = waits[0].wait_plan.expires_at + timedelta(seconds=1)
        asyncio.run(automation.on_market_state(state(at=clock[0])))
        assert automation.runtime().status == AutomationRuntimeStatus.RUNNING
        assert len(store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)) == 1
    assert calls[-1] - calls[2] >= timedelta(hours=72)
    assert store.get_position("owned-position").remaining_quantity == 10
    automation.configure(enabled=False, requested_mode=TradingModeV2.OKX_DEMO)
    count = len(calls)
    clock[0] += timedelta(hours=1)
    asyncio.run(automation.on_market_state(state(at=clock[0])))
    assert len(calls) == count


def test_failed_bounded_retry_preserves_occupied_thesis_and_future_wake(tmp_path, monkeypatch):
    clock = datetime.now(UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = clock + timedelta(seconds=61)
            return value if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("cosmos_quant.trading_intelligence.datetime", Clock)
    monkeypatch.setattr("cosmos_quant.trading_intelligence_automation.datetime", Clock)
    store = make_store(tmp_path)
    plan = activate_plan(store)
    thesis = store.get_thesis(plan.thesis_id)
    store.save_thesis(thesis.model_copy(update={
        "created_at": clock, "expires_at": clock + timedelta(hours=2),
    }))
    store.save_position(position())
    wait = WaitRuntimeV2(
        episode_id="review", symbol=plan.symbol,
        wait_plan=WaitPlan(
            thesis_id=plan.thesis_id, reason="[BOUNDED_RETRY] review retry",
            wake_conditions=[WakeCondition(
                kind=WakeKind.EXPIRES_AT, action=WakeAction.RECALL_LLM, value=clock,
            )], expires_at=clock + timedelta(minutes=1),
        ),
    )
    store.save_wait_runtime(wait)

    async def decide(*_):
        return {"decision_error": True}

    async def execute(*_):
        raise AssertionError("a failed decision must not execute")

    automation = TradingIntelligenceAutomation(
        store, EventDrivenTradingEngine(store), decide=decide, execute=execute,
    )
    automation.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    asyncio.run(automation.on_market_state(state(at=clock + timedelta(seconds=61))))
    assert store.get_thesis(plan.thesis_id).state == ThesisState.ACTIVE
    waits = store.list_wait_runtimes(symbol=plan.symbol, active_only=True)
    assert len(waits) == 1
    assert waits[0].wait_plan.reason.startswith("[FAIL_CLOSED_COOLDOWN]")
