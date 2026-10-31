from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.domain import (
    Fill,
    MarketSnapshot,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioState,
    Position,
    Side,
    TradingMode,
)
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    ActiveThesis,
    AutomationRuntimeStatus,
    DecisionEpisode,
    DeltaContext,
    EvidenceRef,
    FeatureGroup,
    FeatureHealth,
    MarketRegimeV2,
    MarketStateV2,
    PlanState,
    PositionManagementPlan,
    PositionRuntimeV2,
    PositionStateV2,
    PriceZone,
    RiskAllocation,
    TakeProfitRule,
    ThesisState,
    TradePlan,
    TradingModeV2,
    WaitPlan,
    WaitRuntimeV2,
    WakeAction,
    WakeCondition,
    WakeKind,
)
from cosmos_quant.trading_intelligence_automation import (
    TradingIntelligenceAutomation,
    is_expected_automated_entry_rejection,
)
from cosmos_quant.trading_intelligence_engine import EventDrivenTradingEngine
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from cosmos_quant.v2_position_runtime import (
    PositionReconciliationIntegrityError,
    V2PositionReconciler,
)

NOW = datetime(2026, 8, 29, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status_code", "reason", "expected"),
    [
        (
            409,
            "an attributable position already exists for this symbol; "
            "a second plan cannot share the venue's aggregate position",
            True,
        ),
        (409, "global risk halt is active: trading_intelligence_v2", True),
        (409, "unexpected post-claim execution failure", False),
        (500, "plan is not waiting for an entry trigger", False),
    ],
)
def test_expected_automated_entry_rejection_classification(
    status_code: int,
    reason: str,
    expected: bool,
) -> None:
    assert is_expected_automated_entry_rejection(status_code, reason) is expected


def state(*, at: datetime = NOW, last: str = "100.5") -> MarketStateV2:
    healthy = FeatureGroup(health=FeatureHealth.HEALTHY, values={"value": "ok"})
    return MarketStateV2(
        symbol="BTC/USDT:USDT",
        data_as_of=at,
        regime=MarketRegimeV2.TREND_UP,
        market=FeatureGroup(
            health=FeatureHealth.HEALTHY,
            values={"bid": last, "ask": last, "last": last},
        ),
        structure=healthy,
        trend=healthy,
        momentum=healthy,
        volatility=FeatureGroup(
            health=FeatureHealth.HEALTHY,
            values={"atr_14": "2"},
        ),
        volume=healthy,
        microstructure=healthy,
        derivatives=healthy,
        cross_market=healthy,
        events=FeatureGroup(
            health=FeatureHealth.HEALTHY,
            values={"new_risk_blocked": False},
        ),
    )


