from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal

import websockets

from .domain import Candle, MarketSnapshot


class PublicTickerStream:
    """Resilient public ticker stream for OKX and Binance."""

    def __init__(
        self,
        exchange_id: str,
        symbol: str,
        market_type: str,
        callback,
        proxy_url: str = "",
        *,
        timeframe: str | None = None,
        timeframes: list[str] | None = None,
        candle_callback=None,
    ):
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.market_type = market_type
        self.callback = callback
        self.proxy_url = proxy_url
        self._task: asyncio.Task | None = None
        self._candle_task: asyncio.Task | None = None
        self._closed = False
        selected_timeframes = timeframes or ([timeframe] if timeframe else [])
        self.timeframes = tuple(dict.fromkeys(selected_timeframes))
        self.candle_callback = candle_callback

    def start(self) -> None:
        if not self._task or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._run(), name="cosmos-public-ticker")
        if (
            self.timeframes
            and self.candle_callback
            and (not self._candle_task or self._candle_task.done())
        ):
            self._candle_task = asyncio.create_task(
                self._run_candles(), name="cosmos-public-candles"
            )

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        if self._candle_task:
            self._candle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._candle_task
        self._candle_task = None

    async def _run(self) -> None:
        delay = 1
        while not self._closed:
            try:
                if self.exchange_id == "okx":
                    await self._okx()
                elif self.exchange_id == "binance":
                    await self._binance()
                else:
                    raise ValueError(f"unsupported websocket exchange: {self.exchange_id}")
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _run_candles(self) -> None:
        delay = 1
        while not self._closed:
            try:
                if self.exchange_id == "okx":
                    await self._okx_candles()
                elif self.exchange_id == "binance":
                    await self._binance_candles()
                else:
                    raise ValueError(f"unsupported websocket exchange: {self.exchange_id}")
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _okx(self) -> None:
        instrument = self.symbol.replace("/", "-").split(":")[0]
        if self.market_type == "swap" and not instrument.endswith("-SWAP"):
            instrument += "-SWAP"
        url = "wss://ws.okx.com:8443/ws/v5/public"
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=10, proxy=self.proxy_url or True
        ) as socket:
            await socket.send(
                json.dumps(
                    {"op": "subscribe", "args": [{"channel": "tickers", "instId": instrument}]}
                )
            )
            async for raw in socket:
                snapshot = self.parse_okx(raw, self.symbol)
                if snapshot:
                    self.callback(snapshot)

    async def _binance(self) -> None:
        stream_symbol = self.symbol.replace("/", "").split(":")[0].lower()
        host = (
            "wss://fstream.binance.com/ws"
            if self.market_type == "swap"
            else "wss://stream.binance.com:9443/ws"
        )
        async with websockets.connect(
            f"{host}/{stream_symbol}@bookTicker",
            ping_interval=20,
            ping_timeout=10,
            proxy=self.proxy_url or True,
        ) as socket:
            async for raw in socket:
                snapshot = self.parse_binance(raw, self.symbol)
                if snapshot:
                    self.callback(snapshot)

    async def _okx_candles(self) -> None:
        assert self.timeframes and self.candle_callback
        instrument = self.symbol.replace("/", "-").split(":")[0]
        if self.market_type == "swap" and not instrument.endswith("-SWAP"):
            instrument += "-SWAP"
        url = "wss://ws.okx.com:8443/ws/v5/business"
        async with websockets.connect(
            url, ping_interval=20, ping_timeout=10, proxy=self.proxy_url or True
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "op": "subscribe",
                        "args": [
                            {
                                "channel": f"candle{self._okx_bar(timeframe)}",
                                "instId": instrument,
                            }
                            for timeframe in self.timeframes
                        ],
                    }
                )
            )
            async for raw in socket:
                parsed = self.parse_okx_candle(raw)
                if parsed:
                    candle, confirmed = parsed
                    message = json.loads(raw)
                    timeframe = self._timeframe_from_okx_channel(message["arg"]["channel"])
                    self.candle_callback(self.symbol, timeframe, candle, confirmed)

    async def _binance_candles(self) -> None:
        assert self.timeframes and self.candle_callback
        await asyncio.gather(*(self._binance_candle(timeframe) for timeframe in self.timeframes))

    async def _binance_candle(self, timeframe: str) -> None:
        assert self.candle_callback
        stream_symbol = self.symbol.replace("/", "").split(":")[0].lower()
        host = (
            "wss://fstream.binance.com/ws"
            if self.market_type == "swap"
            else "wss://stream.binance.com:9443/ws"
        )
        async with websockets.connect(
            f"{host}/{stream_symbol}@kline_{timeframe}",
            ping_interval=20,
            ping_timeout=10,
            proxy=self.proxy_url or True,
        ) as socket:
            async for raw in socket:
                parsed = self.parse_binance_candle(raw)
                if parsed:
                    candle, confirmed = parsed
                    self.candle_callback(self.symbol, timeframe, candle, confirmed)

    @staticmethod
    def _okx_bar(timeframe: str) -> str:
        return timeframe.upper() if timeframe.endswith(("h", "d", "w")) else timeframe

    @staticmethod
    def _timeframe_from_okx_channel(channel: str) -> str:
        bar = channel.removeprefix("candle")
        return bar.lower() if bar.endswith(("H", "D")) else bar

    @staticmethod
    def parse_okx(raw: str, symbol: str) -> MarketSnapshot | None:
        message = json.loads(raw)
        if not message.get("data"):
            return None
        item = message["data"][0]
        return MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.fromtimestamp(int(item["ts"]) / 1000, tz=UTC),
            bid=Decimal(item["bidPx"]),
            ask=Decimal(item["askPx"]),
            last=Decimal(item["last"]),
            source="okx:websocket",
        )

    @staticmethod
    def parse_binance(raw: str, symbol: str) -> MarketSnapshot | None:
        item = json.loads(raw)
        if "b" not in item or "a" not in item:
            return None
        timestamp = int(item.get("E") or item.get("T") or 0)
        bid = Decimal(item["b"])
        ask = Decimal(item["a"])
        return MarketSnapshot(
            symbol=symbol,
            timestamp=(
                datetime.fromtimestamp(timestamp / 1000, tz=UTC) if timestamp else datetime.now(UTC)
            ),
            bid=bid,
            ask=ask,
            last=(bid + ask) / Decimal("2"),
            source="binance:websocket",
        )

    @staticmethod
    def parse_okx_candle(raw: str) -> tuple[Candle, bool] | None:
        message = json.loads(raw)
        channel = message.get("arg", {}).get("channel", "")
        if not channel.startswith("candle") or not message.get("data"):
            return None
        item = message["data"][0]
        return (
            Candle(
                timestamp=datetime.fromtimestamp(int(item[0]) / 1000, tz=UTC),
                open=Decimal(item[1]),
                high=Decimal(item[2]),
                low=Decimal(item[3]),
                close=Decimal(item[4]),
                volume=Decimal(item[5]),
            ),
            item[8] == "1",
        )

    @staticmethod
    def parse_binance_candle(raw: str) -> tuple[Candle, bool] | None:
        message = json.loads(raw)
        item = message.get("k")
        if not item:
            return None
        return (
            Candle(
                timestamp=datetime.fromtimestamp(int(item["t"]) / 1000, tz=UTC),
                open=Decimal(item["o"]),
                high=Decimal(item["h"]),
                low=Decimal(item["l"]),
                close=Decimal(item["c"]),
                volume=Decimal(item["v"]),
            ),
            bool(item["x"]),
        )
