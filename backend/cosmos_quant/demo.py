from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import sin

from .domain import Candle, MarketSnapshot


def demo_candles(count: int = 180, base: Decimal = Decimal("64280")) -> list[Candle]:
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=count)
    candles: list[Candle] = []
    previous = base * Decimal("0.93")
    for index in range(count):
        trend = base * (Decimal("0.93") + Decimal(index) / Decimal(count) * Decimal("0.07"))
        wave = Decimal(str(sin(index / 5.3))) * Decimal("520")
        close = trend + wave
        open_price = previous
        high = max(open_price, close) + Decimal("240") + Decimal(index % 7) * Decimal("13")
        low = min(open_price, close) - Decimal("230") - Decimal(index % 5) * Decimal("11")
        volume = Decimal("850") + Decimal(index % 19) * Decimal("31")
        candles.append(
            Candle(
                timestamp=start + timedelta(hours=index),
                open=open_price.quantize(Decimal("0.01")),
                high=high.quantize(Decimal("0.01")),
                low=low.quantize(Decimal("0.01")),
                close=close.quantize(Decimal("0.01")),
                volume=volume,
            )
        )
        previous = close
    return candles


def snapshot_from_candles(candles: list[Candle]) -> MarketSnapshot:
    last = candles[-1].close
    return MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=datetime.now(UTC),
        bid=last - Decimal("4.20"),
        ask=last + Decimal("4.20"),
        last=last,
        source="COSMOS Paper",
    )