def plan() -> TradePlan:
    return TradePlan(
        plan_id="plan_auto_test",
        thesis_id="thesis_auto_test",
        symbol="BTC/USDT:USDT",
        side="LONG",
        entry_zone=PriceZone(lower="100", upper="101"),
        entry_conditions=[
            WakeCondition(
                condition_id="wake_auto_test",
                kind=WakeKind.PRICE_IN_ZONE,
                action=WakeAction.EXECUTE_ENTRY,
                lower="100",
                upper="101",
            )
        ],
        management=PositionManagementPlan(
            hard_stop="98",
            take_profits=[
                TakeProfitRule(price="104", close_fraction="1", rationale="target")
            ],
            maximum_holding_minutes=120,
        ),
        minimum_reward_risk="1.5",
        rationale=["test"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )


def make_store(tmp_path) -> TradingIntelligenceStore:
    return TradingIntelligenceStore(SqliteStore(tmp_path / "cosmos.db"))


def activate_plan(store: TradingIntelligenceStore) -> TradePlan:
    thesis = ActiveThesis(
        thesis_id="thesis_auto_test",
        symbol="BTC/USDT:USDT",
        direction="LONG",
        statement="test",
        supporting_evidence=[EvidenceRef(kind="state", reference="state")],
        invalidation_rules=["invalidated"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=3),
    )
    store.save_thesis(thesis)
    store.transition_thesis(
        thesis.thesis_id,
        expected_version=thesis.version,
        target=ThesisState.ACTIVE,
    )
    current = plan()
    store.save_plan(current)
    for target in (
        PlanState.VALIDATED,
        PlanState.ACTIVE,
        PlanState.WAITING_TRIGGER,
        PlanState.ARMED,
        PlanState.TRIGGERED,
    ):
        current = store.transition_plan(
            current.plan_id,
            expected_version=current.version,
            target=target,
        )
    return current


def eth_state(
    *,
    at: datetime,
    last: str,
    breakout: bool = False,
) -> MarketStateV2:
    base = state(at=at, last=last)
    return base.model_copy(
        update={
            "symbol": "ETH/USDT:USDT",
            "regime": (
                MarketRegimeV2.STRONG_TREND_UP
                if breakout
                else MarketRegimeV2.TREND_UP
            ),
            "timeframe_features": {
                "5m": {
                    "close": Decimal(last),
                    "ema_20": Decimal("2483") if breakout else Decimal("2474"),
                    "ema_50": Decimal("2475") if breakout else Decimal("2473"),
                    "ema_200": Decimal("2463"),
                    "adx_14": Decimal("41") if breakout else Decimal("22"),
                    "relative_volume_20": (
                        Decimal("3.25") if breakout else Decimal("1.10")
                    ),
                    "volatility_expansion": breakout,
                },
                "15m": {
                    "close": Decimal(last),
                    "ema_20": Decimal("2474"),
                    "ema_50": Decimal("2465"),
                    "ema_200": Decimal("2461"),
                    "adx_14": Decimal("58") if breakout else Decimal("24"),
                    "relative_volume_20": (
                        Decimal("6.20") if breakout else Decimal("1.20")
                    ),
                    "volatility_expansion": breakout,
                },
            },
        }
    )


def test_automation_replay_invokes_one_decision_and_live_is_impossible(tmp_path) -> None:
    store = make_store(tmp_path)
    decisions: list[str] = []

    async def decide(symbol, trigger, trigger_event_id, mode):
        decisions.append(f"{symbol}:{trigger}:{trigger_event_id}:{mode}")
        return {}

    async def execute(*_):
        raise AssertionError("no plan exists, so execution is impossible")

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=decide,
        execute=execute,
    )
    with pytest.raises(ValueError, match="LIVE_AUTHORIZED"):
        service.configure(enabled=True, requested_mode=TradingModeV2.LIVE_AUTHORIZED)
    service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    asyncio.run(service.on_market_state(state()))
    asyncio.run(service.on_market_state(state()))

    assert len(decisions) == 1
    assert "NO_ACTIVE_THESIS" in decisions[0]
    assert store.automation_runtime().status == "RUNNING"


def test_legacy_unreachable_no_trade_wait_is_recalled_once(tmp_path) -> None:
    store = make_store(tmp_path)
    recalled: list[str] = []

    async def on_wake(aggregate_id, _condition, _state, event_id):
        recalled.append(f"{aggregate_id}:{event_id}")

    legacy_wait = WaitRuntimeV2(
        episode_id="episode_legacy_dead_wait",
        symbol="ETH/USDT:USDT",
        wait_plan=WaitPlan(
            reason="legacy no-thesis wait",
            wake_conditions=[
                WakeCondition(
                    condition_id="wake-legacy-unreachable",
                    kind=WakeKind.EXTERNAL_EVENT,
                    action=WakeAction.RECALL_LLM,
                    value="new_thesis_available",
                )
            ],
            expires_at=NOW + timedelta(minutes=10),
        ),
    )
    store.save_wait_runtime(legacy_wait)
    engine = EventDrivenTradingEngine(store, on_wake=on_wake)
    current = eth_state(at=NOW + timedelta(minutes=1), last="2478.67")

    fired = asyncio.run(engine.ingest_market_state(current))
    assert len(fired) == 1
    assert len(recalled) == 1
    event = store.get_event(fired[0])
    assert event is not None
    assert event["payload"]["reason"] == "WAIT_PLAN_LIVENESS_INVALID"
    assert event["payload"]["errors"] == [
        "UNREACHABLE_EXTERNAL_EVENT:NEW_THESIS_AVAILABLE",
        "NO_TRADE_REQUIRES_MARKET_DERIVED_WAKE",
    ]
    assert store.list_wait_runtimes(symbol="ETH/USDT:USDT")[0].status == "FIRED"

    restarted = EventDrivenTradingEngine(store, on_wake=on_wake)
    assert asyncio.run(restarted.ingest_market_state(current)) == []
    assert len(recalled) == 1


