from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel

from .domain import Candle, MarketSnapshot
from .exchanges.base import ExchangeAdapter
from .storage import SqliteStore
from .timeframes import TIMEFRAME_SECONDS


class MarketQuality(BaseModel):
    status: str
    source: str
    is_real: bool
    is_cached: bool
    candle_count: int
    gap_count: int
    duplicate_count: int
    invalid_count: int
    last_candle_at: datetime
    last_tick_at: datetime
    message: str


class MarketDataError(RuntimeError):
    pass


def validate_candles(candles: list[Candle], timeframe: str) -> tuple[list[Candle], MarketQuality]:
    if timeframe not in TIMEFRAME_SECONDS:
        raise MarketDataError(f"unsupported timeframe: {timeframe}")
    ordered = sorted(candles, key=lambda candle: candle.timestamp)
    unique: dict[datetime, Candle] = {}
    duplicates = 0
    invalid = 0
    for candle in ordered:
        if candle.timestamp in unique:
            duplicates += 1
        if (
            candle.high < max(candle.open, candle.close)
            or candle.low > min(candle.open, candle.close)
            or candle.low > candle.high
            or candle.volume < 0
        ):
            invalid += 1
            continue
        unique[candle.timestamp] = candle
    valid = list(unique.values())
    if len(valid) < 2:
        raise MarketDataError("not enough valid candles")
    expected = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    gaps = sum(
        1
        for previous, current in zip(valid, valid[1:], strict=False)
        if current.timestamp - previous.timestamp > expected * 1.5
    )
    now = datetime.now(UTC)
    placeholder = MarketQuality(
        status="healthy" if not gaps and not invalid else "degraded",
        source="exchange",
        is_real=True,
        is_cached=False,
        candle_count=len(valid),
        gap_count=gaps,
        duplicate_count=duplicates,
        invalid_count=invalid,
        last_candle_at=valid[-1].timestamp,
        last_tick_at=now,
        message="行情数据完整" if not gaps and not invalid else "行情存在缺口或非法 K 线",
    )
    return valid, placeholder


