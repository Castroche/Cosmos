from __future__ import annotations

from .api_contracts import (
    HumanLiveGrantIssueRequest,
    HumanLiveGrantPreviewRequest,
    TradingIntelligenceAutomationRequest,
)
from .dependencies import ServiceDependency
from .trading_intelligence import AutomationRuntimeV2, HumanLiveGrant


def register(app):

    @app.get("/api/trading-intelligence/automation", response_model=AutomationRuntimeV2)
    async def get_trading_intelligence_automation(*, services: ServiceDependency):
        return await services.get_trading_intelligence_automation()

    @app.post("/api/trading-intelligence/automation/commission")
    async def commission_trading_intelligence_automation(
        request: TradingIntelligenceAutomationRequest, *, services: ServiceDependency
    ):
        return await services.commission_trading_intelligence_automation(request)

    @app.post("/api/trading-intelligence/automation/start", response_model=AutomationRuntimeV2)
    async def start_trading_intelligence_automation(
        request: TradingIntelligenceAutomationRequest, *, services: ServiceDependency
    ):
        return await services.start_trading_intelligence_automation(request)

    @app.post("/api/trading-intelligence/automation/stop", response_model=AutomationRuntimeV2)
    async def stop_trading_intelligence_automation(*, services: ServiceDependency):
        return await services.stop_trading_intelligence_automation()

    @app.post("/api/trading-intelligence/live-grants/preview")
    async def preview_trading_intelligence_live_grant(
        request: HumanLiveGrantPreviewRequest, *, services: ServiceDependency
    ):
        return await services.preview_trading_intelligence_live_grant(request)

    @app.post("/api/trading-intelligence/live-grants", response_model=HumanLiveGrant)
    async def issue_trading_intelligence_live_grant(
        request: HumanLiveGrantIssueRequest, *, services: ServiceDependency
    ):
        return await services.issue_trading_intelligence_live_grant(request)

    @app.delete("/api/trading-intelligence/live-grants/{grant_id}", response_model=HumanLiveGrant)
    async def revoke_trading_intelligence_live_grant(grant_id: str, *, services: ServiceDependency):
        return await services.revoke_trading_intelligence_live_grant(grant_id)

    @app.api_route(
        "/api/{unregistered_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def unregistered_api_route(unregistered_path: str, *, services: ServiceDependency):
        return await services.unregistered_api_route(unregistered_path)