def test_eth_volume_breakout_wakes_no_trade_once_without_polling_llm(tmp_path) -> None:
    store = make_store(tmp_path)
    recalled: list[str] = []

    async def on_wake(aggregate_id, _condition, _state, event_id):
        recalled.append(f"{aggregate_id}:{event_id}")

    before = eth_state(at=NOW, last="2479.64")
    store.save_market_state(before)
    runtime = WaitRuntimeV2(
        episode_id="episode_eth_before_breakout",
        symbol="ETH/USDT:USDT",
        wait_plan=WaitPlan(
            reason="no trade until a future market transition",
            wake_conditions=[
                WakeCondition(
                    condition_id="eth-close-above-2600",
                    kind=WakeKind.CANDLE_CLOSE_ABOVE,
                    action=WakeAction.RECALL_LLM,
                    timeframe="5m",
                    value="2600",
                )
            ],
            expires_at=NOW + timedelta(minutes=15),
        ),
    )
    store.save_wait_runtime(runtime)
    engine = EventDrivenTradingEngine(store, on_wake=on_wake)

    quiet_update = eth_state(at=NOW + timedelta(minutes=1), last="2478.10")
    assert asyncio.run(engine.ingest_market_state(quiet_update)) == []
    assert recalled == []

    # This is the real breakout segment that was previously missed: the
    # LLM-authored 2600 condition remains false, while regime/ADX/volume and
    # volatility expansion make the state transition materially actionable.
    breakout = eth_state(
        at=NOW + timedelta(minutes=5),
        last="2485.44",
        breakout=True,
    )
    fired = asyncio.run(engine.ingest_market_state(breakout))
    assert len(fired) == 1
    assert len(recalled) == 1
    event = store.get_event(fired[0])
    assert event is not None
    assert event["payload"]["reason"] == "MATERIAL_MARKET_STATE_TRANSITION"
    assert "regime:TREND_UP->STRONG_TREND_UP" in event["payload"]["material_changes"]

    # Replaying the same state after restart cannot produce a second wake or
    # another LLM decision claim.
    restarted = EventDrivenTradingEngine(store, on_wake=on_wake)
    assert asyncio.run(restarted.ingest_market_state(breakout)) == []
    assert len(recalled) == 1


def test_plan_rejection_is_a_successful_fail_closed_decision_cycle(tmp_path) -> None:
    store = make_store(tmp_path)
    decision_calls = 0

    async def decide(symbol, *_):
        nonlocal decision_calls
        decision_calls += 1
        rejected = DecisionEpisode(
            symbol=symbol,
            status="PLAN_REJECTED",
            market_state_hash=state().state_hash,
            delta=DeltaContext(
                current_state_hash=state().state_hash,
                material_changes=["event:NO_ACTIVE_THESIS"],
            ),
        )
        store.save_episode(rejected)
        return {
            "analysis": None,
            "decision": None,
            "episode": rejected,
            "rejected": True,
        }

    async def execute(*_):
        raise AssertionError("a rejected plan must never be executed")

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=decide,
        execute=execute,
    )
    service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    asyncio.run(service.on_market_state(state()))
    asyncio.run(service.on_market_state(state(at=NOW + timedelta(minutes=1))))

    runtime = store.automation_runtime()
    assert runtime.status == "RUNNING"
    assert runtime.consecutive_failures == 0
    assert runtime.last_error_code is None
    assert decision_calls == 1
    waits = store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)
    assert len(waits) == 1
    assert waits[0].wait_plan.reason.startswith("[BOUNDED_RETRY]")
    assert store.list_episodes(symbol="BTC/USDT:USDT")[0].status == "PLAN_REJECTED"


