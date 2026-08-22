import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

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
    AgentRun,
    AgentRunStatus,
    TradeEpisode,
    TradeEpisodeStatus,
)
from cosmos_quant.agent.store import AgentStore
from cosmos_quant.backtest import (
    BacktestResult,
    BacktestWindowSummary,
    TemporalStabilityReport,
)
from cosmos_quant.configuration import StrategyKind, UserConfiguration
from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import (
    Candle,
    ExitPlan,
    Fill,
    MarketRegime,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PlanStatus,
    PortfolioState,
    Position,
    ProfitTarget,
    Side,
    SignalDirection,
    StrategyPlan,
    StrategySignal,
)
from cosmos_quant.research import ResearchDataset
from cosmos_quant.research_lifecycle import CandidateStatus, create_candidate
from cosmos_quant.storage import SqliteStore
from cosmos_quant.strategy import ExplainableTrendStrategy
from cosmos_quant.strategy_spec import default_strategy_spec
from fastapi import HTTPException


def test_agent_trailing_state_activates_only_after_first_target():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    base = ExplainableTrendStrategy().generate(
        demo_candles(), snapshot_from_candles(demo_candles()), Decimal("100000")
    )
    plan = base.model_copy(
        update={
            "side": Side.LONG,
            "timeframe": "1h",
            "exit": ExitPlan(
                stop_loss=Decimal("90"),
                take_profits=[
                    ProfitTarget(
                        price=Decimal("110"),
                        close_fraction=Decimal("0.6"),
                        rationale="first",
                    ),
                    ProfitTarget(
                        price=Decimal("130"),
                        close_fraction=Decimal("0.4"),
                        rationale="second",
                    ),
                ],
                trailing_stop_atr=Decimal("1"),
                trailing_activation="first_target",
                invalidation="test",
            ),
        }
    )
    before_target = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("101"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("102"),
            volume=Decimal("10"),
        )
        for index in range(15)
    ]

    inactive = api.agent_trailing_state(
        plan,
        before_target,
        start + timedelta(hours=15),
        Decimal("102"),
        start,
        Decimal("100"),
    )

    assert inactive["active"] is False
    assert inactive["targets_reached"] == 0

    after_target = [
        *before_target,
        *[
            Candle(
                timestamp=start + timedelta(hours=index),
                open=Decimal("114"),
                high=Decimal("120"),
                low=Decimal("110"),
                close=Decimal("115"),
                volume=Decimal("20"),
            )
            for index in range(15, 20)
        ],
    ]
    active = api.agent_trailing_state(
        plan,
        after_target,
        start + timedelta(hours=20),
        Decimal("105"),
        start,
        Decimal("100"),
    )

    assert active["active"] is True
    assert active["targets_reached"] == 1
    assert active["breached"] is True
    assert Decimal(active["stop_price"]) > plan.exit.stop_loss


def test_agent_break_even_stop_matches_backtest_without_atr_warmup():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    base = ExplainableTrendStrategy().generate(
        demo_candles(), snapshot_from_candles(demo_candles()), Decimal("100000")
    )
    plan = base.model_copy(
        update={
            "side": Side.LONG,
            "timeframe": "1h",
            "exit": ExitPlan(
                stop_loss=Decimal("90"),
                take_profits=[
                    ProfitTarget(
                        price=Decimal("110"),
                        close_fraction=Decimal("0.6"),
                        rationale="first",
                    ),
                    ProfitTarget(
                        price=Decimal("130"),
                        close_fraction=Decimal("0.4"),
                        rationale="second",
                    ),
                ],
                break_even_after_first_target=True,
                invalidation="test",
            ),
        }
    )
    history = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("101"),
            high=Decimal("111"),
            low=Decimal("99"),
            close=Decimal("105"),
            volume=Decimal("10"),
        )
        for index in range(3)
    ]

    state = api.agent_trailing_state(
        plan,
        history,
        start + timedelta(hours=3),
        Decimal("99"),
        start,
        Decimal("100"),
    )

    assert state["active"] is True
    assert state["break_even_active"] is True
    assert state["targets_reached"] == 1
    assert Decimal(state["stop_price"]) == Decimal("100")
    assert state["breached"] is True


