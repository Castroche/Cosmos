from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from ..configuration import AgentForwardValidationPreferences
from .schemas import AgentAutonomyMode, TradeEpisode, TradeEpisodeStatus


class ForwardValidationCheck(BaseModel):
    code: str
    passed: bool
    current: str
    requirement: str
    message: str


class AgentSymbolForwardPerformance(BaseModel):
    symbol: str
    status: Literal["collecting", "blocked", "failed", "qualified"]
    qualified: bool
    attributable_episodes: int
    independent_runs: int
    wins: int
    losses: int
    breakeven: int
    win_rate_percent: Decimal
    net_pnl: Decimal
    total_r: Decimal
    expectancy_r: Decimal
    recent_episodes: int
    recent_expectancy_r: Decimal
    profit_factor: Decimal | None = None
    max_drawdown_r: Decimal
    max_consecutive_losses: int
    observation_days: Decimal
    checks: list[ForwardValidationCheck] = Field(default_factory=list)


class AgentForwardPerformance(BaseModel):
    scope_mode: AgentAutonomyMode = AgentAutonomyMode.SANDBOX
    status: Literal["collecting", "blocked", "failed", "qualified"]
    qualified_for_live_review: bool
    closed_episodes: int
    attributable_episodes: int
    independent_runs: int
    uncertain_episodes: int
    wins: int
    losses: int
    breakeven: int
    win_rate_percent: Decimal
    net_pnl: Decimal
    total_fees: Decimal
    total_r: Decimal
    expectancy_r: Decimal
    recent_episodes: int
    recent_total_r: Decimal
    recent_expectancy_r: Decimal
    profit_factor: Decimal | None = None
    max_drawdown_r: Decimal
    max_consecutive_losses: int
    observation_days: Decimal
    first_episode_at: datetime | None = None
    last_episode_at: datetime | None = None
    checks: list[ForwardValidationCheck] = Field(default_factory=list)
    symbol_reports: list[AgentSymbolForwardPerformance] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def _q(value: Decimal, places: str = "0.01") -> Decimal:
    return value.quantize(Decimal(places))