def test_rejected_wake_gets_one_bounded_retry_then_cancels_thesis(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    current_thesis = ActiveThesis(
        thesis_id="thesis_retry_test",
        symbol="BTC/USDT:USDT",
        direction="NEUTRAL",
        statement="wait for confirmation",
        supporting_evidence=[EvidenceRef(kind="state", reference="state")],
        invalidation_rules=["expired"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    store.save_thesis(current_thesis)
    store.transition_thesis(
        current_thesis.thesis_id,
        expected_version=1,
        target=ThesisState.ACTIVE,
    )
    store.save_wait_runtime(
        WaitRuntimeV2(
            episode_id="episode_retry_test",
            symbol="BTC/USDT:USDT",
            wait_plan=WaitPlan(
                thesis_id=current_thesis.thesis_id,
                reason="wait for price zone",
                wake_conditions=[
                    WakeCondition(
                        condition_id="reused-label",
                        kind=WakeKind.PRICE_IN_ZONE,
                        action=WakeAction.RECALL_LLM,
                        lower="100",
                        upper="101",
                    )
                ],
                expires_at=NOW + timedelta(hours=1),
            ),
        )
    )

    async def decide(symbol, *_):
        rejected = DecisionEpisode(
            symbol=symbol,
            status="PLAN_REJECTED",
            market_state_hash=state().state_hash,
            delta=DeltaContext(current_state_hash=state().state_hash),
        )
        store.save_episode(rejected)
        return {"episode": rejected, "rejected": True}

    async def execute(*_):
        raise AssertionError("rejected decisions cannot execute")

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=decide,
        execute=execute,
    )
    service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    asyncio.run(service.on_market_state(state()))

    retries = store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True)
    assert len(retries) == 1
    assert retries[0].wait_plan.reason.startswith("[BOUNDED_RETRY]")

    retry_at = retries[0].wait_plan.expires_at + timedelta(seconds=1)
    class RetryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return retry_at if tz else retry_at.replace(tzinfo=None)

    monkeypatch.setattr("cosmos_quant.trading_intelligence.datetime", RetryClock)
    asyncio.run(service.on_market_state(state(at=retry_at)))

    assert store.list_active_theses("BTC/USDT:USDT") == []
    event_types = {item["event_type"] for item in store.list_events(limit=100)}
    assert "BOUNDED_RETRY_SCHEDULED" in event_types
    assert "THESIS_CANCELLED_FAIL_CLOSED" in event_types


def test_demo_entry_wake_reaches_execution_exactly_once(tmp_path) -> None:
    store = make_store(tmp_path)
    current_thesis = ActiveThesis(
        thesis_id="thesis_auto_test",
        symbol="BTC/USDT:USDT",
        direction="LONG",
        statement="confirmed long setup",
        supporting_evidence=[EvidenceRef(kind="state", reference="state")],
        invalidation_rules=["hard stop"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    store.save_thesis(current_thesis)
    store.transition_thesis(
        current_thesis.thesis_id,
        expected_version=1,
        target=ThesisState.ACTIVE,
    )
    current_plan = plan()
    store.save_plan(current_plan)
    for target in (PlanState.VALIDATED, PlanState.ACTIVE, PlanState.WAITING_TRIGGER):
        current_plan = store.transition_plan(
            current_plan.plan_id,
            expected_version=current_plan.version,
            target=target,
        )
    episode = DecisionEpisode(
        episode_id="episode_entry_wake_test",
        symbol="BTC/USDT:USDT",
        status="DECIDED",
        market_state_hash=state().state_hash,
        delta=DeltaContext(current_state_hash=state().state_hash),
        thesis_id=current_thesis.thesis_id,
        plan_id=current_plan.plan_id,
    )
    store.save_episode(episode)
    executions: list[tuple[str, str, str, TradingModeV2]] = []

    async def decide(*_):
        raise AssertionError("entry execution must not recall the LLM")

    async def execute(plan_id, episode_id, event_id, mode):
        executions.append((plan_id, episode_id, event_id, mode))
        return {"status": "submitted"}

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=decide,
        execute=execute,
    )
    runtime = service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    store.save_automation_runtime(
        runtime.model_copy(
            update={
                "status": AutomationRuntimeStatus.DEGRADED,
                "consecutive_failures": 1,
                "last_error_code": "execution:HTTPException",
                "last_error_hash": "expected-entry-rejection",
                "next_retry_at": NOW + timedelta(minutes=1),
                "symbol_failures": {"BTC/USDT:USDT": 1},
                "symbol_next_retry_at": {
                    "BTC/USDT:USDT": NOW + timedelta(minutes=1)
                },
                "symbol_error_codes": {
                    "BTC/USDT:USDT": "execution:HTTPException"
                },
            }
        )
    )
    asyncio.run(service.on_market_state(state()))
    asyncio.run(service.on_market_state(state(at=NOW + timedelta(minutes=1))))

    assert len(executions) == 1
    assert executions[0][0:2] == (current_plan.plan_id, episode.episode_id)
    assert executions[0][3] == TradingModeV2.OKX_DEMO
    recovered = store.automation_runtime()
    assert recovered.last_execution_at is not None
    assert recovered.status == AutomationRuntimeStatus.RUNNING
    assert recovered.consecutive_failures == 0
    assert recovered.last_error_code is None
    assert recovered.symbol_failures == {}


def test_active_thesis_has_four_hour_maximum_age_and_cancels_wait(tmp_path) -> None:
    store = make_store(tmp_path)
    current_thesis = ActiveThesis(
        thesis_id="thesis_max_age_test",
        symbol="BTC/USDT:USDT",
        direction="NEUTRAL",
        statement="long-lived stale view",
        supporting_evidence=[EvidenceRef(kind="state", reference="state")],
        invalidation_rules=["maximum active age"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=12),
    )
    store.save_thesis(current_thesis)
    store.transition_thesis(
        current_thesis.thesis_id,
        expected_version=1,
        target=ThesisState.ACTIVE,
    )
    store.save_wait_runtime(
        WaitRuntimeV2(
            episode_id="episode_max_age_test",
            symbol="BTC/USDT:USDT",
            wait_plan=WaitPlan(
                thesis_id=current_thesis.thesis_id,
                reason="wait too long",
                wake_conditions=[
                    WakeCondition(
                        kind=WakeKind.EXTERNAL_EVENT,
                        action=WakeAction.RECALL_LLM,
                        value="QUANT_PREDICTION_UPDATED",
                    )
                ],
                expires_at=NOW + timedelta(hours=6),
            ),
        )
    )
    decisions: list[str] = []

    async def decide(_symbol, trigger, *_):
        decisions.append(trigger)
        return {}

    async def execute(*_):
        raise AssertionError("stale thesis expiry cannot execute")

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=decide,
        execute=execute,
    )
    service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    asyncio.run(service.on_market_state(state(at=NOW + timedelta(hours=5))))

    assert store.get_thesis(current_thesis.thesis_id).state == ThesisState.EXPIRED
    assert store.list_wait_runtimes(symbol="BTC/USDT:USDT", active_only=True) == []
    assert "THESIS_INVALIDATED" in decisions
    expired = next(
        item for item in store.list_events(limit=50) if item["event_type"] == "THESIS_EXPIRED"
    )
    assert expired["payload"]["maximum_active_age_hours"] == 4


def test_resume_recovers_pre_fix_plan_rejection_failure_without_llm_retry(tmp_path) -> None:
    store = make_store(tmp_path)
    decisions: list[str] = []

    async def decide(*_):
        decisions.append("unexpected")
        return {}

    async def execute(*_):
        raise AssertionError("recovery cannot execute an order")

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=decide,
        execute=execute,
    )
    runtime = service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)
    rejected = DecisionEpisode(
        symbol="ETH/USDT:USDT",
        status="PLAN_REJECTED",
        market_state_hash="rejected-state",
        delta=DeltaContext(
            current_state_hash="rejected-state",
            material_changes=["event:WAKE_TRIGGERED"],
        ),
        created_at=NOW,
    )
    store.save_episode(rejected)
    store.save_automation_runtime(
        runtime.model_copy(
            update={
                "status": AutomationRuntimeStatus.DEGRADED,
                "consecutive_failures": 1,
                "last_decision_at": NOW - timedelta(minutes=1),
                "last_error_code": "decision:HTTPException",
                "last_error_hash": "legacy-error",
                "next_retry_at": NOW + timedelta(minutes=1),
                "symbol_failures": {"ETH/USDT:USDT": 1},
                "symbol_next_retry_at": {
                    "ETH/USDT:USDT": NOW + timedelta(minutes=1)
                },
                "symbol_error_codes": {
                    "ETH/USDT:USDT": "decision:HTTPException"
                },
            }
        )
    )

    asyncio.run(service.resume())

    recovered = store.automation_runtime()
    assert recovered.status == "RUNNING"
    assert recovered.consecutive_failures == 0
    assert recovered.last_error_code is None
    assert recovered.symbol_failures == {}
    assert decisions == []
    assert store.list_events(aggregate_id="ETH/USDT:USDT")[0]["event_type"] == (
        "AUTOMATION_PLAN_REJECTION_RECOVERED"
    )


