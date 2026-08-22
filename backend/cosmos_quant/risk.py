from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from .domain import MarketSnapshot, PortfolioState, Side, StrategyPlan, TradingMode


class RiskLimits(BaseModel):
    max_risk_per_trade: Decimal = Field(default=Decimal("0.01"), gt=0, le=Decimal("0.05"))
    max_daily_drawdown: Decimal = Field(default=Decimal("0.03"), gt=0, le=Decimal("0.2"))
    max_gross_exposure: Decimal = Field(default=Decimal("1.5"), gt=0)
    max_portfolio_risk: Decimal = Field(default=Decimal("0.05"), gt=0, le=Decimal("0.2"))
    existing_position_stress: Decimal = Field(default=Decimal("0.02"), gt=0)
    max_directional_exposure: Decimal = Field(default=Decimal("1.25"), gt=0)
    max_leverage: Decimal = Field(default=Decimal("3"), ge=1)
    max_symbol_exposure: Decimal = Field(default=Decimal("0.75"), gt=0)
    max_open_positions: int = Field(default=5, ge=1)
    min_reward_risk: Decimal = Field(default=Decimal("1.5"), ge=1)
    max_spread_bps: Decimal = Field(default=Decimal("20"), gt=0)
    max_market_age_seconds: int = Field(default=15, gt=0)
    max_abs_funding_rate: Decimal = Field(default=Decimal("0.001"), gt=0)
    execution_leverage: Decimal = Field(default=Decimal("1"), ge=1)
    allow_short: bool = True
    operational_halt: bool = False
    halt_reason: str = ""


class RiskCheck(BaseModel):
    code: str
    passed: bool
    message: str
    blocking: bool = True


