import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter


def test_binance_swap_uses_scope_exact_usdm_client():
    assert CcxtExchangeAdapter._ccxt_client_exchange_id("binance", "swap") == "binanceusdm"
    assert CcxtExchangeAdapter._ccxt_client_exchange_id("binance", "spot") == "binance"
    assert CcxtExchangeAdapter._ccxt_client_exchange_id("okx", "swap") == "okx"


def test_ccxt_history_fetch_paginates_beyond_exchange_batch_limit():
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=700)
    rows = [
        [
            int((start + timedelta(hours=index)).timestamp() * 1000),
            "100",
            "102",
            "99",
            "101",
            "10",
        ]
        for index in range(701)
    ]

    class Client:
        def __init__(self):
            self.calls = 0

        async def fetch_ohlcv(self, _symbol, *, timeframe, since, limit):
            self.calls += 1
            assert timeframe == "1h"
            available = [row for row in rows if row[0] >= since]
            return available[:limit]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()

        async def resolve(symbol):
            return f"{symbol}:USDT"

        adapter._resolve_symbol = resolve
        candles = await adapter.fetch_candles("BTC/USDT", "1h", 650)
        return adapter.client.calls, candles

    calls, candles = asyncio.run(scenario())

    assert calls >= 3
    assert len(candles) == 650
    assert candles == sorted(candles, key=lambda candle: candle.timestamp)


def test_ccxt_funding_history_is_paginated_and_normalized():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = [
        {
            "timestamp": int((start + timedelta(hours=8 * index)).timestamp() * 1000),
            "fundingRate": "0.0001",
            "markPrice": "50000",
        }
        for index in range(205)
    ]

    class Client:
        has = {"fetchFundingRateHistory": True}

        def __init__(self):
            self.calls = 0

        async def fetch_funding_rate_history(self, _symbol, *, since, limit):
            self.calls += 1
            available = [row for row in rows if row["timestamp"] >= since]
            return available[:limit]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.market_type = "swap"

        async def resolve(symbol):
            return f"{symbol}:USDT"

        adapter._resolve_symbol = resolve
        first, second = await asyncio.gather(
            adapter.fetch_funding_history(
                "BTC/USDT",
                start,
                start + timedelta(hours=8 * 204),
            ),
            adapter.fetch_funding_history(
                "BTC/USDT",
                start,
                start + timedelta(hours=8 * 204),
            ),
        )
        return adapter.client.calls, first, second

    calls, points, duplicate = asyncio.run(scenario())

    assert calls == 3
    assert len(points) == 205
    assert duplicate == points
    assert points[0].rate == Decimal("0.0001")
    assert points[-1].mark_price == Decimal("50000")


def test_ccxt_open_interest_history_uses_okx_currency_and_paginates():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = [
        {
            "timestamp": int((start + timedelta(hours=index)).timestamp() * 1000),
            "openInterestAmount": str(1000 + index),
            "openInterestValue": str(50_000_000 + index),
        }
        for index in range(205)
    ]

    class Client:
        has = {"fetchOpenInterestHistory": True}
        markets = {"BTC/USDT:USDT": {"base": "BTC"}}

        def __init__(self):
            self.calls = 0

        async def fetch_open_interest_history(
            self,
            symbol,
            *,
            timeframe,
            since,
            limit,
        ):
            self.calls += 1
            assert symbol == "BTC"
            assert timeframe == "1h"
            available = [row for row in rows if row["timestamp"] >= since]
            return available[:limit]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.exchange_id = "okx"
        adapter.market_type = "swap"

        async def resolve(_symbol):
            return "BTC/USDT:USDT"

        adapter._resolve_symbol = resolve
        first, second = await asyncio.gather(
            adapter.fetch_open_interest_history(
                "BTC/USDT",
                "4h",
                start,
                start + timedelta(hours=204),
            ),
            adapter.fetch_open_interest_history(
                "BTC/USDT",
                "4h",
                start,
                start + timedelta(hours=204),
            ),
        )
        return adapter.client.calls, first, second

    calls, points, duplicate = asyncio.run(scenario())

    assert calls == 3
    assert len(points) == 205
    assert duplicate == points
    assert points[0].open_interest_amount == Decimal("1000")
    assert points[-1].open_interest_value == Decimal("50000204")


def test_ccxt_open_interest_history_clamps_binance_retention_boundary():
    end = datetime(2026, 7, 30, tzinfo=UTC)
    requested_start = end - timedelta(days=90)
    observed_since: list[int] = []

    class Client:
        has = {"fetchOpenInterestHistory": True}
        markets = {"BTC/USDT:USDT": {"base": "BTC"}}

        async def fetch_open_interest_history(
            self,
            symbol,
            *,
            timeframe,
            since,
            limit,
        ):
            observed_since.append(since)
            assert symbol == "BTC/USDT:USDT"
            assert timeframe == "1h"
            assert limit == 100
            return []

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.exchange_id = "binance"
        adapter.market_type = "swap"

        async def resolve(_symbol):
            return "BTC/USDT:USDT"

        adapter._resolve_symbol = resolve
        return await adapter.fetch_open_interest_history(
            "BTC/USDT",
            "1h",
            requested_start,
            end,
        )

    points = asyncio.run(scenario())

    assert points == []
    assert observed_since == [int((end - timedelta(days=29)).timestamp() * 1000)]


