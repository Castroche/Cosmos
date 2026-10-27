from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal

from cosmos_quant import api
from cosmos_quant.domain import PortfolioState, Position
from cosmos_quant.portfolio_risk_v2 import portfolio_risk_snapshot
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    DecisionEpisode,
    DeltaContext,
    PositionRuntimeV2,
)
from cosmos_quant.trading_intelligence_engine import (
    DeterministicRiskEngine,
    RiskContext,
    RiskLimits,
)
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from cosmos_quant.trading_learning import learning_context, refresh_learning
from test_v2_execution import trade_plan


def portfolio(**updates):
    return PortfolioState(
        equity="10000", cash="8000", daily_start_equity="10000", gross_exposure="1000",
        **updates,
    )


def managed(symbol="BTC/USDT:USDT", **updates):
    return PositionRuntimeV2.model_validate({
        "plan_id": f"plan-{symbol}", "symbol": symbol, "side": "LONG", "state": "OPEN",
        "original_quantity": "10", "remaining_quantity": "10", "average_entry": "100",
        "initial_stop": "98", "current_stop": "98", **updates,
    })


def test_other_symbol_position_does_not_make_decision_symbol_occupied():
    account = portfolio(open_positions=[Position(
        symbol="BTC/USDT", side="long", quantity="10", average_entry="100", mark_price="101",
    )])
    context = api.v2_llm_portfolio_context(
        account, symbol="ETH/USDT:USDT", max_daily_drawdown=Decimal("0.02"),
        risk_halted=False, unresolved_reconciliation=False,
    )
    assert context["position_state"] == "FLAT"
    assert context["account_position_state"] == "OPEN"
    assert context["has_position_for_symbol"] is False
    assert context["symbol_positions"] == []
    assert len(context["open_positions"]) == 1


def test_actual_stop_risk_and_pending_reservations_are_not_double_counted():
    account = portfolio(open_positions=[Position(
        symbol="BTC/USDT", side="long", quantity="10", average_entry="100", mark_price="101",
    )])
    snapshot = portfolio_risk_snapshot(account, [
        managed(), managed("ETH/USDT:USDT", state="OPENING", remaining_quantity="0"),
        managed("SOL/USDT:USDT", state="CLOSED", remaining_quantity="0"),
    ], existing_position_stress=Decimal("0.02"))
    assert snapshot["open_positions"] == 2
    assert snapshot["risk_amount"] == Decimal("50")  # BTC 30 + ETH pending 20
    assert snapshot["risk_fraction"] == Decimal("0.005")
    assert not any(item["estimated"] for item in snapshot["positions"])


def test_partial_fills_reserve_unfilled_quantity_and_unknown_stops_use_existing_fallback():
    account = portfolio(open_positions=[
        Position(symbol="BTC/USDT", side="long", quantity="3",
                 average_entry="100", mark_price="101"),
        Position(symbol="SOL/USDT", side="short", quantity="1",
                 average_entry="100", mark_price="99"),
    ])
    snapshot = portfolio_risk_snapshot(account, [managed(
        state="PARTIALLY_FILLED", remaining_quantity="3",
    )], existing_position_stress=Decimal("0.02"))
    assert snapshot["open_positions"] == 2
    assert snapshot["risk_amount"] == Decimal("230")


def test_three_symbol_allocations_fit_the_same_original_risk_limits():
    positions = []
    limits = RiskLimits()
    for symbol in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"):
        snapshot = portfolio_risk_snapshot(
            portfolio(), positions, existing_position_stress=Decimal("0.02"),
        )
        plan = trade_plan().model_copy(update={"symbol": symbol, "plan_id": symbol})
        allocation = DeterministicRiskEngine().allocate(plan, RiskContext(
            equity="10000", available_margin="5000", liquidity_notional_cap="100000",
            open_positions=snapshot["open_positions"],
            portfolio_risk_used=snapshot["risk_fraction"],
        ), limits)
        assert allocation.risk_amount > 0
        positions.append(managed(symbol, state="OPENING", remaining_quantity="0",
                                 original_quantity=allocation.quantity, average_entry="100.5"))
    assert len(positions) == 3
    assert sum(p.original_quantity * Decimal("2.5") for p in positions) <= Decimal("500")