class FakeDemoExchange:
    contract_leverage = Decimal("1")

    def __init__(self) -> None:
        self.open = True
        self.reductions: list[OrderIntent] = []
        self.entry = Order(
            id="entry-order",
            client_order_id="entry-client",
            plan_id="plan_auto_test",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            status=OrderStatus.FILLED,
            quantity="20",
            filled_quantity="20",
            average_fill_price="100.5",
            mode=TradingMode.SANDBOX,
            created_at=NOW,
        )

    async def portfolio(self) -> PortfolioState:
        positions = (
            [
                Position(
                    symbol="BTC/USDT",
                    side=Side.LONG,
                    quantity="20",
                    average_entry="100.5",
                    mark_price="101",
                )
            ]
            if self.open
            else []
        )
        return PortfolioState(
            equity="10000",
            cash="8000",
            daily_start_equity="10000",
            gross_exposure="2020" if self.open else "0",
            open_positions=positions,
        )

    async def open_orders(self):
        return []

    async def fetch_order(self, *_):
        return self.entry

    async def market_snapshot(self, _):
        return MarketSnapshot(
            symbol="BTC/USDT",
            timestamp=NOW,
            bid="100.9",
            ask="101.1",
            last="101",
            source="fake-demo",
        )

    async def verify_position_protection(self, _plan, _orders, _quantity):
        return True

    async def protection_exit_order_ids(self, _orders, _symbol):
        return {"native-exit"}

    async def clear_position_protection(self, *_):
        return True

    async def reduce_position_safely(self, _plan, _orders, intent, _quantity):
        self.reductions.append(intent)
        self.open = False
        return Order(
            id="decision-exit-order",
            client_order_id="decision-exit-client",
            plan_id="plan_auto_test",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            quantity="20",
            filled_quantity="20",
            average_fill_price="101",
            mode=TradingMode.SANDBOX,
            reduce_only=True,
            created_at=NOW,
        )

    async def fetch_fills(self, *_args, **_kwargs):
        return [
            Fill(
                id="fill-entry",
                order_id="entry-order",
                plan_id="plan_auto_test",
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity="20",
                price="100.5",
                fee="1",
                timestamp=NOW,
            ),
            Fill(
                id="fill-exit",
                order_id="native-exit",
                plan_id="plan_auto_test",
                symbol="BTC/USDT",
                side=OrderSide.SELL,
                quantity="20",
                price="104",
                fee="1",
                timestamp=NOW + timedelta(minutes=20),
            ),
        ]


