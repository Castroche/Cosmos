import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.configuration import SUPPORTED_TIMEFRAMES
from cosmos_quant.demo import demo_candles
from cosmos_quant.domain import Candle, MarketSnapshot
from cosmos_quant.market_data import TIMEFRAME_SECONDS, MarketDataService, validate_candles
from cosmos_quant.storage import SqliteStore
from cosmos_quant.timeframes import (
    closed_candles,
    periods_per_year,
    resample_closed_candles,
)
from cosmos_quant.websocket_market import PublicTickerStream


def test_supported_timeframes_stay_consistent_across_configuration_and_market_data():
    assert set(TIMEFRAME_SECONDS) == SUPPORTED_TIMEFRAMES


def test_strategy_history_excludes_the_forming_candle():
    candles = [
        Candle(
            timestamp=datetime(2026, 7, 13, hour, tzinfo=UTC),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("101"),
            volume=Decimal("8"),
        )
        for hour in (8, 9, 10)
    ]

    result = closed_candles(
        candles,
        "1h",
        datetime(2026, 7, 13, 10, 30, tzinfo=UTC),
    )

    assert [candle.timestamp.hour for candle in result] == [8, 9]


def test_crypto_annualization_respects_the_selected_timeframe():
    assert periods_per_year("1h") == 365 * 24
    assert periods_per_year("4h") == 365 * 6
    assert periods_per_year("1d") == 365


def test_resampling_excludes_incomplete_higher_timeframe_bar():
    candles = demo_candles(10)

    result = resample_closed_candles(candles, "1h", "4h")

    assert all(candle.timestamp.hour % 4 == 0 for candle in result)
    assert len(result) <= 2
    assert all(candle.volume > 0 for candle in result)


def test_market_quality_detects_gaps_duplicates_and_invalid_ohlc():
    candles = demo_candles(60)
    duplicate = candles[-1]
    invalid = Candle(
        timestamp=candles[-1].timestamp + timedelta(hours=3),
        open=Decimal("100"),
        high=Decimal("90"),
        low=Decimal("80"),
        close=Decimal("95"),
        volume=Decimal("10"),
    )
    valid, quality = validate_candles([*candles, duplicate, invalid], "1h")

    assert len(valid) == 60
    assert quality.duplicate_count == 1
    assert quality.invalid_count == 1
    assert quality.status == "degraded"


def test_okx_ticker_parser_maps_exchange_payload():
    payload = json.dumps(
        {
            "data": [
                {
                    "ts": "1783920000000",
                    "bidPx": "64200.1",
                    "askPx": "64200.2",
                    "last": "64200.15",
                }
            ]
        }
    )
    snapshot = PublicTickerStream.parse_okx(payload, "BTC/USDT")

    assert snapshot is not None
    assert snapshot.bid == Decimal("64200.1")
    assert snapshot.source == "okx:websocket"


def test_binance_ticker_parser_uses_event_timestamp():
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    payload = json.dumps({"E": timestamp, "b": "64199.9", "a": "64200.1"})
    snapshot = PublicTickerStream.parse_binance(payload, "BTC/USDT")

    assert snapshot is not None
    assert snapshot.last == Decimal("64200.0")
    assert snapshot.timestamp.tzinfo == UTC


