from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from cosmos_quant.ai_configuration import AIProvider
from cosmos_quant.ai_usage import AICompletionResult, AIUsage
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence import (
    ActiveThesis,
    DecisionAction,
    DeltaContext,
    EvidenceRef,
    FeatureGroup,
    FeatureHealth,
    HumanLiveGrant,
    MarketRegimeV2,
    MarketStateV2,
    NoTradeBlocker,
    PlanState,
    PortfolioTraderDecision,
    PositionManagementPlan,
    PriceZone,
    TakeProfitRule,
    ThesisState,
    TradePlan,
    TradingModeV2,
    WaitPlan,
    WakeAction,
    WakeCondition,
    WakeKind,
    evaluate_wake_condition,
    normalize_wait_plan_liveness,
    wait_plan_liveness_errors,
)
from cosmos_quant.trading_intelligence_engine import (
    DeterministicRiskEngine,
    DualLLMDecisionService,
    EventDrivenTradingEngine,
    RiskContext,
    RiskLimits,
    decision_policy_errors,
    normalize_wait_plan_semantics,
    quant_gate,
    resolve_trading_mode,
    trade_plan_semantic_errors,
)
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from pydantic import ValidationError

NOW = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)


def healthy(**values) -> FeatureGroup:
    return FeatureGroup(health=FeatureHealth.HEALTHY, values=values, observed_at=NOW)


def state(price: str, *, state_id: str) -> MarketStateV2:
    return MarketStateV2(
        state_id=state_id,
        symbol="BTC/USDT:USDT",
        data_as_of=NOW,
        regime=MarketRegimeV2.TREND_UP,
        market=healthy(last=Decimal(price)),
        structure=healthy(structure="HH_HL"),
        trend=healthy(ema_20=Decimal("100")),
        momentum=healthy(rsi_14=Decimal("55")),
        volatility=healthy(atr_14=Decimal("2")),
        volume=healthy(relative_volume_20=Decimal("1.2")),
        microstructure=healthy(estimated_slippage_bps=Decimal("2")),
        derivatives=healthy(funding_rate=Decimal("0.0001")),
        cross_market=healthy(btc_regime="TREND_UP"),
        events=healthy(new_risk_blocked=False),
    )


def thesis() -> ActiveThesis:
    return ActiveThesis(
        thesis_id="thesis_test",
        symbol="BTC/USDT:USDT",
        direction="LONG",
        statement="Higher-timeframe trend remains constructive.",
        supporting_evidence=[EvidenceRef(kind="market_state", reference="state")],
        invalidation_rules=["1h structure changes to LH_LL"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=4),
    )