@pytest.mark.parametrize("fill_outage", [False, True])
def test_position_reconciliation_restores_open_then_closes_with_outcome(
    tmp_path, fill_outage,
) -> None:
    store = make_store(tmp_path)
    active_plan = activate_plan(store)
    store.save_market_state(state())
    allocation = RiskAllocation(
        allocation_id="allocation_auto_test",
        plan_id=active_plan.plan_id,
        equity="10000",
        risk_amount="50",
        quantity="20",
        notional="2010",
        leverage="1",
        constraints=["test"],
        configuration_fingerprint="test",
    )
    store.save_artifact(allocation.allocation_id, "RiskAllocation", allocation)
    episode = DecisionEpisode(
        episode_id="episode_auto_test",
        symbol="BTC/USDT:USDT",
        status="ORDERS_SUBMITTED",
        market_state_hash=state().state_hash,
        delta=DeltaContext(current_state_hash=state().state_hash),
        plan_id=active_plan.plan_id,
        risk_allocation_id=allocation.allocation_id,
        order_ids=["entry-order"],
    )
    position = PositionRuntimeV2(
        position_id="position_auto_test",
        plan_id=active_plan.plan_id,
        episode_id=episode.episode_id,
        entry_order_ids=["entry-order"],
        symbol="BTC/USDT:USDT",
        side="LONG",
        state=PositionStateV2.OPENING,
        original_quantity="20",
        remaining_quantity="0",
        average_entry="100.5",
        initial_stop="98",
        current_stop="98",
        opened_at=datetime.now(UTC),
    )
    store.save_position(position)
    episode.position_id = position.position_id
    store.save_episode(episode)
    completed: list[tuple[str, str]] = []

    async def on_completed(symbol: str, event_id: str) -> None:
        completed.append((symbol, event_id))

    reconciler = V2PositionReconciler(
        store,
        halt=lambda *_: None,
        on_trade_completed=on_completed,
        margin_mode="cross",
        commissioned_max_leverage=Decimal("3"),
    )
    exchange = FakeDemoExchange()
    exchange.entry = exchange.entry.model_copy(update={"plan_id": "unknown"})
    first = asyncio.run(reconciler.reconcile(exchange))
    assert first["opened"] == 1
    opened = store.position_for_plan(active_plan.plan_id)
    assert opened.state == PositionStateV2.OPEN
    entry_artifact = store.get_artifact("entry-order")
    assert entry_artifact["payload"]["plan_id"] == active_plan.plan_id

    store.update_position(
        opened.position_id,
        expected_version=opened.version,
        updates={
            "exit_requested_at": datetime.now(UTC),
            "exit_reason": "thesis invalidated",
        },
    )
    second = asyncio.run(reconciler.reconcile(exchange))
    assert second["managed"] == 1
    assert exchange.reductions
    assert store.position_for_plan(active_plan.plan_id).state == PositionStateV2.CLOSING

    if fill_outage:
        fetch_fills = exchange.fetch_fills

        async def unavailable(*args, **kwargs):
            raise ConnectionError("temporary fills outage")

        exchange.fetch_fills = unavailable
        with pytest.raises(RuntimeError, match="failed reconciliation"):
            asyncio.run(reconciler.reconcile(exchange))
        assert store.position_for_plan(active_plan.plan_id).state == PositionStateV2.CLOSING
        assert store.get_artifact("trading-learning-current") is None
        exchange.fetch_fills = fetch_fills

    third = asyncio.run(reconciler.reconcile(exchange))
    closed = store.position_for_plan(active_plan.plan_id)
    final_episode = store.list_episodes(symbol="BTC/USDT:USDT")[0]
    assert third["closed"] == 1
    assert closed.state == PositionStateV2.CLOSED
    assert "native-exit" in closed.exit_order_ids
    assert final_episode.status == "COMPLETED"
    assert Decimal(str(final_episode.outcome["return_after_cost"])) > 0
    assert final_episode.outcome["evidence_complete"] is True
    # Completion is durable before the background learning refresh runs.
    assert store.get_artifact("trading-learning-current") is None
    from cosmos_quant.trading_learning import refresh_learning

    refresh_learning(store)
    learned = store.get_artifact("trading-learning-current")["payload"]
    assert learned["sample_count"] == 1
    assert learned["groups"][0]["regime"] == str(state().regime)
    assert completed and completed[0][0] == "BTC/USDT:USDT"


