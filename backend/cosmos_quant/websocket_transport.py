"""Bounded WebSocket connections with idempotent HTTP proxy EOF handling."""
from __future__ import annotations

from websockets.asyncio import client


class _HTTPProxyConnection(client.HTTPProxyConnection):
    """Guard the websockets 15/16 proxy parser against repeated terminal callbacks.

    Windows may deliver eof_received followed by connection_lost after a failed
    CONNECT. Re-entering the completed parser returns None instead of a Response.
    Keep the original proxy, authentication, TLS and error handling intact.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # TLS setup can fail before connect_http_proxy awaits this future.
        # Retrieving its exception prevents an orphan warning; await still raises it.
        self.response.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )

    def run_parser(self) -> None:
        if not self.response.done():
            super().run_parser()

    def eof_received(self) -> None:
        if not self.reader.eof:
            self.reader.feed_eof()
        self.run_parser()

    def connection_lost(self, exc: Exception | None) -> None:
        if self.response.done():
            return
        if exc is not None:
            self.response.set_exception(exc)
        else:
            self.eof_received()


# connect_http_proxy resolves this protocol class at call time. Keep this shim
# local to the application; remove it once the supported dependency fixes EOF.
client.HTTPProxyConnection = _HTTPProxyConnection


def connect(uri: str, **kwargs):
    kwargs.setdefault("open_timeout", 10)
    kwargs.setdefault("close_timeout", 5)
    return client.connect(uri, **kwargs)