def test_ccxt_positioning_history_paginates_bounded_causal_windows():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    rows = [
        {
            "timestamp": int((start + timedelta(hours=index)).timestamp() * 1000),
            "longShortRatio": "1.5",
            "info": {"longAccount": "0.6", "shortAccount": "0.4"},
        }
        for index in range(701)
    ]

    class Client:
        has = {"fetchLongShortRatioHistory": True}

        def __init__(self):
            self.calls: list[tuple[int, int]] = []

        async def fetch_long_short_ratio_history(
            self,
            _symbol,
            *,
            timeframe,
            since,
            limit,
            params,
        ):
            assert timeframe == "1h"
            assert limit == 500
            until = params["until"]
            self.calls.append((since, until))
            return [row for row in rows if since <= row["timestamp"] <= until][:limit]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.exchange_id = "test"
        adapter.market_type = "swap"

        async def resolve(_symbol):
            return "BTC/USDT:USDT"

        adapter._resolve_symbol = resolve
        points = await adapter.fetch_positioning_history(
            "BTC/USDT",
            "1h",
            start,
            start + timedelta(hours=700),
        )
        return adapter.client.calls, points

    calls, points = asyncio.run(scenario())

    assert len(calls) == 2
    assert all(until - since <= 499 * 60 * 60 * 1000 for since, until in calls)
    assert len(points) == 701
    assert points[0].long_short_ratio == Decimal("1.5")
    assert points[-1].long_account_fraction == Decimal("0.6")
    assert points[-1].timestamp == start + timedelta(hours=700)


def test_ccxt_positioning_history_excludes_future_rows():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)

    class Client:
        has = {"fetchLongShortRatioHistory": True}

        async def fetch_long_short_ratio_history(self, *_args, **_kwargs):
            return [
                {
                    "timestamp": int((start + timedelta(hours=index)).timestamp() * 1000),
                    "longShortRatio": "1.1",
                    "info": {"longAccount": "0.5238", "shortAccount": "0.4762"},
                }
                for index in range(4)
            ]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.exchange_id = "test"
        adapter.market_type = "swap"

        async def resolve(_symbol):
            return "BTC/USDT:USDT"

        adapter._resolve_symbol = resolve
        return await adapter.fetch_positioning_history(
            "BTC/USDT",
            "1h",
            start,
            end,
        )

    points = asyncio.run(scenario())

    assert [point.timestamp for point in points] == [
        start,
        start + timedelta(hours=1),
        end,
    ]


def test_ccxt_basis_history_synchronizes_mark_and_index_without_future_rows():
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = start + timedelta(hours=2)

    class Client:
        has = {"fetchMarkOHLCV": True, "fetchIndexOHLCV": True}

        @staticmethod
        async def fetch_mark_ohlcv(_symbol, *, timeframe, since, limit):
            assert timeframe == "1h"
            assert limit == 1000
            return [
                [
                    int((start + timedelta(hours=index)).timestamp() * 1000),
                    0,
                    0,
                    0,
                    str(101 + index),
                    0,
                ]
                for index in range(4)
                if int((start + timedelta(hours=index)).timestamp() * 1000) >= since
            ]

        @staticmethod
        async def fetch_index_ohlcv(_symbol, *, timeframe, since, limit):
            return [
                [
                    int((start + timedelta(hours=index)).timestamp() * 1000),
                    0,
                    0,
                    0,
                    str(100 + index),
                    0,
                ]
                for index in range(3)
                if int((start + timedelta(hours=index)).timestamp() * 1000) >= since
            ]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.market_type = "swap"

        async def resolve(_symbol):
            return "BTC/USDT:USDT"

        adapter._resolve_symbol = resolve
        return await adapter.fetch_basis_history(
            "BTC/USDT",
            "1h",
            start,
            end,
        )

    points = asyncio.run(scenario())

    assert len(points) == 3
    assert points[-1].timestamp == end
    assert points[0].basis_rate == Decimal("0.01")
    assert all(point.timestamp <= end for point in points)


def test_okx_derivative_price_history_uses_effective_page_limit_until_short_page():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        [
            int((start + timedelta(hours=index)).timestamp() * 1000),
            0,
            0,
            0,
            str(100 + index),
            0,
        ]
        for index in range(205)
    ]

    class Client:
        def __init__(self):
            self.calls = 0

        async def fetch_mark_ohlcv(self, _symbol, *, timeframe, since, limit):
            self.calls += 1
            assert timeframe == "1h"
            assert limit == 100
            return [row for row in rows if row[0] >= since][:limit]

    async def scenario():
        adapter = object.__new__(CcxtExchangeAdapter)
        adapter.client = Client()
        adapter.exchange_id = "okx"
        result = await adapter._fetch_derivative_price_ohlcv(
            adapter.client.fetch_mark_ohlcv,
            "BTC/USDT:USDT",
            "1h",
            start,
            start + timedelta(hours=204),
        )
        return adapter.client.calls, result

    calls, result = asyncio.run(scenario())

    assert calls == 3
    assert len(result) == 205
    assert result[-1][0] == rows[-1][0]
