from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.domain import (
    MarketSnapshot,
    Order,
    OrderStatus,
    TradingMode,
)
from cosmos_quant.position_manager_v2 import (
    DeterministicPositionManager,
    PositionActionType,
)
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    ActiveThesis,
    DecisionEpisode,
    DeltaContext,
    EvidenceRef,
    FeatureGroup,
    FeatureHealth,
    MarketRegimeV2,
    MarketStateV2,
    PositionManagementPlan,
    PositionRuntimeV2,
    PositionStateV2,
    PriceZone,
    RiskAllocation,
    TakeProfitRule,
    TradePlan,
    TradingModeV2,
    WakeAction,
    WakeCondition,
    WakeKind,
)
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from cosmos_quant.v2_execution import (
    EntryRiskBlocked,
    V2ExecutionEngine,
    compile_execution_adapter_plan,
)

NOW = datetime(2026, 8, 29, tzinfo=UTC)


@pytest.fixture(autouse=True)
def execution_clock(monkeypatch):
    class TestClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr("cosmos_quant.trading_intelligence.datetime", TestClock)
    monkeypatch.setattr("cosmos_quant.v2_execution.datetime", TestClock)


def trade_plan() -> TradePlan:
    return TradePlan(
        plan_id="plan_v2_execution",
        thesis_id="thesis_v2_execution",
        symbol="BTC/USDT:USDT",
        side="LONG",
        entry_zone=PriceZone(lower="100", upper="101"),
        entry_conditions=[
            WakeCondition(
                kind=WakeKind.PRICE_IN_ZONE,
                action=WakeAction.EXECUTE_ENTRY,
                lower="100",
                upper="101",
            )
        ],
        management=PositionManagementPlan(
            hard_stop="98",
            take_profits=[
                TakeProfitRule(price="104", close_fraction="0.5", rationale="first"),
                TakeProfitRule(price="108", close_fraction="0.5", rationale="second"),
            ],
            break_even_after_target=1,
            atr_trailing_multiple="2",
            maximum_holding_minutes=120,
        ),
        minimum_reward_risk="2",
        rationale=["validated event-driven setup"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def allocation() -> RiskAllocation:
    return RiskAllocation(
        allocation_id="allocation_v2_execution",
        plan_id="plan_v2_execution",
        equity="10000",
        risk_amount="50",
        quantity="20",
        notional="2010",
        leverage="1",
        constraints=["deterministic-risk-v2"],
        configuration_fingerprint="risk-config",
    )


def market() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=NOW,
        bid="100.4",
        ask="100.6",
        last="100.5",
        source="okx",
    )


def market_state() -> MarketStateV2:
    group = FeatureGroup(health=FeatureHealth.HEALTHY, values={"value": "ok"})
    return MarketStateV2(
        symbol="BTC/USDT:USDT",
        data_as_of=NOW,
        regime=MarketRegimeV2.TREND_UP,
        market=FeatureGroup(health=FeatureHealth.HEALTHY, values={"last": "100.5"}),
        structure=group,
        trend=group,
        momentum=group,
        volatility=group,
        volume=group,
        microstructure=group,
        derivatives=group,
        cross_market=group,
        events=FeatureGroup(
            health=FeatureHealth.HEALTHY,
            values={"new_risk_blocked": False},
        ),
    )


def test_execution_adapter_uses_deterministic_allocation() -> None:
    compiled = compile_execution_adapter_plan(
        trade_plan(),
        allocation(),
        market(),
        margin_mode="cross",
        execution_leverage=Decimal("3"),
        trading_mode=TradingModeV2.OKX_DEMO,
    )
    assert compiled.sizing.suggested_quantity == Decimal("20")
    assert compiled.leverage == Decimal("1")
    assert compiled.research_candidate_id is None
    assert compiled.decision_origin == "trading_intelligence_v2"


def test_official_event_block_rejects_before_execution_side_effects(tmp_path) -> None:
    class ExchangeMustNotBeCalled:
        async def protection_available(self, _symbol):
            raise AssertionError("event risk must reject before exchange preflight")

    store = TradingIntelligenceStore(SqliteStore(tmp_path / "cosmos.db"))
    blocked_state = market_state().model_copy(
        update={
            "events": FeatureGroup(
                health=FeatureHealth.HEALTHY,
                values={"new_risk_blocked": True},
            )
        }
    )
    episode = DecisionEpisode(
        symbol="BTC/USDT:USDT",
        market_state_hash=blocked_state.state_hash,
        delta=DeltaContext(current_state_hash=blocked_state.state_hash),
        plan_id=trade_plan().plan_id,
    )
    engine = V2ExecutionEngine(
        ExchangeMustNotBeCalled(),
        store,
        margin_mode="cross",
        execution_leverage=Decimal("3"),
        demo_credentials_available=True,
    )

    with pytest.raises(EntryRiskBlocked, match="official event risk window"):
        asyncio.run(
            engine.execute(
                plan=trade_plan(),
                allocation=allocation(),
                market_state=blocked_state,
                market=market(),
                episode=episode,
                requested_mode=TradingModeV2.OKX_DEMO,
                configuration_fingerprint="config",
                live_grant=None,
                quant=None,
            )
        )

    assert store.get_artifact(allocation().allocation_id) is None


def test_live_request_without_grant_never_calls_exchange(tmp_path) -> None:
    class ExchangeMustNotBeCalled:
        async def protection_available(self, _):
            raise AssertionError("disabled Live execution must not call the exchange")

    store = TradingIntelligenceStore(SqliteStore(tmp_path / "cosmos.db"))
    thesis = ActiveThesis(
        thesis_id="thesis_v2_execution",
        symbol="BTC/USDT:USDT",
        direction="LONG",
        statement="test thesis",
        supporting_evidence=[EvidenceRef(kind="state", reference="state")],
        invalidation_rules=["structure invalidates"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    store.save_thesis(thesis)
    store.save_plan(trade_plan())
    episode = DecisionEpisode(
        symbol="BTC/USDT:USDT",
        market_state_hash=market_state().state_hash,
        delta=DeltaContext(current_state_hash=market_state().state_hash),
    )
    engine = V2ExecutionEngine(
        ExchangeMustNotBeCalled(),
        store,
        margin_mode="cross",
        execution_leverage=Decimal("3"),
        demo_credentials_available=False,
    )
    with pytest.raises(EntryRiskBlocked, match="Live execution is disabled"):
        asyncio.run(engine.execute(
            plan=trade_plan(),
            allocation=allocation(),
            market_state=market_state(),
            market=market(),
            episode=episode,
            requested_mode=TradingModeV2.LIVE_AUTHORIZED,
            configuration_fingerprint="config",
            live_grant=None,
            quant=None,
        ))
    assert store.get_artifact(allocation().allocation_id) is None


def test_position_manager_applies_targets_and_break_even_without_llm() -> None:
    manager = DeterministicPositionManager()
    position = PositionRuntimeV2(
        plan_id="plan_v2_execution",
        symbol="BTC/USDT:USDT",
        side="LONG",
        state=PositionStateV2.OPEN,
        original_quantity="20",
        remaining_quantity="20",
        average_entry="100.5",
        initial_stop="98",
        current_stop="98",
        opened_at=NOW,
    )
    first = manager.evaluate(
        plan=trade_plan(),
        position=position,
        mark_price=Decimal("104"),
        atr_value=Decimal("2"),
        now=NOW + timedelta(minutes=30),
    )
    assert first.action == PositionActionType.REDUCE
    reduced = manager.apply(position, first)
    assert reduced.remaining_quantity == Decimal("10")
    break_even = manager.evaluate(
        plan=trade_plan(),
        position=reduced,
        mark_price=Decimal("103"),
        atr_value=Decimal("2"),
        now=NOW + timedelta(minutes=31),
    )
    assert break_even.action == PositionActionType.MOVE_STOP
    assert break_even.new_stop == Decimal("100.5")


def test_demo_execution_treats_quant_as_advisory_and_submits_protected_order(tmp_path) -> None:
    class FakeProtectedDemo:
        def __init__(self) -> None:
            self.submissions = 0

        async def protection_available(self, _symbol):
            return True

        async def place_protected_order(self, adapter_plan, intent):
            self.submissions += 1
            return [
                Order(
                    id="demo-entry-order",
                    client_order_id=intent.idempotency_key,
                    plan_id=intent.plan_id,
                    symbol=adapter_plan.symbol,
                    side=intent.side,
                    order_type=intent.order_type,
                    status=OrderStatus.OPEN,
                    quantity=intent.quantity,
                    limit_price=intent.limit_price,
                    mode=TradingMode.SANDBOX,
                )
            ]

        async def verify_protection(self, _order):
            return True

        async def cancel_order(self, *_):
            raise AssertionError("verified protection must not be cancelled")

    store = TradingIntelligenceStore(SqliteStore(tmp_path / "cosmos.db"))
    exchange = FakeProtectedDemo()
    store.save_thesis(
        ActiveThesis(
            thesis_id="thesis_v2_execution",
            symbol="BTC/USDT:USDT",
            direction="LONG",
            statement="test thesis",
            supporting_evidence=[EvidenceRef(kind="state", reference="state")],
            invalidation_rules=["structure invalidates"],
            created_at=NOW,
            expires_at=NOW + timedelta(hours=2),
        )
    )
    store.save_plan(trade_plan())
    episode = DecisionEpisode(
        symbol="BTC/USDT:USDT",
        market_state_hash=market_state().state_hash,
        delta=DeltaContext(current_state_hash=market_state().state_hash),
        plan_id=trade_plan().plan_id,
    )
    result = asyncio.run(
        V2ExecutionEngine(
            exchange,
            store,
            margin_mode="cross",
            execution_leverage=Decimal("3"),
            demo_credentials_available=True,
        ).execute(
            plan=trade_plan(),
            allocation=allocation(),
            market_state=market_state(),
            market=market(),
            episode=episode,
            requested_mode=TradingModeV2.OKX_DEMO,
            configuration_fingerprint="config",
            live_grant=None,
            quant=None,
        )
    )

    position = store.position_for_plan(trade_plan().plan_id)
    assert result.status == "SUBMITTED"
    assert result.protection_verified is True
    assert exchange.submissions == 1
    assert position is not None
    assert position.state == PositionStateV2.OPENING
    assert position.entry_order_ids == ["demo-entry-order"]