def test_margin_resizes_quantity_instead_of_refusing_affordable_smaller_order():
    allocation = DeterministicRiskEngine().allocate(trade_plan(), RiskContext(
        equity="10000", available_margin="100", liquidity_notional_cap="100000",
    ), RiskLimits())
    assert "available-margin-resized" in allocation.constraints
    assert allocation.notional <= Decimal("300")
    assert allocation.risk_amount <= Decimal("50")


def save_outcome(store, episode_id, pnl, *, complete=True, mode="OKX_DEMO", status="COMPLETED"):
    store.save_episode(DecisionEpisode(
        episode_id=episode_id, symbol="BTC/USDT:USDT", status=status,
        market_state_hash="entry-hash", delta=DeltaContext(current_state_hash="entry-hash"),
        outcome={"action": "LONG", "evidence_complete": complete, "trading_mode": mode,
                 "realized_pnl": pnl, "return_on_risk": str(Decimal(pnl) / 50),
                 "regime": "TREND_UP", "mfe": "2", "mae": "-1"},
    ))


def test_learning_replay_versions_cost_outcomes_and_mode_isolation(tmp_path):
    store = TradingIntelligenceStore(SqliteStore(tmp_path / "learning.db"))
    save_outcome(store, "win", "100")
    save_outcome(store, "loss", "-50")
    save_outcome(store, "live", "1000", mode="LIVE_AUTHORIZED")
    save_outcome(store, "partial", "9000", complete=False)
    save_outcome(store, "open", "9000", status="ORDERS_SUBMITTED")
    first = refresh_learning(store)
    second = refresh_learning(store)
    assert first == second
    assert len(store.list_artifacts("TradingLearningVersion")) == 1
    assert first.sample_count == 3 and first.skipped_count == 1
    context = learning_context(store, "BTC/USDT:USDT", "OKX_DEMO")
    assert len(context["groups"]) == 1
    group = context["groups"][0]
    assert group["sample_count"] == 2
    assert Decimal(group["expectancy_r"]) == Decimal("0.5")
    assert Decimal(group["posterior_win_rate"]) == Decimal("0.5")
    assert context["policy"] == "ADVISORY_ONLY"
    assert learning_context(store, "ETH/USDT:USDT", "OKX_DEMO")["groups"] == []
    save_outcome(store, "next", "-50")
    assert refresh_learning(store).version != first.version
    restarted = TradingIntelligenceStore(SqliteStore(tmp_path / "learning.db"))
    assert refresh_learning(restarted) == refresh_learning(store)


def test_execution_allocation_is_serialized_without_rejecting_other_symbols(monkeypatch):
    active = 0
    maximum_active = 0

    async def execute(plan_id, request):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return plan_id

    monkeypatch.setattr(api, "_execute_trading_intelligence_plan", execute)
    monkeypatch.setattr(api, "v2_entry_allocation_lock", asyncio.Lock())

    async def run():
        return await asyncio.gather(*(
            api.execute_trading_intelligence_plan(symbol, None)
            for symbol in ("BTC", "ETH", "SOL")
        ))

    assert asyncio.run(run()) == ["BTC", "ETH", "SOL"]
    assert maximum_active == 1


def test_slow_btc_refresh_does_not_block_eth_or_sol(monkeypatch):
    reached = set()

    async def run():
        all_started = asyncio.Event()

        async def refresh(symbol):
            reached.add(symbol)
            if len(reached) == 3:
                all_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(api, "refresh_trading_intelligence_market_state", refresh)
        task = asyncio.create_task(api.trading_intelligence_market_state_loop())
        try:
            await asyncio.wait_for(all_started.wait(), timeout=2)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    asyncio.run(run())
    assert reached == set(api.V2_SYMBOLS)