def build_forward_performance(
    episodes: list[TradeEpisode],
    preferences: AgentForwardValidationPreferences,
    *,
    scope_mode: AgentAutonomyMode = AgentAutonomyMode.SANDBOX,
    required_symbols: list[str] | None = None,
) -> AgentForwardPerformance:
    closed = sorted(
        (
            episode
            for episode in episodes
            if episode.status == TradeEpisodeStatus.CLOSED and episode.closed_at
        ),
        key=lambda episode: episode.closed_at or episode.opened_at,
    )
    attributable = [
        episode
        for episode in closed
        if episode.realized_pnl is not None and episode.return_on_risk is not None
    ]
    independent_runs = len({episode.run_id for episode in attributable})
    uncertain_count = sum(episode.status == TradeEpisodeStatus.UNCERTAIN for episode in episodes)
    returns = [episode.return_on_risk or Decimal("0") for episode in attributable]
    wins = sum(value > 0 for value in returns)
    losses = sum(value < 0 for value in returns)
    breakeven = len(returns) - wins - losses
    gross_profit_r = sum((value for value in returns if value > 0), Decimal("0"))
    gross_loss_r = abs(sum((value for value in returns if value < 0), Decimal("0")))
    profit_factor = gross_profit_r / gross_loss_r if gross_loss_r > 0 else None
    total_r = sum(returns, Decimal("0"))
    expectancy_r = total_r / len(returns) if returns else Decimal("0")
    net_pnl = sum(
        (episode.realized_pnl or Decimal("0") for episode in attributable),
        Decimal("0"),
    )
    total_fees = sum((episode.fees for episode in attributable), Decimal("0"))
    win_rate = Decimal(wins) / Decimal(len(returns)) * Decimal("100") if returns else Decimal("0")

    cumulative = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    consecutive_losses = 0
    max_consecutive_losses = 0
    for value in returns:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        if value < 0:
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0

    recent_count = min(len(returns), preferences.minimum_recent_episodes)
    recent_returns = returns[-recent_count:] if recent_count else []
    recent_total_r = sum(recent_returns, Decimal("0"))
    recent_expectancy_r = recent_total_r / recent_count if recent_count else Decimal("0")

    first_at = min((episode.opened_at for episode in closed), default=None)
    last_at = max(
        (episode.closed_at for episode in closed if episode.closed_at),
        default=None,
    )
    observation_days = (
        Decimal(str((last_at - first_at).total_seconds() / 86400))
        if first_at and last_at
        else Decimal("0")
    )
    attribution_complete = len(attributable) == len(closed)
    profit_factor_passed = bool(
        gross_profit_r > 0
        and (
            gross_loss_r == 0
            or (profit_factor is not None and profit_factor >= preferences.minimum_profit_factor)
        )
    )
    checks = [
        ForwardValidationCheck(
            code="episode_attribution",
            passed=attribution_complete,
            current=f"{len(attributable)}/{len(closed)}",
            requirement="全部已平仓回合具备净收益与 R 倍数",
            message="成交、费用和计划风险必须能够确定性归因",
        ),
        ForwardValidationCheck(
            code="uncertain_reconciliation",
            passed=uncertain_count == 0,
            current=str(uncertain_count),
            requirement="0 个不确定回合",
            message="存在不确定外部执行效果时禁止用绩效支持实盘",
        ),
        ForwardValidationCheck(
            code="closed_sample",
            passed=len(attributable) >= preferences.minimum_closed_episodes,
            current=str(len(attributable)),
            requirement=f"≥ {preferences.minimum_closed_episodes}",
            message="需要足够多的测试网完整开平仓样本",
        ),
        ForwardValidationCheck(
            code="independent_execution_runs",
            passed=independent_runs >= preferences.minimum_independent_runs,
            current=str(independent_runs),
            requirement=f"≥ {preferences.minimum_independent_runs}",
            message=(
                "前向证据按独立 Agent 执行批次计数，不再使用固定日历天数作为盈利门槛"
            ),
        ),
        ForwardValidationCheck(
            code="net_profit",
            passed=net_pnl > 0,
            current=f"{_q(net_pnl):f} USDT",
            requirement="> 0 USDT",
            message="已扣除可归因手续费后的累计净收益必须为正",
        ),
        ForwardValidationCheck(
            code="profit_factor",
            passed=profit_factor_passed,
            current=(
                "∞"
                if gross_loss_r == 0 and gross_profit_r > 0
                else (f"{_q(profit_factor):f}" if profit_factor is not None else "—")
            ),
            requirement=f"≥ {preferences.minimum_profit_factor:f}",
            message="盈利 R 与亏损 R 的比值必须保持正优势",
        ),
        ForwardValidationCheck(
            code="expectancy",
            passed=expectancy_r >= preferences.minimum_expectancy_r,
            current=f"{_q(expectancy_r, '0.001'):f}R",
            requirement=f"≥ {preferences.minimum_expectancy_r:f}R",
            message="每个完整交易回合的风险标准化期望必须为正",
        ),
        ForwardValidationCheck(
            code="recent_expectancy",
            passed=(
                recent_count >= preferences.minimum_recent_episodes and recent_expectancy_r > 0
            ),
            current=f"{recent_count} 笔 / {_q(recent_expectancy_r, '0.001'):f}R",
            requirement=(f"最近 {preferences.minimum_recent_episodes} 笔平均期望 > 0R"),
            message="近期表现必须仍然为正，避免只依赖早期收益",
        ),
        ForwardValidationCheck(
            code="drawdown",
            passed=max_drawdown <= preferences.maximum_drawdown_r,
            current=f"{_q(max_drawdown, '0.001'):f}R",
            requirement=f"≤ {preferences.maximum_drawdown_r:f}R",
            message="累计风险收益曲线的峰谷回撤必须受控",
        ),
        ForwardValidationCheck(
            code="loss_streak",
            passed=(max_consecutive_losses <= preferences.maximum_consecutive_losses),
            current=str(max_consecutive_losses),
            requirement=f"≤ {preferences.maximum_consecutive_losses}",
            message="连续亏损不能超过前向验收边界",
        ),
    ]
    symbol_reports: list[AgentSymbolForwardPerformance] = []
    normalized_symbols = list(dict.fromkeys(required_symbols or []))
    if normalized_symbols:
        symbol_count = len(normalized_symbols)
        per_symbol_preferences = preferences.model_copy(
            update={
                "minimum_closed_episodes": max(
                    10,
                    (preferences.minimum_closed_episodes + symbol_count - 1) // symbol_count,
                ),
                "minimum_recent_episodes": max(
                    5,
                    (preferences.minimum_recent_episodes + symbol_count - 1) // symbol_count,
                ),
                "minimum_independent_runs": max(
                    5,
                    (preferences.minimum_independent_runs + symbol_count - 1) // symbol_count,
                ),
            }
        )
        for symbol in normalized_symbols:
            symbol_report = build_forward_performance(
                [episode for episode in episodes if episode.symbol == symbol],
                per_symbol_preferences,
                scope_mode=scope_mode,
            )
            symbol_reports.append(
                AgentSymbolForwardPerformance(
                    symbol=symbol,
                    status=symbol_report.status,
                    qualified=symbol_report.qualified_for_live_review,
                    attributable_episodes=symbol_report.attributable_episodes,
                    independent_runs=symbol_report.independent_runs,
                    wins=symbol_report.wins,
                    losses=symbol_report.losses,
                    breakeven=symbol_report.breakeven,
                    win_rate_percent=symbol_report.win_rate_percent,
                    net_pnl=symbol_report.net_pnl,
                    total_r=symbol_report.total_r,
                    expectancy_r=symbol_report.expectancy_r,
                    recent_episodes=symbol_report.recent_episodes,
                    recent_expectancy_r=symbol_report.recent_expectancy_r,
                    profit_factor=symbol_report.profit_factor,
                    max_drawdown_r=symbol_report.max_drawdown_r,
                    max_consecutive_losses=symbol_report.max_consecutive_losses,
                    observation_days=symbol_report.observation_days,
                    checks=symbol_report.checks,
                )
            )
        qualified_symbols = sum(report.qualified for report in symbol_reports)
        checks.append(
            ForwardValidationCheck(
                code="symbol_evidence",
                passed=qualified_symbols == len(symbol_reports),
                current=f"{qualified_symbols}/{len(symbol_reports)}",
                requirement="每个授权品种独立通过前向验收",
                message=(
                    "组合总体盈利不能掩盖单个品种证据不足或负期望；"
                    "每个品种必须分别满足样本、近期优势、收益和回撤边界"
                ),
            )
        )

    qualified = all(check.passed for check in checks)
    if qualified:
        status = "qualified"
    elif not attribution_complete or uncertain_count:
        status = "blocked"
    elif (
        len(attributable) < preferences.minimum_closed_episodes
        or independent_runs < preferences.minimum_independent_runs
        or any(report.status == "collecting" for report in symbol_reports)
    ):
        status = "collecting"
    else:
        status = "failed"
    return AgentForwardPerformance(
        scope_mode=scope_mode,
        status=status,
        qualified_for_live_review=qualified,
        closed_episodes=len(closed),
        attributable_episodes=len(attributable),
        independent_runs=independent_runs,
        uncertain_episodes=uncertain_count,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        win_rate_percent=_q(win_rate),
        net_pnl=_q(net_pnl),
        total_fees=_q(total_fees),
        total_r=_q(total_r, "0.001"),
        expectancy_r=_q(expectancy_r, "0.001"),
        recent_episodes=recent_count,
        recent_total_r=_q(recent_total_r, "0.001"),
        recent_expectancy_r=_q(recent_expectancy_r, "0.001"),
        profit_factor=_q(profit_factor) if profit_factor is not None else None,
        max_drawdown_r=_q(max_drawdown, "0.001"),
        max_consecutive_losses=max_consecutive_losses,
        observation_days=_q(observation_days),
        first_episode_at=first_at,
        last_episode_at=last_at,
        checks=checks,
        symbol_reports=symbol_reports,
    )
