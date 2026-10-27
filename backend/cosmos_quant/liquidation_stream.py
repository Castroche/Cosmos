from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from . import websocket_transport as websockets
from .domain import LiquidationEvent, OrderSide, Side
from .market_data_capture import CaptureSessionStatus, MarketDataCaptureSession
from .storage import SqliteStore

BINANCE_ALL_LIQUIDATIONS_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
BINANCE_LIQUIDATION_STREAM = "!forceOrder@arr"
BINANCE_LIQUIDATION_SAMPLING = "latest_per_symbol_within_1000ms"


def parse_binance_liquidation(
    raw: str | bytes | dict,
    *,
    allowed_symbols: set[str] | None = None,
) -> LiquidationEvent | None:
    message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if "data" in message and isinstance(message["data"], dict):
        message = message["data"]
    if message.get("e") != "forceOrder" or not isinstance(message.get("o"), dict):
        return None
    order = message["o"]
    raw_symbol = str(order.get("s") or "")
    if not raw_symbol:
        return None
    symbol = (
        f"{raw_symbol.removesuffix('USDT')}/USDT" if raw_symbol.endswith("USDT") else raw_symbol
    )
    if allowed_symbols and symbol not in allowed_symbols:
        return None
    if int(message.get("st", 1)) != 1:
        return None
    order_side = OrderSide(str(order["S"]).lower())
    liquidated_side = Side.LONG if order_side == OrderSide.SELL else Side.SHORT
    event_ms = int(message["E"])
    trade_ms = int(order["T"])
    original_quantity = Decimal(str(order["q"]))
    last_filled = Decimal(str(order["l"]))
    accumulated = Decimal(str(order["z"]))
    price = Decimal(str(order["p"]))
    average_price = Decimal(str(order["ap"]))
    identity = "|".join(
        (
            raw_symbol,
            str(trade_ms),
            str(order_side),
            str(original_quantity),
            str(average_price),
            str(accumulated),
        )
    )
    return LiquidationEvent(
        id=f"liq_{sha256(identity.encode()).hexdigest()[:24]}",
        exchange="binance",
        symbol=symbol,
        event_time=datetime.fromtimestamp(event_ms / 1000, tz=UTC),
        trade_time=datetime.fromtimestamp(trade_ms / 1000, tz=UTC),
        order_side=order_side,
        liquidated_side=liquidated_side,
        original_quantity=original_quantity,
        last_filled_quantity=last_filled,
        accumulated_quantity=accumulated,
        price=price,
        average_price=average_price,
        quote_value=average_price * accumulated,
        order_status=str(order["X"]),
    )


class BinanceLiquidationCollector:
    """Reconnectable live collector; it deliberately makes no history claim."""

    def __init__(
        self,
        store: SqliteStore,
        *,
        symbols: list[str],
        proxy_url: str = "",
        heartbeat_interval_seconds: float = 30,
    ):
        self.store = store
        self.symbols = set(symbols)
        self.proxy_url = proxy_url
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.started_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.event_count = 0
        self.reconnect_count = 0
        self.last_error = ""
        self.last_heartbeat_at: datetime | None = None
        self.active_capture_id: str | None = None
        self.interrupted_session_count = 0
        self._closed = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.started_at = datetime.now(UTC)
        self._closed = False
        self.interrupted_session_count = self.store.interrupt_open_market_data_capture_sessions(
            source="binance",
            stream=BINANCE_LIQUIDATION_STREAM,
            reason="collector process restarted before the prior connection was finalized",
        )
        self._task = asyncio.create_task(
            self._run(),
            name="binance-liquidation-collector",
        )

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def status(self) -> dict:
        return {
            "source": "binance",
            "stream": BINANCE_LIQUIDATION_STREAM,
            "sampling": BINANCE_LIQUIDATION_SAMPLING,
            "historical_backfill": False,
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "active_capture_id": self.active_capture_id,
            "interrupted_session_count": self.interrupted_session_count,
            "event_count": self.event_count,
            "reconnect_count": self.reconnect_count,
            "last_error": self.last_error,
            "running": bool(self._task and not self._task.done()),
        }

    async def _heartbeat(self, capture_id: str) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            heartbeat_at = datetime.now(UTC)
            updated = await asyncio.to_thread(
                self.store.heartbeat_market_data_capture_session,
                capture_id,
                heartbeat_at,
            )
            if not updated:
                return
            self.last_heartbeat_at = heartbeat_at

    async def _run(self) -> None:
        delay = 1
        while not self._closed:
            capture_id: str | None = None
            heartbeat_task: asyncio.Task | None = None
            connection_error = ""
            try:
                async with websockets.connect(
                    BINANCE_ALL_LIQUIDATIONS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    proxy=self.proxy_url or True,
                ) as socket:
                    delay = 1
                    connected_at = datetime.now(UTC)
                    capture_id = f"capture_{uuid4().hex[:24]}"
                    session = MarketDataCaptureSession(
                        id=capture_id,
                        source="binance",
                        stream=BINANCE_LIQUIDATION_STREAM,
                        symbols=sorted(self.symbols),
                        sampling=BINANCE_LIQUIDATION_SAMPLING,
                        started_at=connected_at,
                        last_heartbeat_at=connected_at,
                    )
                    await asyncio.to_thread(
                        self.store.start_market_data_capture_session,
                        session,
                    )
                    self.active_capture_id = capture_id
                    self.last_heartbeat_at = connected_at
                    heartbeat_task = asyncio.create_task(
                        self._heartbeat(capture_id),
                        name=f"liquidation-heartbeat-{capture_id}",
                    )
                    async for raw in socket:
                        event = parse_binance_liquidation(
                            raw,
                            allowed_symbols=self.symbols,
                        )
                        if event is None:
                            continue
                        await asyncio.to_thread(
                            self.store.save_liquidation_event,
                            event,
                        )
                        self.last_event_at = event.event_time
                        self.event_count += 1
                    if not self._closed:
                        connection_error = "websocket stream ended without an exception"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                connection_error = str(exc)[:500]
                self.last_error = connection_error
                self.reconnect_count += 1
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                if capture_id is not None:
                    ended_at = datetime.now(UTC)
                    await asyncio.to_thread(
                        self.store.finish_market_data_capture_session,
                        capture_id,
                        status=str(
                            CaptureSessionStatus.CLOSED
                            if self._closed
                            else CaptureSessionStatus.DISCONNECTED
                        ),
                        ended_at=ended_at,
                        error=connection_error,
                    )
                    self.active_capture_id = None
                    self.last_heartbeat_at = ended_at
            if not self._closed:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