def test_position_store_validates_updates_and_recovers_legacy_overflow(tmp_path) -> None:
    store = make_store(tmp_path)
    active_plan = activate_plan(store)
    position = PositionRuntimeV2(
        position_id="position_overflow_test",
        plan_id=active_plan.plan_id,
        symbol="BTC/USDT:USDT",
        side="LONG",
        state=PositionStateV2.OPEN,
        original_quantity="2",
        remaining_quantity="1",
        average_entry="100.5",
        initial_stop="98",
        current_stop="98",
        opened_at=NOW,
    )
    store.save_position(position)

    with pytest.raises(ValueError, match="remaining quantity cannot exceed"):
        store.update_position(
            position.position_id,
            expected_version=position.version,
            updates={"remaining_quantity": Decimal("3")},
        )
    assert store.get_position(position.position_id).remaining_quantity == Decimal("1")

    invalid = position.model_copy(update={"remaining_quantity": Decimal("3")})
    with store.store._lock, store.store.connect() as connection:
        connection.execute(
            "UPDATE ti_positions_v2 SET payload = ? WHERE position_id = ?",
            (invalid.model_dump_json(exclude_none=True), position.position_id),
        )

    recovered = store.recover_legacy_position_quantity_overflows()

    assert recovered[0]["observed_remaining_quantity"] == "3"
    repaired = store.get_position(position.position_id)
    assert repaired.remaining_quantity == repaired.original_quantity == Decimal("2")
    assert repaired.version == 2
    assert store.list_events(aggregate_id=position.position_id)[0]["event_type"] == (
        "POSITION_QUANTITY_OVERFLOW_RECOVERED"
    )


