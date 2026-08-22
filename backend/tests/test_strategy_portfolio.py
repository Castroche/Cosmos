from datetime import UTC, datetime
from decimal import Decimal

from cosmos_quant.domain import (
    EntryRule,
    ExitPlan,
    MarketSnapshot,
    PlanStatus,
    PositionSizing,
    PriceZone,
    ProfitTarget,
    Side,
    StrategyPlan,
)
from cosmos_quant.research_campaign import CampaignCandidateAggregate
from cosmos_quant.strategy_portfolio import (
    admit_strategy_risk,
    allocate_campaign_strategies,
)


def _plan(
    *,
    symbol: str = "BTC/USDT",
    side: Side = Side.LONG,
    family: str = "trend",
    fingerprint: str = "fp_active",
) -> StrategyPlan:
    return StrategyPlan(
        id="stg_active",
        name="active",
        algorithm=family,
        strategy_spec_fingerprint=fingerprint,
        exchange="paper",
        symbol=symbol,
        timeframe="1h",
        side=side,
        status=PlanStatus.ACTIVE,
        entry=EntryRule(
            zone=PriceZone(lower=Decimal("100"), upper=Decimal("101")),
            trigger="test",
        ),
        exit=ExitPlan(
            stop_loss=Decimal("95"),
            take_profits=[
                ProfitTarget(
                    price=Decimal("110"),
                    close_fraction=Decimal("1"),
                    rationale="test",
                )
            ],
            invalidation="test",
        ),
        sizing=PositionSizing(
            risk_fraction=Decimal("0.01"),
            risk_amount=Decimal("100"),
            suggested_quantity=Decimal("1"),
            notional=Decimal("100"),
        ),
        minimum_reward_risk=Decimal("1"),
        thesis="test",
        reasons=["test"],
        snapshot=MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.now(UTC),
            bid=Decimal("100"),
            ask=Decimal("101"),
            last=Decimal("100.5"),
        ),
    )


def test_portfolio_admission_rejects_same_symbol_opposite_direction():
    admission = admit_strategy_risk(
        family="breakout",
        symbol="BTC/USDT",
        side=Side.SHORT,
        strategy_fingerprint="fp_new",
        requested_risk_fraction=Decimal("0.005"),
        active_plans=[_plan()],
        maximum_portfolio_risk=Decimal("0.05"),
    )

    assert admission.allowed is False
    assert admission.admitted_risk_fraction == 0
    assert admission.hard_conflicts


def test_campaign_allocator_keeps_family_and_archetype_cash_reserve_caps():
    candidates = [
        CampaignCandidateAggregate(
            library_entry_id=f"entry_{index}",
            strategy_fingerprint=f"fp_{index}",
            family="trend",
            archetype="moving_average_trend",
            market_count=3,
            robust_market_count=3,
            positive_stress_market_count=3,
            robust_market_ratio=Decimal("1"),
            positive_stress_ratio=Decimal("1"),
            median_return_percent=Decimal("5"),
            median_profit_factor=Decimal("1.5"),
            median_win_rate_percent=Decimal("55"),
            median_payoff_ratio=Decimal("1.5"),
            worst_drawdown_percent=Decimal("10"),
            cross_market_passed=True,
            rank_score=Decimal(str(100 - index)),
        )
        for index in range(3)
    ]

    allocations = allocate_campaign_strategies(candidates)

    assert sum(item.weight for item in allocations) <= Decimal("0.35")