class MarketDataService:
    def __init__(self, store: SqliteStore):
        self.store = store
        self.quality: MarketQuality | None = None
        self.websocket_snapshot: MarketSnapshot | None = None
        self._websocket_snapshots: dict[str, MarketSnapshot] = {}
        self._live_candles: dict[tuple[str, str], list[Candle]] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._fetch_inflight: dict[
            tuple[str, str, str, int],
            asyncio.Task,
        ] = {}

    def update_websocket(self, snapshot: MarketSnapshot) -> None:
        self.websocket_snapshot = snapshot
        self._websocket_snapshots[snapshot.symbol] = snapshot
        changed: dict[str, Candle] = {}
        for (symbol, timeframe), candles in self._live_candles.items():
            if symbol != snapshot.symbol or not candles:
                continue
            candle = self._apply_tick(candles, timeframe, snapshot)
            changed[timeframe] = candle
        self._publish(
            {
                "type": "market",
                "market": snapshot.model_dump(mode="json"),
                "candles": {
                    timeframe: candle.model_dump(mode="json")
                    for timeframe, candle in changed.items()
                },
                "server_time": datetime.now(UTC).isoformat(),
            }
        )

    def websocket_snapshot_for(self, symbol: str) -> MarketSnapshot | None:
        """Return the newest ticker without requiring a candle timeframe cache."""
        return self._websocket_snapshots.get(symbol) or (
            self.websocket_snapshot
            if self.websocket_snapshot and self.websocket_snapshot.symbol == symbol
            else None
        )

    def update_exchange_candle(
        self,
        symbol: str,
        timeframe: str,
        candle: Candle,
        confirmed: bool,
    ) -> None:
        candles = self._live_candles.get((symbol, timeframe))
        if not candles:
            return
        last_timestamp = candles[-1].timestamp
        if last_timestamp.tzinfo is None:
            last_timestamp = last_timestamp.replace(tzinfo=UTC)
        candle_timestamp = candle.timestamp
        if candle_timestamp.tzinfo is None:
            candle_timestamp = candle_timestamp.replace(tzinfo=UTC)
        if candle_timestamp == last_timestamp:
            candles[-1] = candle
        elif candle_timestamp > last_timestamp:
            candles.append(candle)
            del candles[:-500]
        else:
            return
        snapshot = self._websocket_snapshots.get(symbol)
        if (
            snapshot is None
            and self.websocket_snapshot
            and self.websocket_snapshot.symbol == symbol
        ):
            snapshot = self.websocket_snapshot
        if not snapshot:
            return
        self._publish(
            {
                "type": "market",
                "market": snapshot.model_dump(mode="json"),
                "candles": {timeframe: candle.model_dump(mode="json")},
                "candle_confirmed": confirmed,
                "server_time": datetime.now(UTC).isoformat(),
            }
        )

    def _publish(self, event: dict) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(event)

    @staticmethod
    def _apply_tick(candles: list[Candle], timeframe: str, snapshot: MarketSnapshot) -> Candle:
        seconds = TIMEFRAME_SECONDS[timeframe]
        timestamp = snapshot.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        bucket = datetime.fromtimestamp(int(timestamp.timestamp()) // seconds * seconds, tz=UTC)
        last = candles[-1]
        last_timestamp = last.timestamp
        if last_timestamp.tzinfo is None:
            last_timestamp = last_timestamp.replace(tzinfo=UTC)
        if bucket > last_timestamp:
            candle = Candle(
                timestamp=bucket,
                open=last.close,
                high=max(last.close, snapshot.last),
                low=min(last.close, snapshot.last),
                close=snapshot.last,
                volume=Decimal("0"),
            )
            candles.append(candle)
            del candles[:-500]
            return candle
        if bucket == last_timestamp:
            candle = last.model_copy(
                update={
                    "high": max(last.high, snapshot.last),
                    "low": min(last.low, snapshot.last),
                    "close": snapshot.last,
                }
            )
            candles[-1] = candle
            return candle
        return last

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def stream_event(self, symbol: str, timeframe: str) -> dict | None:
        snapshot = self._websocket_snapshots.get(symbol)
        if (
            snapshot is None
            and self.websocket_snapshot
            and self.websocket_snapshot.symbol == symbol
        ):
            snapshot = self.websocket_snapshot
        candles = self._live_candles.get((symbol, timeframe))
        if not snapshot or not candles:
            return None
        return {
            "type": "market",
            "market": snapshot.model_dump(mode="json"),
            "candles": {timeframe: candles[-1].model_dump(mode="json")},
            "server_time": datetime.now(UTC).isoformat(),
        }

    async def fetch(
        self,
        adapter: ExchangeAdapter,
        exchange_id: str,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> tuple[MarketSnapshot, list[Candle], MarketQuality]:
        key = (exchange_id, symbol, timeframe, limit)
        task = self._fetch_inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                self._fetch_once(
                    adapter,
                    exchange_id,
                    symbol,
                    timeframe,
                    limit,
                ),
                name=(f"market-fetch:{exchange_id}:{symbol}:{timeframe}:{limit}"),
            )
            self._fetch_inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._fetch_inflight.get(key) is task and task.done():
                self._fetch_inflight.pop(key, None)

    async def _fetch_once(
        self,
        adapter: ExchangeAdapter,
        exchange_id: str,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> tuple[MarketSnapshot, list[Candle], MarketQuality]:
        try:
            cached_rows = await asyncio.to_thread(
                self.store.load_candles, exchange_id, symbol, timeframe, limit
            )
            rest_snapshot = await adapter.market_snapshot(symbol)
            remote_limit = min(limit, 300) if len(cached_rows) >= limit else limit
            raw_candles = await adapter.fetch_candles(symbol, timeframe, remote_limit)
            if cached_rows:
                cached_candles = [
                    Candle(
                        timestamp=datetime.fromisoformat(row["timestamp"]),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row["volume"]),
                    )
                    for row in cached_rows
                ]
                merged = {candle.timestamp: candle for candle in [*cached_candles, *raw_candles]}
                raw_candles = [merged[key] for key in sorted(merged)][-limit:]
            candles, quality = validate_candles(raw_candles, timeframe)
            self._live_candles[(symbol, timeframe)] = candles
            websocket = self._websocket_snapshots.get(symbol)
            if (
                websocket is None
                and self.websocket_snapshot
                and self.websocket_snapshot.symbol == symbol
            ):
                websocket = self.websocket_snapshot
            if websocket:
                self._apply_tick(candles, timeframe, websocket)
            # Ticks replace/append live bars while durable I/O runs. Persist a
            # stable list snapshot, then use the freshest ticker on return.
            await asyncio.to_thread(
                self.store.save_candles, exchange_id, symbol, timeframe, tuple(candles)
            )
            websocket = self.websocket_snapshot_for(symbol)
            if websocket:
                market_time = websocket.timestamp
                if market_time.tzinfo is None:
                    market_time = market_time.replace(tzinfo=UTC)
                if datetime.now(UTC) - market_time <= timedelta(seconds=15):
                    rest_snapshot = websocket
                    quality.source = f"{exchange_id}:websocket"
                    quality.last_tick_at = websocket.timestamp
            if quality.source == "exchange":
                quality.source = f"{exchange_id}:rest"
                quality.last_tick_at = rest_snapshot.timestamp
            self.quality = quality
            return rest_snapshot, candles, quality
        except Exception as exc:
            cached_rows = await asyncio.to_thread(
                self.store.load_candles, exchange_id, symbol, timeframe, limit
            )
            if len(cached_rows) < 57:
                raise MarketDataError(str(exc)) from exc
            candles = [
                Candle(
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row["volume"]),
                )
                for row in cached_rows
            ]
            candles, quality = validate_candles(candles, timeframe)
            self._live_candles[(symbol, timeframe)] = candles
            last = candles[-1]
            quality.status = "offline"
            quality.source = f"{exchange_id}:verified-cache"
            quality.is_cached = True
            quality.last_tick_at = last.timestamp
            quality.message = f"交易所连接失败，显示已验证的真实缓存：{exc}"
            snapshot = MarketSnapshot(
                symbol=symbol,
                timestamp=last.timestamp,
                bid=last.close,
                ask=last.close,
                last=last.close,
                source=quality.source,
            )
            self.quality = quality
            return snapshot, candles, quality
