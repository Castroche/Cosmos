from decimal import Decimal

from cosmos_quant.demo import demo_candles, snapshot_from_candles
from cosmos_quant.domain import Side
from cosmos_quant.strategy import ExplainableTrendStrategy
from cosmos_quant.timeframes import candle_close_time, timeframe_delta


def test_strategy_emits_explicit_and_valid_price_levels():
    candles = demo_candles()
    snapshot = snapshot_from_candles(candles)
    plan = ExplainableTrendStrategy().generate(candles, snapshot, Decimal("100000"))

    assert plan.entry.zone.lower <= snapshot.last <= plan.entry.zone.upper
    assert plan.sizing.risk_amount == Decimal("750.00")
    assert plan.sizing.suggested_quantity > 0
    assert plan.fingerprint
    assert plan.signal_candle_at == candles[-1].timestamp
    assert plan.data_as_of == snapshot.timestamp
    assert plan.entry.expires_at == (
        candle_close_time(candles[-1], "1h") + timeframe_delta("1h", 8)
    )
    assert "止损" in plan.human_summary
    assert "止盈" in plan.human_summary
    if plan.side == Side.LONG:
        assert plan.exit.stop_loss < plan.entry.zone.lower
        assert all(target.price > plan.entry.zone.upper for target in plan.exit.take_profits)
    else:
        assert plan.exit.stop_loss > plan.entry.zone.upper
        assert all(target.price < plan.entry.zone.lower for target in plan.exit.take_profits)


def test_strategy_rejects_insufficient_history():
    candles = demo_candles(30)
    snapshot = snapshot_from_candles(candles)
    try:
        ExplainableTrendStrategy().generate(candles, snapshot, Decimal("100000"))
    except ValueError as exc:
        assert "55" in str(exc)
    else:
        raise AssertionError("expected insufficient-history error")