def plan() -> TradePlan:
    return TradePlan(
        plan_id="plan_test",
        thesis_id="thesis_test",
        symbol="BTC/USDT:USDT",
        side="LONG",
        entry_zone=PriceZone(lower=Decimal("100"), upper=Decimal("101")),
        entry_conditions=[
            WakeCondition(
                condition_id="wake_test",
                kind=WakeKind.PRICE_CROSSES_ABOVE,
                action=WakeAction.EXECUTE_ENTRY,
                value=Decimal("100"),
            )
        ],
        management=PositionManagementPlan(
            hard_stop=Decimal("98"),
            take_profits=[
                TakeProfitRule(
                    price=Decimal("106"),
                    close_fraction=Decimal("1"),
                    rationale="3R target",
                )
            ],
        ),
        minimum_reward_risk=Decimal("2"),
        rationale=["trend and structure agree"],
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def test_unavailable_features_cannot_be_zero_filled() -> None:
    with pytest.raises(ValidationError):
        FeatureGroup(
            health=FeatureHealth.UNAVAILABLE,
            values={"funding": Decimal("0")},
            reasons=["missing"],
        )


def test_trade_plan_rejects_llm_sizing_and_decision_contract_is_strict() -> None:
    payload = plan().model_dump(mode="json")
    payload["leverage"] = "5"
    with pytest.raises(ValidationError):
        TradePlan.model_validate(payload)
    with pytest.raises(ValidationError):
        PortfolioTraderDecision(action=DecisionAction.NO_TRADE, rationale=["uncertain"])
    wait = WaitPlan(
        reason="wait for quant refresh",
        wake_conditions=[
            WakeCondition(
                kind=WakeKind.EXTERNAL_EVENT,
                action=WakeAction.RECALL_LLM,
                value="QUANT_PREDICTION_UPDATED",
            )
        ],
        expires_at=NOW + timedelta(minutes=10),
    )
    decision = PortfolioTraderDecision(
        action=DecisionAction.NO_TRADE,
        rationale=["no edge"],
        wait_plan=wait,
        hard_blockers=[NoTradeBlocker.NON_DIRECTIONAL_STATE],
    )
    assert decision.action == DecisionAction.NO_TRADE
    with pytest.raises(ValidationError, match="cannot retain"):
        PortfolioTraderDecision(
            action=DecisionAction.NO_TRADE,
            rationale=["no edge"],
            thesis=thesis(),
            wait_plan=wait,
        )


def test_no_trade_wait_requires_reachable_market_liveness() -> None:
    external_only = WaitPlan(
        reason="wait for an event that no provider emits",
        wake_conditions=[
            WakeCondition(
                kind=WakeKind.EXTERNAL_EVENT,
                action=WakeAction.RECALL_LLM,
                value="new_thesis_available",
            )
        ],
        expires_at=NOW + timedelta(minutes=10),
    )
    assert wait_plan_liveness_errors(
        external_only,
        action=DecisionAction.NO_TRADE,
        data_as_of=NOW,
    ) == [
        "UNREACHABLE_EXTERNAL_EVENT:NEW_THESIS_AVAILABLE",
        "NO_TRADE_REQUIRES_MARKET_DERIVED_WAKE",
    ]

    reachable = WaitPlan(
        reason="wake on market structure or a real quant update",
        wake_conditions=[
            WakeCondition(
                kind=WakeKind.CANDLE_CLOSE_ABOVE,
                action=WakeAction.RECALL_LLM,
                timeframe="5m",
                value="2480",
            ),
            WakeCondition(
                kind=WakeKind.EXTERNAL_EVENT,
                action=WakeAction.RECALL_LLM,
                value="QUANT_PREDICTION_UPDATED",
            ),
        ],
        expires_at=NOW + timedelta(minutes=15),
    )
    assert not wait_plan_liveness_errors(
        reachable,
        action=DecisionAction.NO_TRADE,
        data_as_of=NOW,
    )

    too_silent = reachable.model_copy(
        update={"expires_at": NOW + timedelta(minutes=61)}
    )
    assert wait_plan_liveness_errors(
        too_silent,
        action=DecisionAction.NO_TRADE,
        data_as_of=NOW,
    ) == ["WAIT_PLAN_MAX_SILENCE_EXCEEDED:60m"]


def test_no_trade_liveness_normalization_is_bounded_and_non_trading() -> None:
    invalid = WaitPlan(
        reason="wait for manual review",
        wake_conditions=[
            WakeCondition(
                condition_id="manual-only",
                kind=WakeKind.EXTERNAL_EVENT,
                action=WakeAction.RECALL_LLM,
                value="manual_review",
            )
        ],
        expires_at=NOW + timedelta(hours=2),
    )
    normalized, changes = normalize_wait_plan_liveness(
        invalid,
        action=DecisionAction.NO_TRADE,
        data_as_of=NOW,
    )

    assert normalized.expires_at == NOW + timedelta(minutes=60)
    assert normalized.reason.startswith("[LIVENESS_NORMALIZED]")
    assert [item.kind for item in normalized.wake_conditions] == [
        WakeKind.REGIME_CHANGED
    ]
    assert changes == [
        "REMOVED_UNREACHABLE_EXTERNAL_EVENT:MANUAL_REVIEW",
        "ADDED_NO_TRADE_REGIME_WAKE",
        "CLAMPED_WAIT_EXPIRY:60m",
    ]
    assert not wait_plan_liveness_errors(
        normalized,
        action=DecisionAction.NO_TRADE,
        data_as_of=NOW,
    )


def test_crossing_rule_requires_previous_value() -> None:
    condition = plan().entry_conditions[0]
    assert not evaluate_wake_condition(condition, {"price": "101"}, now=NOW)
    assert evaluate_wake_condition(
        condition,
        {"price": "101"},
        {"price": "99"},
        now=NOW,
    )


def test_price_zone_fires_when_market_state_jumps_across_it() -> None:
    condition = WakeCondition(
        kind=WakeKind.PRICE_IN_ZONE,
        action=WakeAction.EXECUTE_ENTRY,
        lower="100",
        upper="101",
        expires_at=NOW + timedelta(hours=1),
    )
    assert evaluate_wake_condition(
        condition,
        {"price": "99"},
        {"price": "102"},
        now=NOW,
    )


def test_entry_condition_inherits_trade_plan_expiry() -> None:
    trade_plan = plan()
    assert all(
        condition.expires_at == trade_plan.expires_at
        for condition in trade_plan.entry_conditions
    )


def test_wake_aliases_and_change_conditions_resolve_market_state_payload() -> None:
    current = {
        "price": "101",
        "regime": "TREND_UP",
        "candles": {
            "1h": {"close": "101", "adx_14": "31", "macd_histogram": "1"},
            "5m": {"relative_volume_20": "1.4"},
        },
        "microstructure": {"values": {"trade_flow_imbalance": "0.3"}},
    }
    previous = {
        "price": "99",
        "regime": "RANGE",
        "candles": {
            "1h": {"close": "99", "adx_14": "20", "macd_histogram": "-1"},
            "5m": {"relative_volume_20": "0.8"},
        },
        "microstructure": {"values": {"trade_flow_imbalance": "-0.2"}},
    }
    assert evaluate_wake_condition(
        WakeCondition(
            kind=WakeKind.REGIME_CHANGED,
            action=WakeAction.RECALL_LLM,
        ),
        current,
        previous,
        now=NOW,
    )
    assert evaluate_wake_condition(
        WakeCondition(
            kind=WakeKind.METRIC_CROSSES_ABOVE,
            action=WakeAction.RECALL_LLM,
            path="macd_histogram",
            timeframe="1h",
            value="0",
        ),
        current,
        previous,
        now=NOW,
    )
    assert evaluate_wake_condition(
        WakeCondition(
            kind=WakeKind.CANDLE_CLOSE_ABOVE,
            action=WakeAction.RECALL_LLM,
            path="model_supplied_path_is_ignored",
            timeframe="1h",
            value="100",
        ),
        current,
        previous,
        now=NOW,
    )


def test_wake_condition_is_durable_and_fires_only_once(tmp_path) -> None:
    database = tmp_path / "cosmos.db"
    base = SqliteStore(database)
    store = TradingIntelligenceStore(base)
    store.save_thesis(thesis())
    active_thesis = store.transition_thesis(
        "thesis_test", expected_version=1, target=ThesisState.ACTIVE
    )
    assert active_thesis.version == 2
    store.save_plan(plan())
    validated = store.transition_plan("plan_test", expected_version=1, target=PlanState.VALIDATED)
    active = store.transition_plan(
        "plan_test", expected_version=validated.version, target=PlanState.ACTIVE
    )
    waiting = store.transition_plan(
        "plan_test", expected_version=active.version, target=PlanState.WAITING_TRIGGER
    )
    assert waiting.state == PlanState.WAITING_TRIGGER
    engine = EventDrivenTradingEngine(store)
    before = state("99", state_id="before")
    after = state("101", state_id="after").model_copy(
        update={"data_as_of": NOW + timedelta(minutes=1)}
    )
    assert asyncio.run(engine.ingest_market_state(before)) == []
    fired = asyncio.run(engine.ingest_market_state(after))
    assert len(fired) == 1
    restarted = EventDrivenTradingEngine(TradingIntelligenceStore(SqliteStore(database)))
    repeated = state("102", state_id="repeated").model_copy(
        update={"data_as_of": NOW + timedelta(minutes=2)}
    )
    assert asyncio.run(restarted.ingest_market_state(repeated)) == []
    assert len(store.list_events(aggregate_id="plan_test")) == 1


def test_same_llm_condition_label_can_fire_once_in_each_owner(tmp_path) -> None:
    store = TradingIntelligenceStore(SqliteStore(tmp_path / "cosmos.db"))
    first_id, first_created = store.fire_wake_once(
        condition_id="wake-breakout",
        event_key="state-a",
        aggregate_id="wait-runtime-a",
        aggregate_version=1,
        payload={},
    )
    repeated_id, repeated_created = store.fire_wake_once(
        condition_id="wake-breakout",
        event_key="state-b",
        aggregate_id="wait-runtime-a",
        aggregate_version=1,
        payload={},
    )
    second_id, second_created = store.fire_wake_once(
        condition_id="wake-breakout",
        event_key="state-c",
        aggregate_id="wait-runtime-b",
        aggregate_version=1,
        payload={},
    )

    assert first_created is True
    assert repeated_created is False
    assert repeated_id == first_id
    assert second_created is True
    assert second_id != first_id


def test_ordinary_market_update_makes_zero_llm_callbacks(tmp_path) -> None:
    calls: list[str] = []

    async def on_wake(plan_id, condition, market_state, event_id) -> None:
        del condition, market_state, event_id
        calls.append(plan_id)

    store = TradingIntelligenceStore(SqliteStore(tmp_path / "cosmos.db"))
    engine = EventDrivenTradingEngine(store, on_wake=on_wake)
    assert asyncio.run(engine.ingest_market_state(state("100", state_id="ordinary"))) == []
    assert calls == []


def test_dual_llm_hot_path_is_exactly_analyst_then_trader() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.operations: list[str] = []

        async def generate_json(self, **kwargs):
            operation = kwargs["operation"]
            self.operations.append(operation)
            if operation == "trading_intelligence_trader":
                assert '"version":"learning-test-v1"' in kwargs["prompt"]
                assert "never create a new blocker or veto" in kwargs["system"]
            if operation == "trading_intelligence_analyst":
                payload = {
                    "market_state_hash": state("100", state_id="llm").state_hash,
                    "data_as_of": NOW.isoformat(),
                    "regime": "TREND_UP",
                    "summary": "Trend is constructive but entry edge is not confirmed.",
                }
            else:
                payload = {
                    "action": "NO_TRADE",
                    "rationale": ["No confirmed entry edge"],
                    "hard_blockers": ["NON_DIRECTIONAL_STATE"],
                    "wait_plan": {
                        "reason": "Wait for manual review",
                        "wake_conditions": [
                            {
                                "kind": "EXTERNAL_EVENT",
                                "action": "RECALL_LLM",
                                "value": "manual_review",
                            },
                            {
                                "condition_id": "already-true-zone",
                                "kind": "PRICE_IN_ZONE",
                                "action": "RECALL_LLM",
                                "lower": "99",
                                "upper": "101",
                            }
                        ],
                        "expires_at": (NOW + timedelta(hours=2)).isoformat(),
                    },
                }
            return (
                AICompletionResult(
                    provider=AIProvider.CUSTOM,
                    model="acceptance-model",
                    data=payload,
                    usage=AIUsage(total_tokens=10),
                    latency_ms=1,
                ),
                [AIProvider.CUSTOM],
            )

    gateway = Gateway()
    market_state = state("100", state_id="llm")
    result = asyncio.run(
        DualLLMDecisionService(gateway).decide(
            market_state=market_state,
            delta=DeltaContext(current_state_hash=market_state.state_hash),
            quant=None,
            portfolio={},
            previous_thesis=None,
            previous_plan=None,
            require_quant_edge=True,
            learning={"version": "learning-test-v1", "policy": "ADVISORY_ONLY", "groups": []},
        )
    )
    assert result.decision.action == DecisionAction.NO_TRADE
    assert result.episode.input_hashes["learning_version"] == "learning-test-v1"
    assert result.decision.wait_plan is not None
    assert result.decision.wait_plan.expires_at == NOW + timedelta(minutes=60)
    assert [item.kind for item in result.decision.wait_plan.wake_conditions] == [
        WakeKind.REGIME_CHANGED,
        WakeKind.ANY,
    ]
    assert any(
        child.kind in {WakeKind.PRICE_CROSSES_ABOVE, WakeKind.PRICE_CROSSES_BELOW}
        for child in result.decision.wait_plan.wake_conditions[1].children
    )
    assert gateway.operations == [
        "trading_intelligence_analyst",
        "trading_intelligence_trader",
    ]


def test_demo_keeps_quant_as_advisory_while_live_fails_closed() -> None:
    decision = PortfolioTraderDecision(
        action=DecisionAction.LONG,
        rationale=["complete setup"],
        thesis=thesis(),
        trade_plan=plan(),
    )

    assert quant_gate(decision, None, require_edge=False) is decision
    live_result = quant_gate(decision, None, require_edge=True, now=NOW)
    assert live_result.action == DecisionAction.WAIT
    assert live_result.trade_plan is None
    assert live_result.wait_plan is not None


def test_directional_wait_requires_a_conditional_trade_plan() -> None:
    directional_wait = PortfolioTraderDecision(
        action=DecisionAction.WAIT,
        rationale=["wait for pullback"],
        thesis=thesis(),
        wait_plan=WaitPlan(
            thesis_id=thesis().thesis_id,
            reason="wait for pullback",
            wake_conditions=[
                WakeCondition(
                    kind=WakeKind.PRICE_IN_ZONE,
                    action=WakeAction.RECALL_LLM,
                    lower="99",
                    upper="100",
                )
            ],
            expires_at=NOW + timedelta(hours=1),
        ),
    )
    assert decision_policy_errors(directional_wait, previous_thesis=None) == [
        "DIRECTIONAL_WAIT_REQUIRES_CONDITIONAL_TRADE_PLAN"
    ]


def test_trade_plan_semantics_reject_cross_symbol_scale_bad_paths_and_weak_rr() -> None:
    base_thesis = thesis()
    bad_scale = plan().model_copy(
        update={
            "entry_zone": PriceZone(lower="50000", upper="50001"),
            "entry_conditions": [
                WakeCondition(
                    kind=WakeKind.PRICE_CROSSES_ABOVE,
                    action=WakeAction.EXECUTE_ENTRY,
                    value="50000",
                )
            ],
            "management": PositionManagementPlan(
                hard_stop="49998",
                take_profits=[
                    TakeProfitRule(price="50006", close_fraction="1", rationale="target")
                ],
            ),
        }
    )
    scale_decision = PortfolioTraderDecision(
        action=DecisionAction.LONG,
        rationale=["test"],
        thesis=base_thesis,
        trade_plan=bad_scale,
    )
    scale_errors = trade_plan_semantic_errors(
        scale_decision,
        state("100", state_id="scale"),
        minimum_reward_risk=Decimal("1.8"),
    )
    assert any(item.startswith("IMPLAUSIBLE_PRICE_SCALE") for item in scale_errors)

    bad_path = plan().model_copy(
        update={
            "entry_conditions": [
                WakeCondition(
                    kind=WakeKind.METRIC_GT,
                    action=WakeAction.EXECUTE_ENTRY,
                    path="rsi_15m",
                    value="55",
                )
            ]
        }
    )
    path_decision = scale_decision.model_copy(update={"trade_plan": bad_path})
    assert any(
        item.startswith("UNRESOLVABLE_WAKE_PATH")
        for item in trade_plan_semantic_errors(
            path_decision,
            state("100", state_id="path"),
            minimum_reward_risk=Decimal("1.8"),
        )
    )

    weak_reward = plan().model_copy(
        update={
            "management": PositionManagementPlan(
                hard_stop="98",
                take_profits=[
                    TakeProfitRule(price="104", close_fraction="1", rationale="target")
                ],
            )
        }
    )
    reward_decision = scale_decision.model_copy(update={"trade_plan": weak_reward})
    assert any(
        item.startswith("MINIMUM_REWARD_RISK_NOT_MET")
        for item in trade_plan_semantic_errors(
            reward_decision,
            state("100", state_id="reward"),
            minimum_reward_risk=Decimal("1.8"),
        )
    )


def test_wait_semantics_remove_hallucinated_threshold_and_path() -> None:
    wait = WaitPlan(
        reason="bad generated conditions",
        wake_conditions=[
            WakeCondition(
                condition_id="btc-scale-on-eth",
                kind=WakeKind.PRICE_CROSSES_ABOVE,
                action=WakeAction.RECALL_LLM,
                value="50000",
            ),
            WakeCondition(
                condition_id="unknown-path",
                kind=WakeKind.METRIC_GT,
                action=WakeAction.RECALL_LLM,
                path="rsi_15m",
                value="55",
            ),
        ],
        expires_at=NOW + timedelta(minutes=30),
    )
    normalized, changes = normalize_wait_plan_semantics(
        wait,
        action=DecisionAction.NO_TRADE,
        current=state("100", state_id="wait-semantics"),
    )
    assert [item.kind for item in normalized.wake_conditions] == [
        WakeKind.REGIME_CHANGED,
        WakeKind.ANY,
    ]
    assert {
        child.kind for child in normalized.wake_conditions[1].children
    } == {WakeKind.PRICE_CROSSES_ABOVE, WakeKind.PRICE_CROSSES_BELOW}
    assert any("IMPLAUSIBLE_PRICE_SCALE" in item for item in changes)
    assert any("UNRESOLVABLE_WAKE_PATH" in item for item in changes)


def test_risk_and_live_routing_fail_closed() -> None:
    engine = DeterministicRiskEngine()
    context = RiskContext(
        equity=Decimal("10000"),
        available_margin=Decimal("5000"),
        liquidity_notional_cap=Decimal("100000"),
    )
    with pytest.raises(PermissionError):
        engine.allocate(plan(), context, RiskLimits(), live=True)

    weak_reward = plan().model_copy(
        update={
            "management": PositionManagementPlan(
                hard_stop="98",
                take_profits=[
                    TakeProfitRule(price="104", close_fraction="1", rationale="target")
                ],
            )
        }
    )
    allocation = engine.allocate(weak_reward, context, RiskLimits())
    assert "llm-reward-risk=1.4000" in allocation.constraints
    fingerprint = "config-v2"
    grant = HumanLiveGrant(
        symbols=["BTC/USDT:USDT"],
        max_risk_per_trade=Decimal("0.005"),
        max_portfolio_risk=Decimal("0.02"),
        max_leverage=Decimal("3"),
        max_daily_drawdown=Decimal("0.02"),
        max_open_positions=2,
        configuration_fingerprint=fingerprint,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=24),
        risk_confirmation_one=True,
        risk_confirmation_two=True,
    )
    assert (
        resolve_trading_mode(
            requested=TradingModeV2.LIVE_AUTHORIZED,
            symbol="BTC/USDT:USDT",
            configuration_fingerprint_value=fingerprint,
            live_grant=grant,
            demo_credentials_available=True,
            now=NOW + timedelta(hours=1),
        )
        == TradingModeV2.LIVE_AUTHORIZED
    )
    assert (
        resolve_trading_mode(
            requested=TradingModeV2.LIVE_AUTHORIZED,
            symbol="BTC/USDT:USDT",
            configuration_fingerprint_value="changed",
            live_grant=grant,
            demo_credentials_available=True,
            now=NOW + timedelta(hours=1),
        )
        == TradingModeV2.OKX_DEMO
    )
