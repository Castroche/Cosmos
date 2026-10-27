from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.trade_history import build_trade_history_v2
from cosmos_quant.trading_intelligence import (
    DecisionEpisode,
    DeltaContext,
    PositionRuntimeV2,
    PositionStateV2,
    RiskAllocation,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def position(identifier: str, *, minutes_ago: int, pnl: str) -> PositionRuntimeV2:
    opened = NOW - timedelta(minutes=minutes_ago + 30)
    return PositionRuntimeV2(
        position_id=identifier,
        plan_id=f"plan_{identifier}",
        episode_id=f"episode_{identifier}",
        symbol="ETH/USDT:USDT",
        side="LONG",
        state=PositionStateV2.CLOSED,
        original_quantity="1",
        remaining_quantity="0",
        average_entry="2000",
        initial_stop="1980",
        current_stop="1980",
        accumulated_fees="5",
        opened_at=opened,
        closed_at=NOW - timedelta(minutes=minutes_ago),
        exit_reason="target" if Decimal(pnl) > 0 else "stop",
    )


def episode(identifier: str, *, pnl: str, r_value: str) -> DecisionEpisode:
    return DecisionEpisode(
        episode_id=f"episode_{identifier}",
        symbol="ETH/USDT:USDT",
        status="COMPLETED",
        market_state_hash=f"state_{identifier}",
        delta=DeltaContext(current_state_hash=f"state_{identifier}"),
        risk_allocation_id=f"risk_{identifier}",
        position_id=identifier,
        outcome={
            "episode_id": f"episode_{identifier}",
            "action": "LONG",
            "direction": "LONG",
            "notional": "2000",
            "gross_pnl": str(Decimal(pnl) + Decimal("5")),
            "realized_pnl": pnl,
            "exit_price": "2125" if Decimal(pnl) > 0 else "1950",
            "return_after_cost": str(Decimal(pnl) / Decimal("2000")),
            "return_on_risk": r_value,
            "fees": "5",
        },
    )


def allocation(identifier: str) -> RiskAllocation:
    return RiskAllocation(
        allocation_id=f"risk_{identifier}",
        plan_id=f"plan_{identifier}",
        equity="10000",
        risk_amount="50",
        quantity="1",
        notional="2000",
        leverage="1",
        constraints=["test"],
        configuration_fingerprint="test",
    )


def test_trade_history_builds_net_statistics_and_equity_curve() -> None:
    positions = [
        position("win", minutes_ago=90, pnl="120"),
        position("loss", minutes_ago=30, pnl="-50"),
    ]
    episodes = {
        "episode_win": episode("win", pnl="120", r_value="2"),
        "episode_loss": episode("loss", pnl="-50", r_value="-1"),
    }
    allocations = {
        "risk_win": allocation("win"),
        "risk_loss": allocation("loss"),
    }

    report = build_trade_history_v2(
        positions,
        episodes,
        allocations,
        symbol="ETH/USDT:USDT",
        days=7,
        now=NOW,
    )

    assert report.summary.trade_count == 2
    assert report.summary.win_rate == Decimal("0.5")
    assert report.summary.net_pnl == Decimal("70")
    assert report.summary.profit_factor == Decimal("2.4")
    assert report.summary.maximum_drawdown == Decimal("50")
    assert report.summary.total_fees == Decimal("10")
    assert report.summary.average_return_on_risk == Decimal("0.5")
    assert [item.cumulative_pnl for item in report.equity_curve] == [
        Decimal("120"),
        Decimal("70"),
    ]
    assert report.items[0].position_id == "loss"
    assert report.items[1].exit_price == Decimal("2125")


def test_trade_history_uses_allocation_to_recover_legacy_pnl() -> None:
    legacy = episode("legacy", pnl="0", r_value="0")
    legacy.outcome = {
        "episode_id": "episode_legacy",
        "action": "LONG",
        "direction": "LONG",
        "return_after_cost": "0.025",
        "return_on_risk": "1",
        "fees": "3",
    }

    report = build_trade_history_v2(
        [position("legacy", minutes_ago=20, pnl="0")],
        {"episode_legacy": legacy},
        {"risk_legacy": allocation("legacy")},
        days=0,
        now=NOW,
    )

    assert report.items[0].realized_pnl == Decimal("50")
    assert report.items[0].gross_pnl == Decimal("53")
