from __future__ import annotations

from .api_contracts import TradingIntelligenceDecisionRequest
from .dependencies import ServiceDependency


def register(app):

    @app.post("/api/trading-intelligence/decide")
    async def invoke_trading_intelligence(
        request: TradingIntelligenceDecisionRequest, *, services: ServiceDependency
    ):
        return await services.invoke_trading_intelligence(request)
