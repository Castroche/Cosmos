from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.agent.performance import build_forward_performance
from cosmos_quant.agent.schemas import (
    AgentAutonomyMode,
    TradeEpisode,
    TradeEpisodeStatus,
)
from cosmos_quant.configuration import AgentForwardValidationPreferences


def _episode(
    index: int,
    return_on_risk: str,
    *,
    symbol: str = "BTC/USDT",
    sequence: int | None = None,
) -> TradeEpisode:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    risk_multiple = Decimal(return_on_risk)
    identity = sequence if sequence is not None else index
    return TradeEpisode(
        id=f"episode_{symbol.replace('/', '_')}_{identity}",
        goal_id="sandbox-goal",
        policy_id="sandbox-policy",
        run_id=f"run_{symbol.replace('/', '_')}_{identity}",
        plan_id=f"plan_{symbol.replace('/', '_')}_{identity}",
        symbol=symbol,
        side="long",
        status=TradeEpisodeStatus.CLOSED,
        planned_risk=Decimal("100"),
        realized_pnl=risk_multiple * Decimal("100"),
        fees=Decimal("1"),
        return_on_risk=risk_multiple,
        opened_at=opened,
        closed_at=opened + timedelta(hours=1),
    )


def test_forward_performance_qualifies_consistent_sandbox_evidence():
    episodes = [_episode(index, "-0.4" if index % 3 == 0 else "0.5") for index in range(30)]

    report = build_forward_performance(
        episodes,
        AgentForwardValidationPreferences(),
    )

    assert report.scope_mode == AgentAutonomyMode.SANDBOX
    assert report.status == "qualified"
    assert report.qualified_for_live_review is True
    assert report.closed_episodes == 30
    assert report.independent_runs == 30
    assert report.net_pnl > 0
    assert report.profit_factor == Decimal("2.50")
    assert all(check.passed for check in report.checks)


def test_forward_performance_uses_independent_runs_not_elapsed_days():
    episodes = [_episode(index, "-0.4" if index % 3 == 0 else "0.5") for index in range(30)]
    for episode in episodes:
        episode.run_id = "one-concentrated-run"

    concentrated = build_forward_performance(
        episodes,
        AgentForwardValidationPreferences(),
    )

    assert concentrated.observation_days >= Decimal("28")
    assert concentrated.independent_runs == 1
    assert concentrated.status == "collecting"
    independent = next(
        check for check in concentrated.checks if check.code == "independent_execution_runs"
    )
    assert independent.passed is False

    same_day = [_episode(index, "-0.4" if index % 3 == 0 else "0.5") for index in range(30)]
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    for episode in same_day:
        episode.opened_at = instant
        episode.closed_at = instant + timedelta(hours=1)

    evidence_driven = build_forward_performance(
        same_day,
        AgentForwardValidationPreferences(),
    )

    assert evidence_driven.observation_days == Decimal("0.04")
    assert evidence_driven.independent_runs == 30
    assert evidence_driven.qualified_for_live_review is True


def test_forward_performance_rejects_stale_recent_edge():
    episodes = [
        *[_episode(index, "0.5") for index in range(20)],
        *[_episode(index, "-0.2") for index in range(20, 30)],
    ]

    report = build_forward_performance(
        episodes,
        AgentForwardValidationPreferences(),
    )

    assert report.net_pnl > 0
    assert report.recent_expectancy_r < 0
    assert report.status == "failed"
    assert report.qualified_for_live_review is False
    recent = next(check for check in report.checks if check.code == "recent_expectancy")
    assert recent.passed is False


def test_forward_performance_blocks_uncertain_execution_evidence():
    episodes = [_episode(index, "0.5") for index in range(30)]
    uncertain = _episode(31, "0")
    uncertain.status = TradeEpisodeStatus.UNCERTAIN
    uncertain.realized_pnl = None
    uncertain.return_on_risk = None
    uncertain.closed_at = None
    episodes.append(uncertain)

    report = build_forward_performance(
        episodes,
        AgentForwardValidationPreferences(),
    )

    assert report.status == "blocked"
    assert report.uncertain_episodes == 1
    reconciliation = next(
        check for check in report.checks if check.code == "uncertain_reconciliation"
    )
    assert reconciliation.passed is False


def test_forward_performance_requires_each_authorized_symbol_to_qualify():
    symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    episodes = [
        _episode(
            local_index * 2,
            "-0.4" if local_index % 3 == 0 else "0.5",
            symbol=symbol,
            sequence=symbol_index * 10 + local_index,
        )
        for symbol_index, symbol in enumerate(symbols)
        for local_index in range(10)
    ]

    report = build_forward_performance(
        episodes,
        AgentForwardValidationPreferences(),
        required_symbols=symbols,
    )

    assert report.qualified_for_live_review is True
    assert [item.symbol for item in report.symbol_reports] == symbols
    assert all(item.qualified for item in report.symbol_reports)
    symbol_check = next(check for check in report.checks if check.code == "symbol_evidence")
    assert symbol_check.passed is True

    incomplete = build_forward_performance(
        [episode for episode in episodes if episode.symbol != "SOL/USDT"],
        AgentForwardValidationPreferences(),
        required_symbols=symbols,
    )

    assert incomplete.qualified_for_live_review is False
    assert incomplete.status == "collecting"
    sol = next(item for item in incomplete.symbol_reports if item.symbol == "SOL/USDT")
    assert sol.attributable_episodes == 0
    assert sol.qualified is False
