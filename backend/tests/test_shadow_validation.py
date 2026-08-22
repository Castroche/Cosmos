from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.agent.shadow_validation import (
    ShadowOutcomeStatus,
    ShadowPlanOutcome,
    ShadowValidationTrial,
    evaluate_shadow_plan,
    summarize_shadow_trials,
)
from cosmos_quant.agent.store import AgentStore
from cosmos_quant.domain import (
    Candle,
    EntryRule,
    ExitPlan,
    MarketSnapshot,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    StrategyPlan,
)
from cosmos_quant.storage import SqliteStore

BASE = datetime(2026, 7, 28, 10, tzinfo=UTC)


def _candle(
    hour: int,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100",
) -> Candle:
    return Candle(
        timestamp=BASE + timedelta(hours=hour),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
    )


def _plan(
    *,
    expires_at: datetime | None = None,
    take_profits: list[ProfitTarget] | None = None,
    maximum_holding_bars: int | None = None,
) -> StrategyPlan:
    return StrategyPlan(
        id="shadow-plan",
        name="shadow validation fixture",
        exchange="okx",
        symbol="BTC/USDT",
        timeframe="1h",
        side="long",
        entry=EntryRule(
            zone=PriceZone(lower=Decimal("100"), upper=Decimal("100")),
            trigger="touch",
            expires_at=expires_at,
        ),
        exit=ExitPlan(
            stop_loss=Decimal("90"),
            take_profits=take_profits
            or [
                ProfitTarget(
                    price=Decimal("110"),
                    close_fraction=Decimal("1"),
                    rationale="fixture",
                )
            ],
            maximum_holding_bars=maximum_holding_bars,
            invalidation="fixture",
        ),
        sizing=PositionSizing(
            risk_fraction=Decimal("0.01"),
            risk_amount=Decimal("10"),
            suggested_quantity=Decimal("1"),
            notional=Decimal("100"),
        ),
        minimum_reward_risk=Decimal("1"),
        thesis="fixture",
        reasons=["fixture"],
        snapshot=MarketSnapshot(
            symbol="BTC/USDT",
            timestamp=BASE,
            bid=Decimal("99.9"),
            ask=Decimal("100.1"),
            last=Decimal("100"),
        ),
    )


def test_shadow_excludes_forming_decision_bar_and_never_exits_on_entry_bar():
    plan = _plan()
    outcome = evaluate_shadow_plan(
        plan,
        [
            _candle(0, high="120", low="80"),
            _candle(1, high="120", low="80"),
        ],
        decision_at=BASE + timedelta(minutes=2),
        as_of=BASE + timedelta(hours=2),
    )

    assert outcome.status == ShadowOutcomeStatus.OPEN
    assert outcome.entered_at == BASE + timedelta(hours=2)
    assert outcome.exit_reasons == []
    assert outcome.bars_evaluated == 1


def test_shadow_uses_stop_before_target_on_ambiguous_later_bar():
    plan = _plan()
    outcome = evaluate_shadow_plan(
        plan,
        [
            _candle(1),
            _candle(2, high="120", low="80"),
        ],
        decision_at=BASE + timedelta(minutes=2),
        as_of=BASE + timedelta(hours=3),
    )

    assert outcome.status == ShadowOutcomeStatus.CLOSED
    assert outcome.exit_reasons == ["stop_loss"]
    assert outcome.exited_at == BASE + timedelta(hours=3)
    assert outcome.return_on_risk is not None
    assert outcome.return_on_risk < Decimal("-1")


def test_shadow_tracks_partial_targets_and_net_cost_adjusted_return():
    plan = _plan(
        take_profits=[
            ProfitTarget(
                price=Decimal("110"),
                close_fraction=Decimal("0.5"),
                rationale="first",
            ),
            ProfitTarget(
                price=Decimal("120"),
                close_fraction=Decimal("0.5"),
                rationale="second",
            ),
        ]
    )
    outcome = evaluate_shadow_plan(
        plan,
        [
            _candle(1),
            _candle(2, high="115", low="99", close="110"),
            _candle(3, high="121", low="109", close="120"),
        ],
        decision_at=BASE + timedelta(minutes=2),
        as_of=BASE + timedelta(hours=4),
    )

    assert outcome.status == ShadowOutcomeStatus.CLOSED
    assert outcome.exit_reasons == ["take_profit", "take_profit"]
    assert outcome.net_pnl == Decimal("14.83")
    assert outcome.fees == Decimal("0.13")
    assert outcome.slippage_cost == Decimal("0.04")
    assert outcome.return_on_risk == Decimal("1.483")


