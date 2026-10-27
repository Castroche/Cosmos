"""Immediate contract tests: in-memory gateways and temporary SQLite only."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cosmos_quant.domain import PortfolioState, Position
from cosmos_quant.portfolio_risk_v2 import portfolio_risk_snapshot
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    ActiveThesis,
    DeltaContext,
    FeatureGroup,
    MarketAnalysis,
    MarketStateV2,
    PortfolioTraderDecision,
    PositionRuntimeV2,
    TradePlan,
    WaitPlan,
    WakeCondition,
)
from cosmos_quant.trading_intelligence_engine import (
    DeterministicRiskEngine,
    DualLLMDecisionService,
    EventDrivenTradingEngine,
    RiskContext,
    RiskLimits,
    decision_policy_errors,
    new_decision_contract_errors,
    normalize_direct_trade_decision,
)
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from pydantic import ValidationError

NOW = datetime(2026, 9, 6, tzinfo=UTC)
SYMBOL = "BTC/USDT:USDT"


def market():
    group = FeatureGroup(health="healthy", values={})
    return MarketStateV2(
        symbol=SYMBOL,
        data_as_of=NOW,
        regime="TREND_UP",
        market=FeatureGroup(health="healthy", values={"last": Decimal("100")}),
        **{
            name: group
            for name in (
                "structure",
                "trend",
                "momentum",
                "volatility",
                "volume",
                "microstructure",
                "derivatives",
                "cross_market",
                "events",
            )
        },
    )


def trade(**updates):
    payload = {
        "thesis_id": "thesis-immediate",
        "symbol": SYMBOL,
        "side": "LONG",
        "order_type": "LIMIT",
        "limit_price": "100",
        "valid_price_range": {"lower": "95", "upper": "105"},
        "entry_zone": {"lower": "98", "upper": "106"},
        "management": {
            "hard_stop": "95",
            "take_profits": [
                {"price": "110", "close_fraction": "1", "rationale": "structural target"},
            ],
        },
        "minimum_reward_risk": "2",
        "rationale": ["current opportunity"],
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    payload.update(updates)
    return TradePlan.model_validate(payload)


def decision(plan=None):
    plan = plan or trade()
    thesis = ActiveThesis(
        thesis_id=plan.thesis_id,
        symbol=SYMBOL,
        direction=plan.side,
        statement="current structure",
        supporting_evidence=[
            {"kind": "market_state", "reference": "state"},
        ],
        invalidation_rules=["stop breached"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    return PortfolioTraderDecision(
        action=plan.side,
        rationale=["current opportunity"],
        thesis=thesis,
        trade_plan=plan,
    )


def legacy():
    return trade(
        order_type=None,
        limit_price=None,
        valid_price_range=None,
        entry_conditions=[
            {"kind": "PRICE_IN_ZONE", "action": "EXECUTE_ENTRY", "lower": "98", "upper": "106"}
        ],
    )


def context(**updates):
    values = dict(equity="10000", available_margin="10000", liquidity_notional_cap="100000")
    values.update(updates)
    return RiskContext(**values)


def existing(**updates):
    values = dict(
        position_id="pool",
        position_version=3,
        symbol=SYMBOL,
        side="LONG",
        quantity="2",
        mark_price="100",
        risk_amount="6",
    )
    values.update(updates)
    return values


def test_explicit_entry_and_legacy_roundtrip():
    limit = trade()
    assert limit.execution_price == Decimal("100")
    assert limit.entry_conditions == []
    assert TradePlan.model_validate_json(limit.model_dump_json()).execution_price == 100
    immediate_market = trade(order_type="MARKET", limit_price=None, reference_price="99")
    assert immediate_market.execution_price == 99
    old = legacy()
    assert old.execution_price == 102
    assert TradePlan.model_validate_json(old.model_dump_json()).entry_conditions


@pytest.mark.parametrize(
    "updates",
    [
        {"limit_price": None},
        {"valid_price_range": None},
        {"limit_price": "106"},
        {"limit_price": "0"},
        {"order_type": "STOP"},
        {"order_type": "MARKET", "limit_price": None},
        {"order_type": "MARKET", "reference_price": "100"},
        {"order_type": None},
        {"position_id": "pool"},
        {"position_version": 1},
        {
            "entry_conditions": [
                {"kind": "PRICE_IN_ZONE", "action": "EXECUTE_ENTRY", "lower": "98", "upper": "101"}
            ]
        },
    ],
)
def test_immediate_shape_is_strict(updates):
    with pytest.raises(ValidationError):
        trade(**updates)


def test_new_geometry_uses_exact_price_not_descriptive_zone():
    assert trade(entry_zone={"lower": "80", "upper": "120"}).execution_price == 100
    with pytest.raises(ValidationError):
        trade(limit_price="110", valid_price_range={"lower": "100", "upper": "115"})


def wait(action="WAIT", **updates):
    values = dict(
        reason="wait for price confirmation",
        wake_conditions=[
            {"kind": "PRICE_CROSSES_ABOVE", "action": "RECALL_LLM", "value": "103"},
        ],
        expires_at=NOW + timedelta(minutes=30),
    )
    values.update(updates)
    return PortfolioTraderDecision(
        action=action, rationale=["no current opportunity"], wait_plan=WaitPlan(**values)
    )


@pytest.mark.parametrize("action", ["WAIT", "NO_TRADE"])
def test_wait_without_thesis_or_hard_blocker_is_allowed(action):
    result = wait(action)
    assert result.hard_blockers == []
    assert result.wait_plan.thesis_id is None
    assert new_decision_contract_errors(result, market_state=market()) == []
    assert decision_policy_errors(result, previous_thesis={"direction": "LONG"}) == []


@pytest.mark.parametrize(
    "wake",
    [
        {"kind": "PRICE_IN_ZONE", "lower": "99", "upper": "101"},
        {"kind": "PRICE_CROSSES_ABOVE", "value": "99"},
        {"kind": "PRICE_CROSSES_BELOW", "value": "101"},
        {"kind": "PRICE_CROSSES_ABOVE", "value": "not-a-price"},
        {"kind": "REGIME_CHANGED"},
        {"kind": "EXPIRES_AT", "value": NOW + timedelta(minutes=10)},
    ],
)
def test_new_wait_rejects_missing_or_already_satisfied_price_wake(wake):
    result = wait(wake_conditions=[{"action": "RECALL_LLM", **wake}])
    # Persisted legacy wait remains readable, but cannot be newly generated.
    PortfolioTraderDecision.model_validate_json(result.model_dump_json())
    assert "NEW_WAIT_REQUIRES_ACTUAL_FUTURE_PRICE_WAKE" in new_decision_contract_errors(
        result,
        market_state=market(),
    )


def test_new_wait_requires_bounded_future_expiry():
    for expires in (NOW, NOW + timedelta(hours=3)):
        assert new_decision_contract_errors(wait(expires_at=expires), market_state=market())


def test_normalization_never_manufactures_immediate_entry_wake():
    result = decision()
    assert normalize_direct_trade_decision(result, market()) is result
    old = normalize_direct_trade_decision(decision(legacy()), market())
    assert old.trade_plan.entry_conditions[0].kind == "PRICE_IN_ZONE"


@pytest.mark.parametrize("last", ["94", "106"])
def test_new_entry_rejects_an_already_invalid_price_range(last):
    state = market().model_copy(
        update={"market": FeatureGroup(health="healthy", values={"last": Decimal(last)})}
    )
    assert "CURRENT_PRICE_OUTSIDE_ENTRY_VALIDITY_RANGE" in new_decision_contract_errors(
        decision(), market_state=state
    )


def test_resting_limit_is_valid_away_from_market_within_its_validity_range():
    state = market().model_copy(
        update={"market": FeatureGroup(health="healthy", values={"last": Decimal("104")})}
    )
    assert not new_decision_contract_errors(decision(), market_state=state)


def test_engine_cannot_fire_immediate_even_with_injected_legacy_conditions(tmp_path):
    store = TradingIntelligenceStore(SqliteStore(tmp_path / "isolated.sqlite3"))
    # Inject a stale in-memory hybrid to prove the engine guard is independent
    # of schema validation and not merely an empty-list accident.
    hybrid = trade().model_copy(
        update={
            "state": "WAITING_TRIGGER",
            "entry_conditions": [
                WakeCondition(
                    kind="PRICE_IN_ZONE", action="EXECUTE_ENTRY", lower="90", upper="110"
                ),
            ],
        }
    )
    store.list_open_plans = lambda symbol: [hybrid]
    calls = []

    async def fired(*args):
        calls.append(args)

    engine = EventDrivenTradingEngine(store, on_wake=fired)
    assert asyncio.run(engine.ingest_market_state(market())) == []
    assert calls == []


def test_exact_risk_and_add_stop_change_with_no_extra_slot():
    engine = DeterministicRiskEngine()
    allocation = engine.allocate(trade(), context(), RiskLimits())
    assert allocation.quantity == 10  # 50 / (100 - 95), not midpoint 102.
    allocation = engine.allocate(
        trade(position_id="pool", position_version=3),
        context(open_positions=1, portfolio_risk_used="0.0006", existing_position=existing()),
        RiskLimits(max_open_positions=1),
    )
    assert allocation.quantity == Decimal("9.2")  # Existing risk rises 6 -> 10; add has 46.
    with pytest.raises(ValueError, match="maximum open positions"):
        engine.allocate(trade(), context(open_positions=1), RiskLimits(max_open_positions=1))


@pytest.mark.parametrize(
    "updates",
    [
        {"position_id": "wrong"},
        {"position_version": 2},
        {"side": "SHORT"},
        {"symbol": "ETH/USDT:USDT"},
        {"risk_amount": "1000"},
    ],
)
def test_add_risk_rejects_wrong_target_and_unaccounted_context(updates):
    with pytest.raises(ValueError):
        DeterministicRiskEngine().allocate(
            trade(position_id="pool", position_version=3),
            context(
                open_positions=1, portfolio_risk_used="0.001", existing_position=existing(**updates)
            ),
            RiskLimits(),
        )


def test_add_requires_position_risk_context():
    with pytest.raises(ValueError, match="target position risk context"):
        DeterministicRiskEngine().allocate(
            trade(position_id="pool", position_version=3),
            context(),
            RiskLimits(),
        )


def test_add_widening_exhausts_portfolio_budget():
    with pytest.raises(ValueError, match="portfolio risk budget exhausted"):
        DeterministicRiskEngine().allocate(
            trade(position_id="pool", position_version=3),
            context(open_positions=1, portfolio_risk_used="0.0499", existing_position=existing()),
            RiskLimits(),
        )


def account():
    return PortfolioState(
        equity="10000",
        cash="10000",
        daily_start_equity="10000",
        gross_exposure="400",
        open_positions=[
            Position(
                symbol="BTC/USDT",
                side="long",
                quantity="2",
                average_entry="100",
                mark_price="100",
                stop_loss="95",
            ),
            Position(
                symbol="BTC/USDT",
                side="short",
                quantity="2",
                average_entry="100",
                mark_price="100",
                stop_loss="105",
            ),
        ],
    )


def pending(**updates):
    values = dict(
        entry_id="entry",
        symbol=SYMBOL,
        side="LONG",
        status="PARTIALLY_FILLED",
        mode="OKX_DEMO",
        quantity="10",
        filled_quantity="8",
        accounted_quantity="2",
        reserved_risk="50",
        reserved_margin="100",
        position_id="pool",
    )
    values.update(updates)
    return values


def test_gross_hedged_risk_and_unaccounted_fill_reservations():
    snapshot = portfolio_risk_snapshot(
        account(),
        [],
        existing_position_stress=Decimal("0.01"),
        pending_entries=[pending(), pending()],
    )
    assert snapshot["risk_amount"] == 60  # Both directional stops (20) plus 8/10 * 50.
    assert snapshot["pending_margin"] == 80
    assert snapshot["open_positions"] == 2
    assert len(snapshot["positions"]) == 2


@pytest.mark.parametrize("status", ["FILLED", "CANCELLED", "REJECTED", "SHADOW_RECORDED"])
def test_terminal_reservations_do_not_spend_risk(status):
    result = portfolio_risk_snapshot(
        account(),
        [],
        existing_position_stress=Decimal("0.01"),
        pending_entries=[pending(status=status)],
    )
    assert result["risk_amount"] == 20
    assert result["pending_margin"] == 0


def test_pending_new_direction_counts_one_slot_and_mode_filter():
    portfolio = account().model_copy(update={"open_positions": []})
    result = portfolio_risk_snapshot(
        portfolio,
        [],
        existing_position_stress=Decimal("0.01"),
        pending_entries=[pending(), pending(entry_id="second")],
    )
    assert result["open_positions"] == 1
    assert result["risk_amount"] == 80
    result = portfolio_risk_snapshot(
        portfolio,
        [],
        existing_position_stress=Decimal("0.01"),
        pending_entries=[pending()],
        mode="SHADOW",
    )
    assert result["open_positions"] == 0


def test_snapshot_exposes_matching_risk_context():
    position = PositionRuntimeV2(
        position_id="pool",
        version=3,
        plan_id="plan",
        symbol=SYMBOL,
        side="LONG",
        state="OPEN",
        original_quantity="8",
        remaining_quantity="2",
        average_entry="100",
        initial_stop="90",
        current_stop="97",
    )
    snapshot = portfolio_risk_snapshot(
        account(), [position], existing_position_stress=Decimal("0.01")
    )
    row = next(row for row in snapshot["positions"] if row["side"] == "LONG")
    assert {key: row[key] for key in existing()} == {
        **existing(),
        "quantity": Decimal("2"),
        "mark_price": Decimal("100"),
        "risk_amount": Decimal("6"),
        "symbol": "BTC/USDT",
    }
    assert snapshot["risk_amount"] == 16


class Gateway:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=next(self.outputs),
            model="fake",
            latency_ms=0,
            usage=SimpleNamespace(total_tokens=1),
        ), None


def test_new_legacy_trade_is_repaired_exactly_once():
    gateway = Gateway([decision().model_dump(mode="json")])
    result, repaired = asyncio.run(
        DualLLMDecisionService(gateway)._validate_with_one_repair(
            PortfolioTraderDecision,
            decision(legacy()).model_dump(mode="json"),
            role="PortfolioTrader",
            source_prompt="{}",
            market_state=market(),
            portfolio={},
        )
    )
    assert repaired and result.trade_plan.order_type == "LIMIT"
    assert len(gateway.calls) == 1
    assert "NEW_TRADE_REQUIRES_IMMEDIATE_ORDER_TYPE" in gateway.calls[0]["prompt"]


def test_failed_contract_repair_is_bounded():
    payload = decision(legacy()).model_dump(mode="json")
    gateway = Gateway([payload])
    with pytest.raises(ValueError, match="CONTRACT_ERROR"):
        asyncio.run(
            DualLLMDecisionService(gateway)._validate_with_one_repair(
                PortfolioTraderDecision,
                payload,
                role="PortfolioTrader",
                source_prompt="{}",
            )
        )
    assert len(gateway.calls) == 1


def test_already_invalid_entry_is_repaired_once_before_execution():
    state = market().model_copy(
        update={"market": FeatureGroup(health="healthy", values={"last": Decimal("106")})}
    )
    corrected = decision(trade(valid_price_range={"lower": "95", "upper": "108"}))
    gateway = Gateway([corrected.model_dump(mode="json")])
    result, repaired = asyncio.run(
        DualLLMDecisionService(gateway)._validate_with_one_repair(
            PortfolioTraderDecision,
            decision().model_dump(mode="json"),
            role="PortfolioTrader",
            source_prompt="{}",
            market_state=state,
        )
    )
    assert repaired and result.trade_plan.valid_price_range.upper == 108
    assert len(gateway.calls) == 1
    assert "CURRENT_PRICE_OUTSIDE_ENTRY_VALIDITY_RANGE" in gateway.calls[0]["prompt"]


@pytest.mark.parametrize("action", ["WAIT", "NO_TRADE", "LONG"])
def test_valid_new_contract_hot_path_is_two_calls(action):
    state = market()
    analysis = MarketAnalysis(
        market_state_hash=state.state_hash,
        data_as_of=NOW,
        regime=state.regime,
        summary="current structure",
    )
    authored = decision() if action == "LONG" else wait(action)
    gateway = Gateway([analysis.model_dump(mode="json"), authored.model_dump(mode="json")])
    result = asyncio.run(
        DualLLMDecisionService(gateway).decide(
            market_state=state,
            delta=DeltaContext(current_state_hash=state.state_hash),
            quant=None,
            portfolio={"decision_symbol": SYMBOL, "symbol_positions": []},
            previous_thesis=None,
            previous_plan=None,
            require_quant_edge=False,
        )
    )
    assert result.decision.action == action
    assert len(gateway.calls) == 2
    prompt = gateway.calls[1]["system"]
    assert "MUST return LONG or SHORT" not in prompt
    assert "immediate order submission" in prompt
    assert json.loads(gateway.calls[1]["prompt"])["market_state"]["symbol"] == SYMBOL


def test_hedged_add_and_management_target_contract():
    positions = {"symbol_positions": [existing(), existing(position_id="short", side="SHORT")]}
    assert (
        new_decision_contract_errors(
            decision(trade(position_id="pool", position_version=3)),
            portfolio=positions,
            market_state=market(),
        )
        == []
    )
    assert "EXISTING_DIRECTION_ADD_REQUIRES_POSITION_TARGET" in new_decision_contract_errors(
        decision(),
        portfolio=positions,
    )
    manage = PortfolioTraderDecision(
        action="MANAGE_EXISTING",
        rationale=["tighten stop"],
        management_plan=trade().management,
        position_id="short",
        position_version=3,
    )
    assert new_decision_contract_errors(manage, portfolio=positions, market_state=market()) == []
    manage.position_version = 2
    assert "POSITION_TARGET_NOT_CURRENT" in new_decision_contract_errors(
        manage, portfolio=positions
    )


def test_legacy_pooled_position_defaults_and_roundtrip():
    position = PositionRuntimeV2(
        plan_id="legacy",
        symbol=SYMBOL,
        side="LONG",
        original_quantity="2",
        remaining_quantity="1",
        average_entry="100",
        initial_stop="95",
        current_stop="98",
    )
    assert position.plan_ids == []
    assert position.entry_filled_quantities == {}
    position.plan_ids = ["legacy", "add"]
    position.entry_filled_quantities = {"entry": Decimal("2")}
    assert PositionRuntimeV2.model_validate_json(position.model_dump_json()) == position
