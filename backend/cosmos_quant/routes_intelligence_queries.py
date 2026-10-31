from __future__ import annotations

from fastapi import Query

from .dependencies import ServiceDependency
from .trade_history import TradeHistoryResponseV2
from .trading_intelligence import MarketStateV2


def register(app):

    @app.get("/api/trading-intelligence/official-events")
    async def trading_intelligence_official_events(*, services: ServiceDependency):
        return await services.trading_intelligence_official_events()

    @app.get("/api/trading-intelligence/migration-summary")
    async def trading_intelligence_migration_summary(*, services: ServiceDependency):
        return await services.trading_intelligence_migration_summary()

    @app.get("/api/trading-intelligence/quant-models")
    async def trading_intelligence_quant_models(*, services: ServiceDependency):
        return await services.trading_intelligence_quant_models()

    @app.post("/api/trading-intelligence/official-events/refresh")
    async def refresh_trading_intelligence_official_events(*, services: ServiceDependency):
        return await services.refresh_trading_intelligence_official_events()

    @app.get("/api/trading-intelligence/overview")
    async def trading_intelligence_overview(*, services: ServiceDependency):
        return await services.trading_intelligence_overview()

    @app.get("/api/trading-intelligence/learning")
    async def trading_intelligence_learning(*, services: ServiceDependency):
        return await services.trading_intelligence_learning()

    @app.get("/api/trading-intelligence/market-state/{symbol:path}")
    async def trading_intelligence_market_state(symbol: str, *, services: ServiceDependency):
        return await services.trading_intelligence_market_state(symbol)

    @app.post("/api/trading-intelligence/market-state", response_model=MarketStateV2)
    async def ingest_trading_intelligence_market_state(
        state: MarketStateV2, *, services: ServiceDependency
    ):
        return await services.ingest_trading_intelligence_market_state(state)

    @app.post(
        "/api/trading-intelligence/market-state/{symbol:path}/refresh", response_model=MarketStateV2
    )
    async def refresh_trading_intelligence_market_state(
        symbol: str, *, services: ServiceDependency
    ):
        return await services.refresh_trading_intelligence_market_state(symbol)

    @app.get("/api/trading-intelligence/episodes")
    async def trading_intelligence_episodes(
        symbol: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        *,
        services: ServiceDependency,
    ):
        return await services.trading_intelligence_episodes(symbol, limit)

    @app.get("/api/trading-intelligence/events")
    async def trading_intelligence_events(
        aggregate_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        *,
        services: ServiceDependency,
    ):
        return await services.trading_intelligence_events(aggregate_id, limit)

    @app.get("/api/trading-intelligence/attribution")
    async def trading_intelligence_attribution(*, services: ServiceDependency):
        return await services.trading_intelligence_attribution()

    @app.get("/api/trading-intelligence/trade-history", response_model=TradeHistoryResponseV2)
    async def trading_intelligence_trade_history(
        symbol: str | None = None,
        days: int = Query(default=30, ge=0, le=3650),
        limit: int = Query(default=500, ge=1, le=2000),
        *,
        services: ServiceDependency,
    ):
        return await services.trading_intelligence_trade_history(symbol, days, limit)

    @app.get("/api/trading-intelligence/artifacts/{artifact_id}")
    async def trading_intelligence_artifact(artifact_id: str, *, services: ServiceDependency):
        return await services.trading_intelligence_artifact(artifact_id)