def test_reconciliation_rejects_duplicate_aggregate_position_scope(tmp_path) -> None:
    store = make_store(tmp_path)
    active_plan = activate_plan(store)
    second_plan = plan().model_copy(update={"plan_id": "plan_auto_test_second"})
    store.save_plan(second_plan)
    for target in (
        PlanState.VALIDATED,
        PlanState.ACTIVE,
        PlanState.WAITING_TRIGGER,
        PlanState.ARMED,
        PlanState.TRIGGERED,
    ):
        second_plan = store.transition_plan(
            second_plan.plan_id,
            expected_version=second_plan.version,
            target=target,
        )
    for suffix, scoped_plan in (("a", active_plan), ("b", second_plan)):
        store.save_position(
            PositionRuntimeV2(
                position_id=f"position_duplicate_{suffix}",
                plan_id=scoped_plan.plan_id,
                symbol="BTC/USDT:USDT",
                side="LONG",
                state=PositionStateV2.OPEN,
                original_quantity="10",
                remaining_quantity="10",
                average_entry="100.5",
                initial_stop="98",
                current_stop="98",
                opened_at=NOW,
            )
        )
    reconciler = V2PositionReconciler(
        store,
        halt=lambda *_: None,
        on_trade_completed=lambda *_: None,
        margin_mode="cross",
        commissioned_max_leverage=Decimal("3"),
    )

    with pytest.raises(
        PositionReconciliationIntegrityError,
        match="multiple v2 positions share one aggregate Demo venue scope",
    ):
        asyncio.run(reconciler.reconcile(FakeDemoExchange()))


def test_resume_cancels_failed_claim_without_orders_or_position(tmp_path) -> None:
    store = make_store(tmp_path)
    failed_plan = activate_plan(store)
    failed_event_id, _ = store.append_event(
        dedupe_key="entry-execution-failed:test",
        event_type="ENTRY_EXECUTION_FAILED",
        aggregate_type="plan",
        aggregate_id=failed_plan.plan_id,
        aggregate_version=failed_plan.version,
        payload={"wake_event_id": "wake-test", "risk_halted": True},
    )

    async def must_not_run(*_):
        raise AssertionError("recovery without market state must not invoke work")

    service = TradingIntelligenceAutomation(
        store,
        EventDrivenTradingEngine(store),
        decide=must_not_run,
        execute=must_not_run,
    )
    service.configure(enabled=True, requested_mode=TradingModeV2.OKX_DEMO)

    asyncio.run(service.resume())

    recovered = store.get_plan(failed_plan.plan_id)
    assert recovered is not None
    assert recovered.state == PlanState.CANCELLED
    recovery_event = store.list_events(aggregate_id=failed_plan.plan_id)[0]
    assert recovery_event["event_type"] == "FAILED_ENTRY_PLAN_RECOVERED"
    assert recovery_event["payload"]["failed_event_id"] == failed_event_id