def test_shadow_marks_expired_plan_as_no_entry():
    plan = _plan(expires_at=BASE + timedelta(hours=2))
    outcome = evaluate_shadow_plan(
        plan,
        [
            _candle(1, high="105", low="101"),
            _candle(2, high="101", low="99"),
        ],
        decision_at=BASE + timedelta(minutes=2),
        as_of=BASE + timedelta(hours=3),
    )

    assert outcome.status == ShadowOutcomeStatus.NO_ENTRY
    assert outcome.entered_at is None


def test_shadow_time_stop_matches_closed_bar_holding_count():
    plan = _plan(maximum_holding_bars=2)
    outcome = evaluate_shadow_plan(
        plan,
        [
            _candle(1),
            _candle(2, close="101"),
            _candle(3, close="102"),
        ],
        decision_at=BASE + timedelta(minutes=2),
        as_of=BASE + timedelta(hours=4),
    )

    assert outcome.status == ShadowOutcomeStatus.CLOSED
    assert outcome.exit_reasons == ["time_stop"]
    assert outcome.exited_at == BASE + timedelta(hours=4)
    assert outcome.exit_price is not None


def test_shadow_can_force_close_open_position_at_validation_horizon():
    plan = _plan()
    outcome = evaluate_shadow_plan(
        plan,
        [
            _candle(1),
            _candle(2, high="106", low="96", close="105"),
        ],
        decision_at=BASE + timedelta(minutes=2),
        as_of=BASE + timedelta(hours=3),
        force_close=True,
    )

    assert outcome.status == ShadowOutcomeStatus.HORIZON_MARK
    assert outcome.exit_reasons == ["horizon_mark"]
    assert outcome.remaining_quantity == 0
    assert outcome.net_pnl > 0


def test_shadow_trial_store_round_trip_and_calibration_summary(tmp_path):
    plan = _plan()
    winner = ShadowValidationTrial(
        id="shadow-winner",
        goal_id="goal",
        run_id="run",
        decision_batch_id="batch-1",
        plan=plan,
        decision_at=BASE,
        horizon_at=BASE + timedelta(hours=48),
        screening_rank=2,
        screening_summary="rejected candidate",
        adaptation_evidence={"label": "responsive"},
        signal_evidence={"confidence": "0.72"},
        backtest_evidence={
            "profit_factor": "1.18",
            "validation_reasons": ["cost stress failed"],
        },
        outcome=ShadowPlanOutcome(
            status=ShadowOutcomeStatus.CLOSED,
            plan_id=plan.id,
            symbol=plan.symbol,
            timeframe=plan.timeframe,
            side=plan.side,
            decision_at=BASE,
            return_on_risk=Decimal("1.25"),
        ),
    )
    database = SqliteStore(tmp_path / "shadow.db")
    agent_store = AgentStore(database)
    agent_store.save_shadow_trial(winner)

    loaded = agent_store.list_shadow_trials(
        goal_id="goal",
        symbol="BTC/USDT",
        timeframe="1h",
        terminal=True,
    )
    summary = summarize_shadow_trials(loaded, minimum_settled_batches=1)

    assert len(loaded) == 1
    assert loaded[0].id == winner.id
    assert loaded[0].outcome.return_on_risk == Decimal("1.25")
    assert loaded[0].backtest_evidence["profit_factor"] == "1.18"
    assert summary.settled_outcomes == 1
    assert summary.rejected_positive == 1
    assert summary.expectancy_r == Decimal("1.250")
    assert summary.calibration_ready is True
