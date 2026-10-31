from __future__ import annotations

from fastapi import WebSocket

from .dependencies import ServiceDependency
from .domain import LiquidationEvent


def register(app):

    @app.get("/api/dashboard")
    async def dashboard(*, services: ServiceDependency):
        return await services.dashboard()

    @app.get("/api/market")
    async def market_window(
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = 300,
        *,
        services: ServiceDependency,
    ):
        return await services.market_window(symbol, timeframe, limit)

    @app.get("/api/market-data/liquidations/status")
    async def liquidation_collection_status(*, services: ServiceDependency):
        return await services.liquidation_collection_status()

    @app.get("/api/market-data/liquidations", response_model=list[LiquidationEvent])
    async def liquidation_events(
        symbol: str | None = None, limit: int = 1000, *, services: ServiceDependency
    ):
        return await services.liquidation_events(symbol, limit)

    @app.websocket("/api/stream/market")
    async def stream_market(websocket: WebSocket):
        """Push the current exchange tick and forming candle to the browser."""
        services = websocket.app.state.services
        return await services.stream_market(websocket)
