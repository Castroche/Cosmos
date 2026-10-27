from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from time import time

from pydantic import BaseModel

from . import websocket_transport as websockets
from .domain import OrderStatus
from .order_ledger import PrivateOrderEvent


class PrivateStreamStatus(BaseModel):
    connected: bool = False
    authenticated: bool = False
    reconnect_count: int = 0
    last_event_at: datetime | None = None
    last_error: str = ""


def okx_login_payload(
    api_key: str,
    secret: str,
    passphrase: str,
    timestamp: str,
) -> dict:
    message = f"{timestamp}GET/users/self/verify".encode()
    signature = base64.b64encode(
        hmac.new(secret.encode(), message, hashlib.sha256).digest()
    ).decode()
    return {
        "op": "login",
        "args": [
            {
                "apiKey": api_key,
                "passphrase": passphrase,
                "timestamp": timestamp,
                "sign": signature,
            }
        ],
    }


def parse_okx_order_event(
    item: dict,
    quantity_converter: Callable[[str, Decimal], Decimal],
) -> PrivateOrderEvent | None:
    status_map = {
        "live": OrderStatus.OPEN,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "mmp_canceled": OrderStatus.CANCELED,
    }
    status = status_map.get(str(item.get("state", "")))
    if not status or not item.get("ordId"):
        return None
    instrument = str(item.get("instId", ""))
    filled_raw = item.get("accFillSz")
    filled = (
        quantity_converter(instrument, Decimal(str(filled_raw)))
        if filled_raw not in (None, "")
        else None
    )
    update_ms = item.get("uTime") or item.get("fillTime")
    updated_at = (
        datetime.fromtimestamp(int(update_ms) / 1000, tz=UTC) if update_ms else datetime.now(UTC)
    )
    average = item.get("avgPx")
    fee = item.get("fee")
    return PrivateOrderEvent(
        order_id=str(item["ordId"]),
        client_order_id=str(item.get("clOrdId") or ""),
        status=status,
        filled_quantity=filled,
        average_fill_price=(Decimal(str(average)) if average not in (None, "") else None),
        fee=abs(Decimal(str(fee))) if fee not in (None, "") else None,
        updated_at=updated_at,
    )


class OkxPrivateOrderStream:
    def __init__(
        self,
        credentials: dict[str, str],
        sandbox: bool,
        quantity_converter: Callable[[str, Decimal], Decimal],
        callback: Callable[[PrivateOrderEvent], None],
        proxy_url: str = "",
    ):
        self.credentials = credentials
        self.sandbox = sandbox
        self.quantity_converter = quantity_converter
        self.callback = callback
        self.proxy_url = proxy_url
        self.status = PrivateStreamStatus()
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        if not self._task or self._task.done():
            self._closed = False
            self._task = asyncio.create_task(self._run(), name="okx-private-orders")

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.status.connected = False
        self.status.authenticated = False

    async def _run(self) -> None:
        delay = 1
        while not self._closed:
            try:
                await self._connect_once()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.connected = False
                self.status.authenticated = False
                self.status.last_error = str(exc)[:300]
                self.status.reconnect_count += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _connect_once(self) -> None:
        url = (
            "wss://wspap.okx.com:8443/ws/v5/private"
            if self.sandbox
            else "wss://ws.okx.com:8443/ws/v5/private"
        )
        async with websockets.connect(
            url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            proxy=self.proxy_url or True,
        ) as socket:
            self.status.connected = True
            timestamp = str(int(time()))
            await socket.send(
                json.dumps(
                    okx_login_payload(
                        self.credentials["api_key"],
                        self.credentials["secret"],
                        self.credentials["passphrase"],
                        timestamp,
                    )
                )
            )
            login = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            if login.get("event") != "login" or login.get("code") != "0":
                raise RuntimeError(f"OKX 私有 WebSocket 登录失败：{login.get('msg', login)}")
            self.status.authenticated = True
            self.status.last_error = ""
            await socket.send(
                json.dumps(
                    {
                        "id": "cosmos-orders",
                        "op": "subscribe",
                        "args": [{"channel": "orders", "instType": "ANY"}],
                    }
                )
            )
            while not self._closed:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=25)
                except TimeoutError:
                    await socket.send("ping")
                    continue
                if raw == "pong":
                    continue
                payload = json.loads(raw)
                for item in payload.get("data") or []:
                    event = parse_okx_order_event(item, self.quantity_converter)
                    if event:
                        self.callback(event)
                        self.status.last_event_at = datetime.now(UTC)
