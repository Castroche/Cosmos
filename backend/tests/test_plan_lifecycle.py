from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import SignalDirection, StrategySignal
from cosmos_quant.plan_lifecycle import EntrySignalState, assess_entry_signal
from cosmos_quant.strategy import ExplainableTrendStrategy


def _plan_and_signal():
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    strategy = ExplainableTrendStrategy()
    plan = strategy.generate(candles, snapshot, Decimal("100000"))
    plan.entry.expires_at = datetime.now(UTC) + timedelta(hours=2)
    signal = StrategySignal(
        algorithm="trend",
        symbol=plan.symbol,
        timeframe=plan.timeframe,
        direction=plan.side,
        regime=plan.regime,
        confidence=plan.confidence,
        signal_candle_at=candles[-1].timestamp,
        data_as_of=snapshot.timestamp,
        reasons=["fixture signal"],
    )
    return plan, signal


def test_flat_signal_degrades_but_does_not_invalidate_unexpired_plan():
    plan, signal = _plan_and_signal()
    flat = signal.model_copy(update={"direction": SignalDirection.FLAT})

    assessment = assess_entry_signal(plan, flat)

    assert assessment.state == EntrySignalState.DEGRADED
    assert assessment.execution_allowed is True
    assert assessment.blocking is False


def test_opposite_signal_invalidates_plan():
    plan, signal = _plan_and_signal()
    opposite = SignalDirection.SHORT if str(plan.side) == "long" else SignalDirection.LONG
    assessment = assess_entry_signal(
        plan,
        signal.model_copy(update={"direction": opposite}),
    )

    assert assessment.state == EntrySignalState.INVALIDATED
    assert assessment.execution_allowed is False
    assert assessment.blocking is True


def test_expired_plan_is_blocked_even_when_signal_is_aligned():
    plan, signal = _plan_and_signal()
    plan.entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assessment = assess_entry_signal(plan, signal)

    assert assessment.state == EntrySignalState.EXPIRED
    assert assessment.execution_allowed is False
