from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import PortfolioState, Position, ProfitTarget, Side
from cosmos_quant.risk import RiskEngine, RiskLimits
from cosmos_quant.strategy import ExplainableTrendStrategy


def test_fresh_plan_passes_default_risk_gates():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    portfolio = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )
    decision = RiskEngine().evaluate(plan, portfolio, market)
    assert decision.approved
    assert all(check.passed for check in decision.checks)


def test_ai_plan_requires_compiled_champion_provenance():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    plan.strategy_spec = {
        **plan.strategy_spec,
        "source": "ai",
    }
    plan.strategy_spec_fingerprint = "spec-ai"
    portfolio = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )

    raw = RiskEngine().evaluate(plan, portfolio, market)
    assert not next(
        check for check in raw.checks if check.code == "research_source_isolated"
    ).passed

    plan.research_candidate_id = "cand_ai"
    plan.validated_strategy_artifact = {
        "candidate_id": "cand_ai",
        "candidate_status": "champion",
        "source": "ai",
        "spec_fingerprint": "spec-ai",
        "dataset_id": "ds_ai",
        "data_fingerprint": "data_ai",
        "backtest_fingerprint": "bt_ai",
        "drift_status": "stable",
        "compiled_at": datetime.now(UTC).isoformat(),
    }
    compiled = RiskEngine().evaluate(plan, portfolio, market)
    assert next(
        check for check in compiled.checks if check.code == "research_source_isolated"
    ).passed


def test_stale_market_and_drawdown_reject_order():
    candles = demo_candles()
    market = snapshot_from_candles(candles).model_copy(
        update={"timestamp": datetime.now(UTC) - timedelta(minutes=1)}
    )
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    portfolio = PortfolioState(
        equity=Decimal("96000"),
        cash=Decimal("96000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )
    decision = RiskEngine().evaluate(plan, portfolio, market)
    failed = {check.code for check in decision.checks if not check.passed}
    assert not decision.approved
    assert {"market_freshness", "daily_drawdown"} <= failed


def test_swap_risk_rejects_insufficient_margin_and_leverage_mismatch():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    plan.market_type = "swap"
    plan.margin_mode = "cross"
    plan.leverage = Decimal("5")
    portfolio = PortfolioState(
        equity=Decimal("10000"),
        cash=Decimal("500"),
        available_margin=Decimal("500"),
        daily_start_equity=Decimal("10000"),
        gross_exposure=Decimal("0"),
    )
    decision = RiskEngine(
        RiskLimits(
            max_gross_exposure=Decimal("20"),
            max_symbol_exposure=Decimal("20"),
            max_leverage=Decimal("3"),
            execution_leverage=Decimal("3"),
        )
    ).evaluate(plan, portfolio, market)
    failed = {check.code for check in decision.checks if not check.passed}
    assert {"leverage", "margin_capacity"} <= failed


def test_swap_risk_rejects_opening_against_existing_net_position():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("10000"))
    plan.market_type = "swap"
    plan.margin_mode = "cross"
    plan.leverage = Decimal("3")
    plan.side = "short"
    portfolio = PortfolioState(
        equity=Decimal("10000"),
        cash=Decimal("9000"),
        available_margin=Decimal("9000"),
        daily_start_equity=Decimal("10000"),
        gross_exposure=Decimal("1000"),
        open_positions=[
            Position(
                symbol=plan.symbol,
                side="long",
                quantity=Decimal("0.02"),
                average_entry=market.last,
                mark_price=market.last,
            )
        ],
    )
    decision = RiskEngine(
        RiskLimits(
            max_gross_exposure=Decimal("10"),
            max_symbol_exposure=Decimal("10"),
            execution_leverage=Decimal("3"),
        )
    ).evaluate(plan, portfolio, market)
    assert not next(check for check in decision.checks if check.code == "position_direction").passed


def test_exchange_swap_rejects_missing_or_extreme_funding_rate():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("100000"))
    plan.market_type = "swap"
    plan.trading_mode = "sandbox"
    plan.margin_mode = "cross"
    plan.leverage = Decimal("3")
    portfolio = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        available_margin=Decimal("100000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )
    engine = RiskEngine(RiskLimits(execution_leverage=Decimal("3")))

    missing = engine.evaluate(plan, portfolio, market)
    assert not next(check for check in missing.checks if check.code == "funding_rate").passed

    extreme_market = market.model_copy(update={"funding_rate": Decimal("0.0015")})
    extreme = engine.evaluate(plan, portfolio, extreme_market)
    assert not next(check for check in extreme.checks if check.code == "funding_rate").passed

    normal_market = market.model_copy(update={"funding_rate": Decimal("0.0001")})
    normal = engine.evaluate(plan, portfolio, normal_market)
    assert next(check for check in normal.checks if check.code == "funding_rate").passed

    plan.side = "short"
    favorable_short = engine.evaluate(plan, portfolio, extreme_market)
    assert next(check for check in favorable_short.checks if check.code == "funding_rate").passed


def test_portfolio_budget_rejects_aggregate_and_same_direction_risk():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, market, Decimal("10000"))
    portfolio = PortfolioState(
        equity=Decimal("10000"),
        cash=Decimal("10000"),
        daily_start_equity=Decimal("10000"),
        gross_exposure=Decimal("3000"),
        open_positions=[
            Position(
                symbol="ETH/USDT",
                side=plan.side,
                quantity=Decimal("0.1"),
                average_entry=market.last,
                mark_price=market.last,
            )
        ],
    )
    decision = RiskEngine(
        RiskLimits(
            max_gross_exposure=Decimal("10"),
            max_symbol_exposure=Decimal("10"),
            max_portfolio_risk=Decimal("0.01"),
            existing_position_stress=Decimal("0.02"),
            max_directional_exposure=Decimal("0.10"),
        )
    ).evaluate(plan, portfolio, market)

    failed = {check.code for check in decision.checks if not check.passed}
    assert "portfolio_risk_budget" in failed
    assert "directional_concentration" in failed


def test_partial_profit_plan_uses_weighted_reward_risk_without_first_target_veto():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(
        candles,
        market,
        Decimal("100000"),
    )
    entry = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
    stop_distance = abs(entry - plan.exit.stop_loss)
    direction = Decimal("1") if plan.side == Side.LONG else Decimal("-1")
    plan.exit.take_profits = [
        ProfitTarget(
            price=entry + direction * stop_distance * Decimal("0.8"),
            close_fraction=Decimal("0.5"),
            rationale="early de-risk",
        ),
        ProfitTarget(
            price=entry + direction * stop_distance * Decimal("2.4"),
            close_fraction=Decimal("0.5"),
            rationale="trend continuation",
        ),
    ]
    portfolio = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )

    decision = RiskEngine().evaluate(plan, portfolio, market)

    assert decision.approved
    assert next(check for check in decision.checks if check.code == "reward_risk").passed
    first_target = next(
        check for check in decision.checks if check.code == "first_target_reward_risk"
    )
    assert first_target.passed is False
    assert first_target.blocking is False


def test_risk_recomputes_stop_loss_amount_instead_of_trusting_declared_fraction():
    candles = demo_candles()
    market = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(
        candles,
        market,
        Decimal("100000"),
    )
    plan.sizing.suggested_quantity *= Decimal("2")
    portfolio = PortfolioState(
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        daily_start_equity=Decimal("100000"),
        gross_exposure=Decimal("0"),
    )

    decision = RiskEngine().evaluate(plan, portfolio, market)

    assert decision.approved is False
    assert "sizing_consistency" in decision.blocking_failure_codes
    assert decision.summary.startswith("硬风控拒绝")