def test_agent_time_exit_counts_only_causally_closed_bars():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    plan = ExplainableTrendStrategy().generate(
        demo_candles(), snapshot_from_candles(demo_candles()), Decimal("100000")
    )
    plan.exit.maximum_holding_bars = 2
    history = [
        Candle(
            timestamp=start + timedelta(hours=index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(3)
    ]

    before_second_close = api.agent_time_exit_state(
        plan,
        history,
        start + timedelta(hours=2, minutes=30),
        start + timedelta(hours=1),
    )
    after_second_close = api.agent_time_exit_state(
        plan,
        history,
        start + timedelta(hours=3),
        start + timedelta(hours=1),
    )

    assert before_second_close["held_bars"] == 1
    assert before_second_close["due"] is False
    assert after_second_close["held_bars"] == 2
    assert after_second_close["due"] is True


def test_sandbox_probe_requires_multi_source_evidence_and_rescales_risk():
    now = datetime.now(UTC)
    result = BacktestResult(
        initial_equity=Decimal("100000"),
        final_equity=Decimal("100250"),
        net_profit=Decimal("250"),
        return_percent=Decimal("0.25"),
        max_drawdown_percent=Decimal("3.36"),
        win_rate_percent=Decimal("54"),
        profit_factor=Decimal("1.03"),
        sharpe=Decimal("0.2"),
        total_trades=37,
        trades=[],
        equity_curve=[Decimal("100000"), Decimal("100250")],
        sample_bars=1999,
        sample_years=Decimal("0.91"),
        stress_return_percent=Decimal("0.39"),
        recent_window=BacktestWindowSummary(
            label="latest_25_percent",
            sample_bars=499,
            data_start=now - timedelta(days=80),
            data_end=now,
            net_profit=Decimal("1050"),
            return_percent=Decimal("1.05"),
            max_drawdown_percent=Decimal("1.16"),
            win_rate_percent=Decimal("58"),
            profit_factor=Decimal("1.38"),
            total_trades=12,
        ),
        temporal_stability=TemporalStabilityReport(
            fold_count=4,
            minimum_trades_per_fold=4,
            evaluable_folds=4,
            positive_folds=3,
            positive_fold_ratio=Decimal("0.75"),
            worst_fold_return_percent=Decimal("-1.63"),
            passed=True,
            windows=[],
        ),
    )

    evidence = api.sandbox_probe_evidence(
        result,
        UserConfiguration().strategy.validation,
    )

    assert evidence["eligible"] is True
    assert evidence["live_eligible"] is False

    plan = ExplainableTrendStrategy().generate(
        demo_candles(),
        snapshot_from_candles(demo_candles()),
        Decimal("100000"),
        risk_fraction=Decimal("0.0025"),
    )
    resized = api.rescale_plan_risk(plan, Decimal("0.0005"))

    assert resized.sizing.risk_fraction == Decimal("0.0005")
    assert resized.sizing.risk_amount == plan.sizing.risk_amount * Decimal("0.2")
    assert resized.sizing.suggested_quantity < plan.sizing.suggested_quantity
    assert any(reason.startswith("测试网探测层：") for reason in resized.reasons)

    failed = api.sandbox_probe_evidence(
        result.model_copy(update={"stress_return_percent": Decimal("-0.01")}),
        UserConfiguration().strategy.validation,
    )
    assert failed["eligible"] is False
    assert "stress_positive" in failed["failed_checks"]


def test_exchange_agent_plan_requires_cross_market_campaign(monkeypatch):
    candles = demo_candles()
    plan = (
        ExplainableTrendStrategy()
        .generate(
            candles,
            snapshot_from_candles(candles),
            Decimal("100000"),
        )
        .model_copy(
            update={
                "trading_mode": "sandbox",
                "strategy_spec_fingerprint": "uncertified-spec",
            }
        )
    )
    monkeypatch.setattr(
        api,
        "store",
        SimpleNamespace(list_research_campaigns=lambda _limit: []),
    )

    with pytest.raises(HTTPException, match="exact-spec immutable Campaign"):
        api.validate_plan_campaign_provenance(plan)

    paper = plan.model_copy(update={"trading_mode": "paper"})
    api.validate_plan_campaign_provenance(paper)


def test_uncertain_closed_episode_is_reconciled_without_resuming_trading(tmp_path, monkeypatch):
    store = AgentStore(SqliteStore(tmp_path / "agent-reconciliation.db"))
    policy = AgentPolicy(
        id="reconcile-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )
    goal = AgentGoal(
        objective="Reconcile uncertain protected trading episodes safely",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id=policy.id,
    )
    plan = ExplainableTrendStrategy().generate(
        demo_candles(), snapshot_from_candles(demo_candles()), Decimal("100000")
    )
    episode = TradeEpisode(
        goal_id=goal.id,
        policy_id=policy.id,
        run_id="run-reconcile",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.UNCERTAIN,
        entry_order_ids=["entry-1"],
        exit_order_ids=["exit-1"],
        opened_at=datetime.now(UTC),
    )
    store.save_policy(policy)
    store.save_goal(goal)
    store.save_episode(episode)
    entry = Order(
        id="entry-1",
        client_order_id="entry-client",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.FILLED,
        quantity=Decimal("0.10"),
        filled_quantity=Decimal("0.10"),
    )
    evidence = [
        Fill(
            id="fill-entry",
            order_id="entry-1",
            plan_id=plan.id,
            symbol=plan.symbol,
            side=OrderSide.BUY,
            quantity=Decimal("0.10"),
            price=Decimal("50000"),
            timestamp=datetime.now(UTC),
        ),
        Fill(
            id="fill-exit",
            order_id="exit-1",
            plan_id=plan.id,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            quantity=Decimal("0.10"),
            price=Decimal("50100"),
            timestamp=datetime.now(UTC),
        ),
    ]

    class Execution:
        verified_quantity = None

        async def verify_position_protection(self, _plan, _orders, quantity):
            self.verified_quantity = quantity
            return True

    execution = Execution()

    async def initialize():
        return None

    async def market_context(**_):
        return None

    async def portfolio():
        return PortfolioState(
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            daily_start_equity=Decimal("100000"),
            gross_exposure=Decimal("0"),
        )

    async def collect(_plan, _episode):
        return [entry], evidence

    monkeypatch.setattr(api, "agent_store", store)
    monkeypatch.setattr(api, "plans", {plan.id: plan})
    monkeypatch.setattr(api.runtime, "execution", execution)
    monkeypatch.setattr(api.runtime, "initialize", initialize)
    monkeypatch.setattr(api.runtime, "market_context", market_context)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    monkeypatch.setattr(api, "collect_agent_episode_evidence", collect)

    asyncio.run(api.poll_agent_trade_events())

    reconciled = store.list_episodes()[0]
    assert reconciled.status == TradeEpisodeStatus.AWAITING_REVIEW
    assert execution.verified_quantity == Decimal("0")
    assert "manual global resume remains required" not in reconciled.summary


def test_persisted_exit_client_id_recovers_fill_without_known_order_id(
    monkeypatch,
):
    candles = demo_candles()
    plan = ExplainableTrendStrategy().generate(
        candles, snapshot_from_candles(candles), Decimal("100000")
    )
    episode = TradeEpisode(
        goal_id="goal-evidence",
        run_id="run-evidence",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.UNCERTAIN,
        entry_order_ids=["entry-known"],
        exit_client_order_ids=["cx-persisted-exit"],
        opened_at=datetime.now(UTC),
    )
    entry = Order(
        id="entry-known",
        client_order_id="entry-client",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.FILLED,
        quantity=Decimal("0.10"),
        filled_quantity=Decimal("0.10"),
    )

    class Execution:
        fill_attempts = 0
        protection_lookup = None

        async def fetch_order(self, order_id, symbol):
            assert order_id == entry.id and symbol == plan.symbol
            return entry

        async def protection_exit_order_ids(self, entry_orders, symbol):
            self.protection_lookup = ([item.id for item in entry_orders], symbol)
            return {"protection-child-order"}

        async def fetch_fills(self, symbol, since, limit):
            del since, limit
            self.fill_attempts += 1
            if self.fill_attempts == 1:
                raise TimeoutError("temporary OKX fill-history timeout")
            return [
                Fill(
                    id="entry-fill",
                    order_id=entry.id,
                    client_order_id=entry.client_order_id,
                    plan_id=None,
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=Decimal("0.10"),
                    price=Decimal("50000"),
                    timestamp=datetime.now(UTC),
                ),
                Fill(
                    id="exit-fill",
                    order_id="previously-unknown-order",
                    client_order_id="cx-persisted-exit",
                    plan_id=None,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    quantity=Decimal("0.04"),
                    price=Decimal("50100"),
                    timestamp=datetime.now(UTC),
                ),
                Fill(
                    id="protected-exit-fill",
                    order_id="protection-child-order",
                    client_order_id="O-exchange-generated",
                    plan_id=None,
                    symbol=symbol,
                    side=OrderSide.SELL,
                    quantity=Decimal("0.06"),
                    price=Decimal("49900"),
                    timestamp=datetime.now(UTC),
                ),
            ]

    async def initialize():
        return None

    execution = Execution()
    monkeypatch.setattr(api.runtime, "execution", execution)
    monkeypatch.setattr(api.runtime, "initialize", initialize)

    orders, evidence = asyncio.run(api.collect_agent_episode_evidence(plan, episode))

    assert orders == [entry]
    assert {fill.id for fill in evidence} == {
        "entry-fill",
        "exit-fill",
        "protected-exit-fill",
    }
    assert execution.fill_attempts == 2
    assert execution.protection_lookup == (["entry-known"], plan.symbol)


def test_open_episode_with_mismatched_protection_becomes_uncertain(tmp_path, monkeypatch):
    store = AgentStore(SqliteStore(tmp_path / "open-protection.db"))
    policy = AgentPolicy(
        id="open-policy",
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )
    goal = AgentGoal(
        objective="Monitor every open Agent position and its protection",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.SANDBOX,
        risk_policy_id=policy.id,
    )
    candles = demo_candles()
    plan = ExplainableTrendStrategy().generate(
        candles, snapshot_from_candles(candles), Decimal("100000")
    )
    episode = TradeEpisode(
        goal_id=goal.id,
        policy_id=policy.id,
        run_id="run-open",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.OPEN,
        entry_order_ids=["entry-open"],
        opened_at=datetime.now(UTC),
    )
    store.save_policy(policy)
    store.save_goal(goal)
    store.save_episode(episode)
    entry = Order(
        id="entry-open",
        client_order_id="entry-open-client",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.FILLED,
        quantity=Decimal("0.10"),
        filled_quantity=Decimal("0.10"),
    )
    entry_fill = Fill(
        id="entry-open-fill",
        order_id=entry.id,
        client_order_id=entry.client_order_id,
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        price=Decimal("50000"),
        timestamp=datetime.now(UTC),
    )

    class Execution:
        async def fetch_order(self, order_id, symbol):
            assert order_id == entry.id and symbol == plan.symbol
            return entry

        async def verify_position_protection(self, _plan, _orders, _quantity):
            return False

    class RiskState:
        reason = ""

        def halt(self, reason, source):
            self.reason = f"{source}:{reason}"
            return type("State", (), {"reason": reason})()

    risk = RiskState()

    async def initialize():
        return None

    async def market_context(**_):
        return None

    async def portfolio():
        return PortfolioState(
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            daily_start_equity=Decimal("100000"),
            gross_exposure=Decimal("5000"),
            open_positions=[
                Position(
                    symbol=plan.symbol,
                    side=Side.LONG,
                    quantity=Decimal("0.10"),
                    average_entry=Decimal("50000"),
                    mark_price=Decimal("50000"),
                )
            ],
        )

    async def collect(_plan, _episode):
        return [entry], [entry_fill]

    monkeypatch.setattr(api, "agent_store", store)
    monkeypatch.setattr(api, "plans", {plan.id: plan})
    monkeypatch.setattr(api, "risk_state_service", risk)
    monkeypatch.setattr(api.runtime, "execution", Execution())
    monkeypatch.setattr(api.runtime, "initialize", initialize)
    monkeypatch.setattr(api.runtime, "market_context", market_context)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    monkeypatch.setattr(api, "collect_agent_episode_evidence", collect)

    asyncio.run(api.poll_agent_trade_events())

    monitored = store.list_episodes()[0]
    assert monitored.status == TradeEpisodeStatus.UNCERTAIN
    assert "protection_reconciliation" in risk.reason


def test_interrupted_reduction_persists_intent_and_halts_before_retry(tmp_path, monkeypatch):
    store = AgentStore(SqliteStore(tmp_path / "interrupted-reduction.db"))
    policy = AgentPolicy(
        id="paper-reduction-policy",
        autonomy_mode=AgentAutonomyMode.PAPER,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade=Decimal("0.005"),
        max_daily_drawdown=Decimal("0.02"),
        max_open_positions=1,
        max_trades_per_day=2,
        allow_agent_entry=True,
    )
    goal = AgentGoal(
        objective="Reduce an owned position without blind timeout retries",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.PAPER,
        risk_policy_id=policy.id,
    )
    candles = demo_candles()
    plan = ExplainableTrendStrategy().generate(
        candles,
        snapshot_from_candles(candles),
        Decimal("100000"),
        side_override=Side.LONG,
    )
    plan.decision_origin = "agent"
    plan.agent_goal_id = goal.id
    plan.agent_run_id = "run-interrupted"
    plan.agent_policy_id = policy.id
    plan.agent_policy_fingerprint = policy.fingerprint
    episode = TradeEpisode(
        goal_id=goal.id,
        policy_id=policy.id,
        run_id="run-interrupted",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.OPEN,
        entry_order_ids=["entry-interrupted"],
        opened_at=datetime.now(UTC),
    )
    store.save_policy(policy)
    store.save_goal(goal)
    store.save_episode(episode)
    entry = Order(
        id="entry-interrupted",
        client_order_id="entry-interrupted-client",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.FILLED,
        quantity=Decimal("0.10"),
        filled_quantity=Decimal("0.10"),
    )
    entry_fill = Fill(
        id="entry-interrupted-fill",
        order_id=entry.id,
        client_order_id=entry.client_order_id,
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        price=Decimal("50000"),
        timestamp=datetime.now(UTC),
    )

    class Execution:
        async def reduce_position_safely(self, *_args):
            raise asyncio.CancelledError

    class Configuration:
        @staticmethod
        def current_configuration():
            return UserConfiguration(trading_mode="paper")

    class RiskState:
        reason = ""

        def halt(self, reason, source):
            self.reason = f"{source}:{reason}"
            return type("State", (), {"reason": reason})()

    risk = RiskState()

    async def initialize():
        return None

    async def portfolio():
        return PortfolioState(
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            daily_start_equity=Decimal("100000"),
            gross_exposure=Decimal("5000"),
            open_positions=[
                Position(
                    symbol=plan.symbol,
                    side=Side.LONG,
                    quantity=Decimal("0.10"),
                    average_entry=Decimal("50000"),
                    mark_price=Decimal("50000"),
                )
            ],
        )

    async def collect(_plan, _episode):
        return [entry], [entry_fill]

    monkeypatch.setattr(api, "agent_store", store)
    monkeypatch.setattr(api, "plans", {plan.id: plan})
    monkeypatch.setattr(api, "configuration_service", Configuration())
    monkeypatch.setattr(api, "risk_state_service", risk)
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(api.runtime, "execution", Execution())
    monkeypatch.setattr(api.runtime, "initialize", initialize)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    monkeypatch.setattr(api, "collect_agent_episode_evidence", collect)
    coordinator = api.build_agent_coordinator(goal, policy)
    handler = coordinator.registry.get("reduce_position").handler

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            handler(
                plan_id=plan.id,
                symbol=plan.symbol,
                fraction="0.5",
                _agent_run_id="run-interrupted",
                _agent_tool_call_id="tool-interrupted",
            )
        )

    interrupted = store.list_episodes()[0]
    assert interrupted.status == TradeEpisodeStatus.UNCERTAIN
    assert len(interrupted.exit_client_order_ids) == 1
    assert interrupted.exit_client_order_ids[0].startswith("cx")
    assert "protection_rebalance" in risk.reason


def test_closed_trade_review_writes_evidence_memory_used_by_next_run(tmp_path, monkeypatch):
    store = AgentStore(SqliteStore(tmp_path / "review-memory.db"))
    policy = AgentPolicy(
        id="paper-review-policy",
        autonomy_mode=AgentAutonomyMode.PAPER,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        max_risk_per_trade=Decimal("0.005"),
        max_daily_drawdown=Decimal("0.02"),
        max_open_positions=1,
        max_trades_per_day=2,
        allow_agent_entry=True,
    )
    goal = AgentGoal(
        objective="Review verified fills and use the evidence in later decisions",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.PAPER,
        risk_policy_id=policy.id,
    )
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(
        candles, snapshot, Decimal("100000"), side_override=Side.LONG
    )
    plan.decision_origin = "agent"
    plan.agent_goal_id = goal.id
    plan.agent_run_id = "run-review"
    plan.agent_policy_id = policy.id
    plan.agent_policy_fingerprint = policy.fingerprint
    policy = policy.model_copy(update={"allow_agent_entry": False})
    episode = TradeEpisode(
        goal_id=goal.id,
        policy_id=policy.id,
        run_id="run-review",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.AWAITING_REVIEW,
        entry_order_ids=["entry-review"],
        exit_order_ids=["exit-review"],
        planned_risk=plan.sizing.risk_amount,
        opened_at=datetime.now(UTC),
    )
    fills = [
        Fill(
            id="fill-review-entry",
            order_id="entry-review",
            plan_id=plan.id,
            symbol=plan.symbol,
            side=OrderSide.BUY,
            quantity=Decimal("0.10"),
            price=Decimal("50000"),
            fee=Decimal("1"),
            timestamp=datetime.now(UTC),
        ),
        Fill(
            id="fill-review-exit",
            order_id="exit-review",
            plan_id=plan.id,
            symbol=plan.symbol,
            side=OrderSide.SELL,
            quantity=Decimal("0.10"),
            price=Decimal("51000"),
            fee=Decimal("1"),
            timestamp=datetime.now(UTC),
        ),
    ]
    store.save_policy(policy)
    store.save_goal(goal)
    store.save_episode(episode)

    class Execution:
        async def open_orders(self):
            return []

    class Configuration:
        @staticmethod
        def current_configuration():
            return UserConfiguration(trading_mode="paper")

    async def market_context(**_):
        return snapshot, candles

    async def portfolio():
        return PortfolioState(
            equity=Decimal("100098"),
            cash=Decimal("100098"),
            daily_start_equity=Decimal("100000"),
            gross_exposure=Decimal("0"),
        )

    async def collect(_plan, _episode):
        return [], fills

    monkeypatch.setattr(api, "agent_store", store)
    monkeypatch.setattr(api, "plans", {plan.id: plan})
    monkeypatch.setattr(api, "configuration_service", Configuration())
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(api.runtime, "execution", Execution())
    monkeypatch.setattr(api.runtime, "market_context", market_context)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    monkeypatch.setattr(api, "collect_agent_episode_evidence", collect)

    coordinator = api.build_agent_coordinator(goal, policy)
    reviewed = asyncio.run(
        coordinator.registry.get("review_closed_trade").handler(
            plan_id=plan.id,
            _agent_run_id="run-review",
            _agent_tool_call_id="tool-review",
        )
    )

    assert reviewed["episode"]["status"] == "closed"
    assert reviewed["episode"]["realized_pnl"] == "98.00"
    assert reviewed["memory"]["evidence_refs"] == ["entry-review", "exit-review"]
    assert "outcome=win" in reviewed["memory"]["content"]
    assert f"strategy_family={plan.algorithm}" in reviewed["memory"]["content"]
    assert "average_entry=50000" in reviewed["memory"]["content"]
    assert "average_exit=51000" in reviewed["memory"]["content"]

    store.append_event(
        AgentEvent(
            event_type=AgentEventType.MANUAL_TRIGGERED,
            source="test",
            goal_id=goal.id,
            dedupe_key="review-memory-next-run",
            payload={"symbol": plan.symbol, "timeframe": plan.timeframe},
        )
    )

    class MemoryCapturingReasoner:
        memories = []

        async def decide(self, *, state, **_):
            self.memories = state.memories
            return AgentDecision(
                run_id=state.run_id,
                action=AgentAction.WAIT,
                reason="Verified episode memory was inspected before choosing to wait",
                valid_until=datetime.now(UTC) + timedelta(minutes=5),
            )

    reasoner = MemoryCapturingReasoner()
    next_coordinator = api.build_agent_coordinator(goal, policy)
    next_coordinator.reasoner = reasoner
    run = asyncio.run(
        next_coordinator.process_next(worker_id="memory-test-worker", goal=goal, policy=policy)
    )

    assert run is not None
    assert reasoner.memories[0]["id"] == reviewed["memory"]["id"]
    assert reasoner.memories[0]["evidence_refs"] == [
        "entry-review",
        "exit-review",
    ]


def test_owned_position_assessment_uses_exact_hold_exit_and_halt_boundaries(
    tmp_path,
    monkeypatch,
):
    store = AgentStore(SqliteStore(tmp_path / "position-assessment.db"))
    policy = AgentPolicy(
        id="position-assessment-policy",
        autonomy_mode=AgentAutonomyMode.PAPER,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
        allow_agent_exit=True,
    )
    goal = AgentGoal(
        objective="Assess the attributed protected position before any new entry",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.PAPER,
        risk_policy_id=policy.id,
    )
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(
        candles,
        snapshot,
        Decimal("100000"),
        side_override=Side.LONG,
    )
    plan.decision_origin = "agent"
    plan.agent_goal_id = goal.id
    plan.agent_run_id = "run-position-assessment"
    plan.agent_policy_id = policy.id
    plan.agent_policy_fingerprint = policy.fingerprint
    episode = TradeEpisode(
        goal_id=goal.id,
        policy_id=policy.id,
        run_id="run-position-assessment",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.OPEN,
        entry_order_ids=["entry-position-assessment"],
        planned_risk=plan.sizing.risk_amount,
        opened_at=datetime.now(UTC),
    )
    entry = Order(
        id="entry-position-assessment",
        client_order_id="entry-position-assessment-client",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        status=OrderStatus.FILLED,
        quantity=Decimal("0.10"),
        filled_quantity=Decimal("0.10"),
        average_fill_price=snapshot.last,
    )
    entry_fill = Fill(
        id="fill-position-assessment",
        order_id=entry.id,
        client_order_id=entry.client_order_id,
        plan_id=plan.id,
        symbol=plan.symbol,
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        price=snapshot.last,
        timestamp=datetime.now(UTC),
    )
    store.save_policy(policy)
    store.save_goal(goal)
    store.save_episode(episode)

    class Execution:
        async def open_orders(self):
            return []

        async def verify_position_protection(self, _plan, _orders, quantity):
            return quantity == Decimal("0.10")

    async def market_context(**_):
        return snapshot, candles

    async def portfolio():
        return PortfolioState(
            equity=Decimal("100000"),
            cash=Decimal("95000"),
            daily_start_equity=Decimal("100000"),
            gross_exposure=Decimal("5000"),
            open_positions=[
                Position(
                    symbol=plan.symbol,
                    side=plan.side,
                    quantity=Decimal("0.10"),
                    average_entry=snapshot.last,
                    mark_price=snapshot.last,
                )
            ],
        )

    async def collect(_plan, _episode):
        return [entry], [entry_fill]

    signal = StrategySignal(
        algorithm=plan.algorithm,
        symbol=plan.symbol,
        timeframe=plan.timeframe,
        direction=SignalDirection.FLAT,
        regime=MarketRegime.RANGE,
        confidence=Decimal("0.55"),
        signal_candle_at=candles[-1].timestamp,
        data_as_of=candles[-1].timestamp,
        reasons=["latest closed signal has no confirmed reversal"],
    )
    monkeypatch.setattr(api, "agent_store", store)
    monkeypatch.setattr(api, "plans", {plan.id: plan})
    monkeypatch.setattr(api.runtime, "execution", Execution())
    monkeypatch.setattr(api.runtime, "market_context", market_context)
    monkeypatch.setattr(api.runtime, "portfolio", portfolio)
    monkeypatch.setattr(api, "collect_agent_episode_evidence", collect)
    monkeypatch.setattr(
        api,
        "evaluate_current_plan_signal",
        lambda *_args, **_kwargs: signal,
    )

    coordinator = api.build_agent_coordinator(goal, policy)
    assessment = asyncio.run(
        coordinator.registry.get("assess_owned_position").handler(
            plan_id=plan.id,
            symbol=plan.symbol,
        )
    )

    assert assessment["recommendation"] == "hold"
    assert assessment["attributable_quantity"] == "0.10"
    assert assessment["protection_verified"] is True
    assert assessment["execution_arguments"] == {}

    opposite_signal = signal.model_copy(
        update={
            "direction": SignalDirection.SHORT,
            "confidence": Decimal("0.75"),
            "reasons": ["latest closed signal confirms a short reversal"],
        }
    )
    monkeypatch.setattr(
        api,
        "evaluate_current_plan_signal",
        lambda *_args, **_kwargs: opposite_signal,
    )
    exit_assessment = asyncio.run(
        coordinator.registry.get("assess_owned_position").handler(
            plan_id=plan.id,
            symbol=plan.symbol,
        )
    )

    assert exit_assessment["recommendation"] == "exit"
    assert exit_assessment["execution_arguments"] == {
        "plan_id": plan.id,
        "symbol": plan.symbol,
    }

    class UnprotectedExecution(Execution):
        async def verify_position_protection(self, _plan, _orders, _quantity):
            return False

    monkeypatch.setattr(api.runtime, "execution", UnprotectedExecution())
    halt_assessment = asyncio.run(
        coordinator.registry.get("assess_owned_position").handler(
            plan_id=plan.id,
            symbol=plan.symbol,
        )
    )

    assert halt_assessment["recommendation"] == "halt"
    assert halt_assessment["protection_verified"] is False
    assert halt_assessment["execution_arguments"] == {
        "reason": halt_assessment["reason"],
    }


def test_canceled_episode_archives_stale_agent_plan(tmp_path, monkeypatch):
    core_store = SqliteStore(tmp_path / "terminal-plan-repair.db")
    durable_agent_store = AgentStore(core_store)
    candles = demo_candles()
    plan = ExplainableTrendStrategy().generate(
        candles,
        snapshot_from_candles(candles),
        Decimal("100000"),
    )
    plan.status = PlanStatus.ACTIVE
    plan.decision_origin = "agent"
    episode = TradeEpisode(
        goal_id="terminal-plan-goal",
        policy_id="terminal-plan-policy",
        run_id="terminal-plan-run",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.CANCELED,
        opened_at=datetime.now(UTC),
    )
    core_store.save_strategy(plan)
    durable_agent_store.save_episode(episode)
    monkeypatch.setattr(api, "store", core_store)
    monkeypatch.setattr(api, "agent_store", durable_agent_store)
    monkeypatch.setattr(api, "plans", {plan.id: plan})

    archived = api.reconcile_terminal_agent_plan_states()

    assert archived == 1
    assert plan.status == PlanStatus.ARCHIVED
    persisted = {
        item["id"]: StrategyPlan.model_validate(item) for item in core_store.load_strategies()
    }
    assert persisted[plan.id].status == PlanStatus.ARCHIVED
    assert api.reconcile_terminal_agent_plan_states() == 0


def test_screening_archives_only_unselected_agent_drafts(
    tmp_path,
    monkeypatch,
):
    core_store = SqliteStore(tmp_path / "candidate-lifecycle.db")
    candles = demo_candles()
    base = ExplainableTrendStrategy().generate(
        candles,
        snapshot_from_candles(candles),
        Decimal("100000"),
    )
    selected = base.model_copy(
        deep=True,
        update={
            "id": "selected-agent-candidate",
            "decision_origin": "agent",
            "status": PlanStatus.DRAFT,
        },
    )
    rejected = base.model_copy(
        deep=True,
        update={
            "id": "rejected-agent-candidate",
            "decision_origin": "agent",
            "status": PlanStatus.DRAFT,
        },
    )
    operator = base.model_copy(
        deep=True,
        update={
            "id": "operator-draft",
            "decision_origin": "operator",
            "status": PlanStatus.DRAFT,
        },
    )
    monkeypatch.setattr(api, "store", core_store)

    archived = api.archive_unselected_agent_candidate_plans(
        [selected, rejected, operator],
        selected.id,
    )

    assert archived == [rejected.id]
    assert selected.status == PlanStatus.DRAFT
    assert rejected.status == PlanStatus.ARCHIVED
    assert operator.status == PlanStatus.DRAFT


def test_startup_archives_only_terminal_agent_screening_drafts(
    tmp_path,
    monkeypatch,
):
    core_store = SqliteStore(tmp_path / "stale-candidate-repair.db")
    candles = demo_candles()
    base = ExplainableTrendStrategy().generate(
        candles,
        snapshot_from_candles(candles),
        Decimal("100000"),
    )
    terminal = base.model_copy(
        deep=True,
        update={
            "id": "terminal-run-draft",
            "decision_origin": "agent",
            "agent_run_id": "run-terminal",
            "status": PlanStatus.DRAFT,
        },
    )
    running = base.model_copy(
        deep=True,
        update={
            "id": "running-run-draft",
            "decision_origin": "agent",
            "agent_run_id": "run-running",
            "status": PlanStatus.DRAFT,
        },
    )
    episode_owned = base.model_copy(
        deep=True,
        update={
            "id": "active-episode-draft",
            "decision_origin": "agent",
            "agent_run_id": "run-terminal",
            "status": PlanStatus.DRAFT,
        },
    )
    approved_terminal = base.model_copy(
        deep=True,
        update={
            "id": "terminal-run-approved",
            "decision_origin": "agent",
            "agent_run_id": "run-terminal",
            "status": PlanStatus.APPROVED,
        },
    )
    terminal_run = AgentRun(
        id="run-terminal",
        goal_id="candidate-goal",
        trigger_event_id="terminal-event",
        status=AgentRunStatus.WAITING,
    )
    running_run = AgentRun(
        id="run-running",
        goal_id="candidate-goal",
        trigger_event_id="running-event",
        status=AgentRunStatus.RUNNING,
    )
    active_episode = TradeEpisode(
        goal_id="candidate-goal",
        policy_id="candidate-policy",
        run_id="run-terminal",
        plan_id=episode_owned.id,
        symbol=episode_owned.symbol,
        side=str(episode_owned.side),
        status=TradeEpisodeStatus.OPEN,
        opened_at=datetime.now(UTC),
    )
    for plan in (terminal, running, episode_owned, approved_terminal):
        core_store.save_strategy(plan)
    monkeypatch.setattr(api, "store", core_store)
    monkeypatch.setattr(
        api,
        "agent_store",
        SimpleNamespace(
            list_episodes=lambda _limit: [active_episode],
            load_run=lambda run_id: {
                terminal_run.id: terminal_run,
                running_run.id: running_run,
            }.get(run_id),
        ),
    )
    monkeypatch.setattr(
        api,
        "plans",
        {
            terminal.id: terminal,
            running.id: running,
            episode_owned.id: episode_owned,
            approved_terminal.id: approved_terminal,
        },
    )

    archived = api.archive_stale_agent_screening_plans()

    assert archived == [terminal.id, approved_terminal.id]
    assert terminal.status == PlanStatus.ARCHIVED
    assert running.status == PlanStatus.DRAFT
    assert episode_owned.status == PlanStatus.DRAFT
    assert approved_terminal.status == PlanStatus.ARCHIVED


def test_pre_submit_rejection_cancels_without_false_exchange_uncertainty(
    tmp_path,
    monkeypatch,
):
    core_store = SqliteStore(tmp_path / "entry-exception-classification.db")
    durable_agent_store = AgentStore(core_store)
    candles = demo_candles()
    plan = ExplainableTrendStrategy().generate(
        candles,
        snapshot_from_candles(candles),
        Decimal("100000"),
    )
    plan.status = PlanStatus.ACTIVE
    plan.decision_origin = "agent"
    episode = TradeEpisode(
        goal_id="entry-classification-goal",
        policy_id="entry-classification-policy",
        run_id="entry-classification-run",
        plan_id=plan.id,
        symbol=plan.symbol,
        side=str(plan.side),
        status=TradeEpisodeStatus.RESERVED,
        opened_at=datetime.now(UTC),
    )
    core_store.save_strategy(plan)
    durable_agent_store.save_episode(episode)
    monkeypatch.setattr(api, "store", core_store)
    monkeypatch.setattr(api, "agent_store", durable_agent_store)
    monkeypatch.setattr(
        api,
        "risk_state_service",
        SimpleNamespace(get=lambda: SimpleNamespace(source="")),
    )

    status = api.settle_agent_entry_exception(
        plan,
        episode,
        HTTPException(409, "latest closed signal invalidated the entry"),
    )

    assert status == TradeEpisodeStatus.CANCELED
    assert episode.status == TradeEpisodeStatus.CANCELED
    assert plan.status == PlanStatus.ARCHIVED
    assert "before exchange submission" in episode.summary

    uncertain_plan = plan.model_copy(
        deep=True,
        update={"id": "uncertain-entry-plan", "status": PlanStatus.ACTIVE},
    )
    uncertain_episode = episode.model_copy(
        deep=True,
        update={
            "id": "uncertain-entry-episode",
            "plan_id": uncertain_plan.id,
            "status": TradeEpisodeStatus.RESERVED,
            "summary": "",
        },
    )
    core_store.save_strategy(uncertain_plan)
    durable_agent_store.save_episode(uncertain_episode)

    uncertain_status = api.settle_agent_entry_exception(
        uncertain_plan,
        uncertain_episode,
        HTTPException(502, "exchange acknowledgement unavailable"),
    )

    assert uncertain_status == TradeEpisodeStatus.UNCERTAIN
    assert uncertain_episode.status == TradeEpisodeStatus.UNCERTAIN
    assert uncertain_plan.status == PlanStatus.ACTIVE
    assert "reconciliation required" in uncertain_episode.summary


def test_forward_outcome_evidence_uses_only_matching_closed_strategy_memory():
    candles = demo_candles()
    plan = ExplainableTrendStrategy().generate(
        candles,
        snapshot_from_candles(candles),
        Decimal("100000"),
    )
    returns = [
        Decimal("0.4"),
        Decimal("-0.2"),
        Decimal("0.3"),
        Decimal("-0.1"),
        Decimal("0.6"),
    ]
    historical_plans: dict[str, StrategyPlan] = {}
    episodes: list[TradeEpisode] = []
    now = datetime.now(UTC)
    for index, risk_multiple in enumerate(returns):
        historical = plan.model_copy(
            deep=True,
            update={"id": f"historical-plan-{index}"},
        )
        historical_plans[historical.id] = historical
        episodes.append(
            TradeEpisode(
                id=f"historical-episode-{index}",
                goal_id="forward-memory-goal",
                policy_id="forward-memory-policy",
                run_id=f"forward-memory-run-{index}",
                plan_id=historical.id,
                symbol=plan.symbol,
                side=str(plan.side),
                status=TradeEpisodeStatus.CLOSED,
                planned_risk=Decimal("100"),
                realized_pnl=risk_multiple * Decimal("100"),
                return_on_risk=risk_multiple,
                opened_at=now - timedelta(days=10 - index),
                closed_at=now - timedelta(days=9 - index),
            )
        )
    mismatched = plan.model_copy(
        deep=True,
        update={"id": "mismatched-timeframe-plan", "timeframe": "4h"},
    )
    historical_plans[mismatched.id] = mismatched
    episodes.append(
        episodes[0].model_copy(
            deep=True,
            update={
                "id": "mismatched-timeframe-episode",
                "plan_id": mismatched.id,
                "return_on_risk": Decimal("-9"),
            },
        )
    )

    evidence = api.build_plan_forward_outcome_evidence(
        plan,
        episodes,
        historical_plans,
    )

    assert evidence["sample"] == 5
    assert evidence["total_r"] == Decimal("1.0")
    assert evidence["expectancy_r"] == Decimal("0.2")
    assert evidence["mature_for_ranking"] is True
    assert evidence["role"] == "ranking_only"
    assert "mismatched-timeframe-episode" not in evidence["episode_ids"]


def test_agent_trade_plan_prefers_matching_research_champion(tmp_path, monkeypatch):
    core_store = SqliteStore(tmp_path / "agent-champion.db")
    durable_agent_store = AgentStore(core_store)
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    dataset = ResearchDataset(
        id="ds_agent_champion",
        exchange="okx",
        market_type="swap",
        symbol="BTC/USDT",
        timeframe="1h",
        candle_count=len(candles),
        data_start=candles[0].timestamp,
        data_end=candles[-1].timestamp,
        data_fingerprint="agent_champion_dataset",
    )
    core_store.save_research_dataset(dataset, candles, [])
    champion = create_candidate(default_strategy_spec(StrategyKind.TREND))
    champion.status = CandidateStatus.CHAMPION
    champion.dataset_id = dataset.id
    core_store.save_strategy_candidate(champion)
    policy = AgentPolicy(
        id="research-champion-policy",
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        allowed_symbols=["BTC/USDT"],
        allowed_timeframes=["1h"],
    )
    goal = AgentGoal(
        objective="Use only validated research parameters when building a trade plan",
        symbols=["BTC/USDT"],
        timeframes=["1h"],
        autonomy_mode=AgentAutonomyMode.RESEARCH,
        risk_policy_id=policy.id,
    )
    generated = ExplainableTrendStrategy().generate(
        candles,
        snapshot,
        Decimal("100000"),
        side_override=Side.LONG,
    )
    captured = {}

    async def fake_generate(request):
        captured["request"] = request
        return generated

    monkeypatch.setattr(api, "store", core_store)
    monkeypatch.setattr(api, "agent_store", durable_agent_store)
    monkeypatch.setattr(api, "generate_strategy", fake_generate)
    monkeypatch.setattr(api, "plans", {})
    monkeypatch.setattr(api, "add_audit", lambda *args, **kwargs: None)

    coordinator = api.build_agent_coordinator(goal, policy)
    response = asyncio.run(
        coordinator.registry.get("build_trade_plan").handler(
            prompt="Follow the validated trend specification",
            strategy_kind="trend",
            symbol="BTC/USDT",
            timeframe="1h",
            _agent_run_id="run-champion",
            _agent_tool_call_id="tool-champion",
        )
    )

    assert captured["request"].strategy_spec == champion.spec
    assert champion.id in " ".join(response["reasons"])
    assert response["agent_goal_id"] == goal.id