class RiskDecision(BaseModel):
    approved: bool
    checks: list[RiskCheck]
    blocking_failure_codes: list[str] = Field(default_factory=list)
    summary: str = ""


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        plan: StrategyPlan,
        portfolio: PortfolioState,
        market: MarketSnapshot,
        now: datetime | None = None,
    ) -> RiskDecision:
        now = now or datetime.now(UTC)
        market_time = market.timestamp
        if market_time.tzinfo is None:
            market_time = market_time.replace(tzinfo=UTC)
        age = max((now - market_time).total_seconds(), 0)
        entry_reference = (plan.entry.zone.lower + plan.entry.zone.upper) / Decimal("2")
        stop_distance = abs(entry_reference - plan.exit.stop_loss)
        first_target_distance = abs(plan.exit.take_profits[0].price - entry_reference)
        first_target_reward_risk = (
            first_target_distance / stop_distance if stop_distance > 0 else Decimal("0")
        )
        weighted_target_distance = sum(
            (
                abs(target.price - entry_reference) * target.close_fraction
                for target in plan.exit.take_profits
            ),
            Decimal("0"),
        )
        reward_risk = (
            weighted_target_distance / stop_distance if stop_distance > 0 else Decimal("0")
        )
        actual_stop_risk = stop_distance * plan.sizing.suggested_quantity
        sizing_tolerance = max(
            plan.sizing.risk_amount * Decimal("0.01"),
            Decimal("0.01"),
        )
        sizing_consistent = actual_stop_risk <= plan.sizing.risk_amount + sizing_tolerance
        effective_risk_fraction = (
            max(actual_stop_risk, plan.sizing.risk_amount) / portfolio.equity
            if portfolio.equity > 0
            else Decimal("999")
        )
        projected_exposure = portfolio.gross_exposure + plan.sizing.notional
        max_exposure = portfolio.equity * self.limits.max_gross_exposure
        projected_leverage = (
            projected_exposure / portfolio.equity if portfolio.equity > 0 else Decimal("999")
        )
        existing_symbol_exposure = sum(
            (
                abs(position.quantity * position.mark_price)
                for position in portfolio.open_positions
                if position.symbol == plan.symbol
            ),
            Decimal("0"),
        )
        projected_symbol_exposure = existing_symbol_exposure + plan.sizing.notional
        max_symbol_exposure = portfolio.equity * self.limits.max_symbol_exposure
        open_symbols = {position.symbol for position in portfolio.open_positions}
        projected_position_count = len(open_symbols | {plan.symbol})
        estimated_existing_risk = sum(
            (
                abs(position.quantity)
                * (
                    abs(position.mark_price - position.stop_loss)
                    if position.stop_loss is not None
                    else position.mark_price * self.limits.existing_position_stress
                )
                for position in portfolio.open_positions
            ),
            Decimal("0"),
        )
        projected_portfolio_risk = estimated_existing_risk + plan.sizing.risk_amount
        max_portfolio_risk = portfolio.equity * self.limits.max_portfolio_risk
        same_direction_exposure = sum(
            (
                abs(position.quantity * position.mark_price)
                for position in portfolio.open_positions
                if position.side == plan.side
            ),
            Decimal("0"),
        )
        projected_directional_exposure = same_direction_exposure + plan.sizing.notional
        max_directional_exposure = portfolio.equity * self.limits.max_directional_exposure
        direction_conflict = any(
            position.symbol == plan.symbol and position.side != plan.side
            for position in portfolio.open_positions
        )
        leverage = plan.leverage if plan.market_type == "swap" else Decimal("1")
        available_margin = (
            portfolio.available_margin if portfolio.available_margin is not None else portfolio.cash
        )
        required_capital = (
            plan.sizing.notional / leverage if plan.market_type == "swap" else plan.sizing.notional
        )
        entry_fraction_to_stop = (
            stop_distance / entry_reference if entry_reference > 0 else Decimal("999")
        )
        liquidation_buffer = Decimal("0.80") / leverage
        funding_rate = market.funding_rate
        research_source = plan.strategy_spec.get("source") if plan.strategy_spec else None
        compiled_artifact = plan.validated_strategy_artifact or {}
        research_source_passed = research_source not in {"ai", "ml"} or (
            compiled_artifact.get("candidate_status") == "champion"
            and compiled_artifact.get("source") == research_source
            and compiled_artifact.get("candidate_id") == plan.research_candidate_id
            and compiled_artifact.get("spec_fingerprint") == plan.strategy_spec_fingerprint
            and compiled_artifact.get("drift_status") not in {None, "", "critical", "unavailable"}
        )
        directional_funding_cost = (
            funding_rate
            if funding_rate is not None and plan.side == Side.LONG
            else -funding_rate
            if funding_rate is not None
            else None
        )
        funding_rate_passed = (
            plan.market_type != "swap"
            or (funding_rate is None and plan.trading_mode == TradingMode.PAPER)
            or (
                directional_funding_cost is not None
                and directional_funding_cost <= self.limits.max_abs_funding_rate
            )
        )

        direction_valid = (
            plan.side == Side.LONG and plan.exit.stop_loss < plan.entry.zone.lower
        ) or (plan.side == Side.SHORT and plan.exit.stop_loss > plan.entry.zone.upper)

        checks = [
            RiskCheck(
                code="operational_halt",
                passed=not self.limits.operational_halt,
                message=(
                    "交易状态正常"
                    if not self.limits.operational_halt
                    else f"系统已停机：{self.limits.halt_reason}"
                ),
            ),
            RiskCheck(
                code="market_freshness",
                passed=age <= self.limits.max_market_age_seconds,
                message=f"行情年龄 {age:.1f}s / 上限 {self.limits.max_market_age_seconds}s",
            ),
            RiskCheck(
                code="spread",
                passed=market.spread_bps <= self.limits.max_spread_bps,
                message=f"价差 {market.spread_bps} bps / 上限 {self.limits.max_spread_bps} bps",
            ),
            RiskCheck(
                code="funding_rate",
                passed=funding_rate_passed,
                message=(
                    "现货没有永续资金费率"
                    if plan.market_type != "swap"
                    else (
                        "纸面模式暂未取得资金费率，不影响策略研究"
                        if funding_rate is None and plan.trading_mode == TradingMode.PAPER
                        else (
                            "未取得实时资金费率，禁止交易所合约开仓"
                            if funding_rate is None
                            else (
                                f"当前资金费率 {funding_rate * 100:.4f}%，"
                                f"对{plan.side}方向的持仓成本 "
                                f"{directional_funding_cost * 100:.4f}% / "
                                f"上限 {self.limits.max_abs_funding_rate * 100:.4f}%"
                            )
                        )
                    )
                ),
            ),
            RiskCheck(
                code="trade_risk",
                passed=(
                    plan.sizing.risk_fraction <= self.limits.max_risk_per_trade
                    and effective_risk_fraction <= self.limits.max_risk_per_trade
                ),
                message=(
                    f"申报 {plan.sizing.risk_fraction * 100:.4f}%，"
                    f"按止损复算 {effective_risk_fraction * 100:.4f}% / "
                    f"上限 {self.limits.max_risk_per_trade * 100}%"
                ),
            ),
            RiskCheck(
                code="sizing_consistency",
                passed=sizing_consistent,
                message=(
                    f"数量×止损距离 {actual_stop_risk:.4f} / "
                    f"申报风险 {plan.sizing.risk_amount:.4f} USDT"
                ),
            ),
            RiskCheck(
                code="research_source_isolated",
                passed=research_source_passed,
                message=(
                    "交易计划来自系统或操作员审核规格"
                    if research_source_passed
                    else "AI/ML 候选规格禁止直接执行，必须经研究晋升后由操作员创建新计划"
                ),
            ),
            RiskCheck(
                code="daily_drawdown",
                passed=portfolio.daily_drawdown_fraction < self.limits.max_daily_drawdown,
                message=(
                    f"日内回撤 {portfolio.daily_drawdown_fraction * 100}% / "
                    f"熔断线 {self.limits.max_daily_drawdown * 100}%"
                ),
            ),
            RiskCheck(
                code="gross_exposure",
                passed=projected_exposure <= max_exposure,
                message=f"预计敞口 {projected_exposure:f} / 上限 {max_exposure:f} USDT",
            ),
            RiskCheck(
                code="portfolio_risk_budget",
                passed=projected_portfolio_risk <= max_portfolio_risk,
                message=(
                    f"组合压力风险 {projected_portfolio_risk:.2f} / "
                    f"预算 {max_portfolio_risk:.2f} USDT"
                ),
            ),
            RiskCheck(
                code="directional_concentration",
                passed=projected_directional_exposure <= max_directional_exposure,
                message=(
                    f"同向预计敞口 {projected_directional_exposure:.2f} / "
                    f"上限 {max_directional_exposure:.2f} USDT"
                ),
            ),
            RiskCheck(
                code="leverage",
                passed=(
                    projected_leverage <= self.limits.max_leverage
                    and leverage <= self.limits.max_leverage
                    and leverage == self.limits.execution_leverage
                ),
                message=(
                    f"计划 {leverage:f}x / 账户配置 "
                    f"{self.limits.execution_leverage:f}x；组合预计 "
                    f"{projected_leverage:.2f}x / 风控上限 "
                    f"{self.limits.max_leverage}x"
                ),
            ),
            RiskCheck(
                code="margin_capacity",
                passed=required_capital <= available_margin,
                message=(
                    f"预计占用保证金 {required_capital:.2f} / 可用 {available_margin:.2f} USDT"
                    if plan.market_type == "swap"
                    else f"预计占用资金 {required_capital:.2f} / 可用 {available_margin:.2f} USDT"
                ),
            ),
            RiskCheck(
                code="liquidation_buffer",
                passed=(plan.market_type != "swap" or entry_fraction_to_stop < liquidation_buffer),
                message=(
                    (
                        f"止损距离 {entry_fraction_to_stop * 100:.2f}% / "
                        f"{leverage:f}x 清算缓冲上限 {liquidation_buffer * 100:.2f}%"
                    )
                    if plan.market_type == "swap"
                    else "现货仓位没有强制清算风险"
                ),
            ),
            RiskCheck(
                code="symbol_concentration",
                passed=projected_symbol_exposure <= max_symbol_exposure,
                message=(
                    f"{plan.symbol} 预计敞口 {projected_symbol_exposure:f} / "
                    f"上限 {max_symbol_exposure:f} USDT"
                ),
            ),
            RiskCheck(
                code="position_count",
                passed=projected_position_count <= self.limits.max_open_positions,
                message=(
                    f"预计持仓品种 {projected_position_count} / "
                    f"上限 {self.limits.max_open_positions}"
                ),
            ),
            RiskCheck(
                code="position_direction",
                passed=not direction_conflict,
                message=(
                    "当前品种没有反向持仓"
                    if not direction_conflict
                    else "当前品种存在反向持仓；单向持仓模式下必须先平仓再反向开仓"
                ),
            ),
            RiskCheck(
                code="reward_risk",
                passed=reward_risk >= self.limits.min_reward_risk,
                message=(
                    f"按各档减仓比例加权盈亏比 {reward_risk:.2f} / "
                    f"下限 {self.limits.min_reward_risk}"
                ),
            ),
            RiskCheck(
                code="first_target_reward_risk",
                passed=first_target_reward_risk >= Decimal("1"),
                blocking=False,
                message=(
                    f"第一档止盈 {first_target_reward_risk:.2f}R；"
                    "仅作执行结构提示，不重复阻断已通过的组合盈亏比"
                ),
            ),
            RiskCheck(
                code="stop_direction",
                passed=direction_valid,
                message="止损位于开仓区间的正确方向" if direction_valid else "止损方向错误",
            ),
            RiskCheck(
                code="short_permission",
                passed=self.limits.allow_short or plan.side != Side.SHORT,
                message=(
                    "当前市场允许做空" if self.limits.allow_short else "现货账户禁止建立空头仓位"
                ),
            ),
        ]
        blocking_failures = [item.code for item in checks if item.blocking and not item.passed]
        return RiskDecision(
            approved=not blocking_failures,
            checks=checks,
            blocking_failure_codes=blocking_failures,
            summary=(
                "全部硬风控通过"
                if not blocking_failures
                else "硬风控拒绝：" + "、".join(blocking_failures)
            ),
        )
