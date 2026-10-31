from __future__ import annotations

from .api_contracts import TradingIntelligenceExecuteRequest
from .dependencies import ServiceDependency


def register(app):

    @app.post("/api/trading-intelligence/plans/{plan_id}/execute")
    async def execute_trading_intelligence_plan(
        plan_id: str, request: TradingIntelligenceExecuteRequest, *, services: ServiceDependency
    ):
        return await services.execute_trading_intelligence_plan(plan_id, request)
