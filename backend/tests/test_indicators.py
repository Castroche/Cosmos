from decimal import Decimal

from cosmos_quant.demo import demo_candles
from cosmos_quant.indicators import (
    INDICATOR_CATALOG,
    average_directional_index,
    bollinger_bands,
    commodity_channel_index,
    macd,
    on_balance_volume,
    rate_of_change,
    recommend_indicator_stack,
    stochastic_oscillator,
    volume_weighted_average_price,
    williams_r,
)


def test_indicator_catalog_covers_trend_momentum_volume_and_crypto_context():
    assert len(INDICATOR_CATALOG) >= 15
    assert {
        "macd",
        "adx",
        "rsi",
        "bollinger_bands",
        "obv",
        "vwap",
        "funding_rate",
        "open_interest",
        "order_book_imbalance",
    }.issubset(INDICATOR_CATALOG)


def test_extended_closed_candle_indicators_return_bounded_values():
    candles = demo_candles(120)
    closes = [item.close for item in candles]

    bands = bollinger_bands(closes)
    macd_value = macd(closes)
    stochastic = stochastic_oscillator(candles)

    assert bands["lower"] <= bands["middle"] <= bands["upper"]
    assert macd_value["histogram"] == macd_value["line"] - macd_value["signal"]
    assert Decimal("0") <= stochastic["k"] <= Decimal("100")
    assert Decimal("0") <= stochastic["d"] <= Decimal("100")
    assert Decimal("0") <= average_directional_index(candles) <= Decimal("100")
    assert Decimal("-100") <= williams_r(candles) <= Decimal("0")
    assert isinstance(commodity_channel_index(candles), Decimal)
    assert isinstance(on_balance_volume(candles), Decimal)
    assert volume_weighted_average_price(candles) > 0
    assert isinstance(rate_of_change(closes), Decimal)


def test_indicator_stack_is_regime_specific_and_non_redundant():
    trend = recommend_indicator_stack("persistent_trend")
    range_stack = recommend_indicator_stack("stable_range")

    assert [item["name"] for item in trend] == [
        "ema",
        "adx",
        "atr",
        "open_interest",
    ]
    assert "rsi" in {item["name"] for item in range_stack}
    assert all(item.get("warning") for item in trend)


def test_latest_adx_is_independent_of_irrelevant_older_history():
    candles = demo_candles(240)

    assert average_directional_index(candles) == average_directional_index(candles[-29:])