def test_websocket_tick_updates_forming_candle_and_publishes(tmp_path):
    service = MarketDataService(SqliteStore(tmp_path / "market.db"))
    candle = Candle(
        timestamp=datetime(2026, 7, 13, 8, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("8"),
    )
    service._live_candles[("BTC/USDT", "1h")] = [candle]
    queue = service.subscribe()

    service.update_websocket(
        MarketSnapshot(
            symbol="BTC/USDT",
            timestamp=datetime(2026, 7, 13, 8, 30, tzinfo=UTC),
            bid=Decimal("102.9"),
            ask=Decimal("103.1"),
            last=Decimal("103"),
            source="okx:websocket",
        )
    )

    event = queue.get_nowait()
    forming = service._live_candles[("BTC/USDT", "1h")][-1]
    assert event["candles"]["1h"]["close"] == "103"
    assert forming.high == Decimal("103")
    assert forming.volume == Decimal("8")

    service.update_websocket(
        MarketSnapshot(
            symbol="BTC/USDT",
            timestamp=datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
            bid=Decimal("103.9"),
            ask=Decimal("104.1"),
            last=Decimal("104"),
            source="okx:websocket",
        )
    )
    candles = service._live_candles[("BTC/USDT", "1h")]
    assert len(candles) == 2
    assert candles[-1].timestamp == datetime(2026, 7, 13, 9, tzinfo=UTC)
    assert candles[-1].close == Decimal("104")


def test_okx_native_candle_parser_maps_ohlcv_and_confirmation():
    payload = json.dumps(
        {
            "arg": {"channel": "candle1H", "instId": "BTC-USDT"},
            "data": [
                [
                    "1783929600000",
                    "63000",
                    "63200",
                    "62900",
                    "63150",
                    "123.45",
                    "0",
                    "0",
                    "0",
                ]
            ],
        }
    )

    parsed = PublicTickerStream.parse_okx_candle(payload)

    assert parsed is not None
    candle, confirmed = parsed
    assert candle.close == Decimal("63150")
    assert candle.volume == Decimal("123.45")
    assert not confirmed


def test_native_candle_replaces_synthetic_bar_with_exchange_volume(tmp_path):
    service = MarketDataService(SqliteStore(tmp_path / "native-candle.db"))
    initial = Candle(
        timestamp=datetime(2026, 7, 13, 8, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )
    service._live_candles[("BTC/USDT", "1h")] = [initial]
    service.websocket_snapshot = MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=datetime(2026, 7, 13, 8, 30, tzinfo=UTC),
        bid=Decimal("102.9"),
        ask=Decimal("103.1"),
        last=Decimal("103"),
        source="okx:websocket",
    )
    queue = service.subscribe()
    native = initial.model_copy(
        update={"high": Decimal("104"), "close": Decimal("103"), "volume": Decimal("9.5")}
    )

    service.update_exchange_candle("BTC/USDT", "1h", native, False)

    event = queue.get_nowait()
    assert service._live_candles[("BTC/USDT", "1h")][-1].volume == Decimal("9.5")
    assert event["candles"]["1h"]["volume"] == "9.5"
    assert event["candle_confirmed"] is False


def test_identical_concurrent_market_history_requests_are_coalesced(tmp_path):
    candles = demo_candles(80)

    class Adapter:
        def __init__(self):
            self.snapshot_calls = 0
            self.history_calls = 0

        async def market_snapshot(self, symbol):
            self.snapshot_calls += 1
            await asyncio.sleep(0.01)
            return MarketSnapshot(
                symbol=symbol,
                timestamp=candles[-1].timestamp,
                bid=candles[-1].close,
                ask=candles[-1].close,
                last=candles[-1].close,
                source="test",
            )

        async def fetch_candles(self, _symbol, _timeframe, _limit):
            self.history_calls += 1
            await asyncio.sleep(0.01)
            return candles

    async def scenario():
        service = MarketDataService(SqliteStore(tmp_path / "coalesced-market.db"))
        adapter = Adapter()
        results = await asyncio.gather(
            *(
                service.fetch(
                    adapter,
                    "okx",
                    "BTC/USDT",
                    "1h",
                    80,
                )
                for _ in range(4)
            )
        )
        return adapter, results

    adapter, results = asyncio.run(scenario())

    assert adapter.snapshot_calls == 1
    assert adapter.history_calls == 1
    assert all(len(result[1]) == 80 for result in results)


def test_okx_candle_channel_timeframe_mapping():
    assert PublicTickerStream._okx_bar("3m") == "3m"
    assert PublicTickerStream._okx_bar("15m") == "15m"
    assert PublicTickerStream._okx_bar("1h") == "1H"
    assert PublicTickerStream._okx_bar("2h") == "2H"
    assert PublicTickerStream._okx_bar("1d") == "1D"
    assert PublicTickerStream._okx_bar("1w") == "1W"
    assert PublicTickerStream._timeframe_from_okx_channel("candle4H") == "4h"
