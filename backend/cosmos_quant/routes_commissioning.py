from __future__ import annotations

from .api_contracts import CommissioningRequest, MultiLegCommissioningRequest
from .configuration import CommissioningResult
from .dependencies import ServiceDependency
from .multi_leg_execution import MultiLegCommissioningResult


def register(app):

    @app.post("/api/settings/commission-multi-leg", response_model=MultiLegCommissioningResult)
    async def commission_multi_leg(
        request: MultiLegCommissioningRequest, *, services: ServiceDependency
    ):
        return await services.commission_multi_leg(request)

    @app.get("/api/settings/commission-multi-leg", response_model=list[MultiLegCommissioningResult])
    async def list_multi_leg_commissioning(*, services: ServiceDependency):
        return await services.list_multi_leg_commissioning()

    @app.post("/api/settings/commission-protection", response_model=CommissioningResult)
    async def commission_protection(request: CommissioningRequest, *, services: ServiceDependency):
        return await services.commission_protection(request)

    @app.post(
        "/api/settings/commission-agent-position-management", response_model=CommissioningResult
    )
    async def commission_agent_position_management(
        request: CommissioningRequest, *, services: ServiceDependency
    ):
        return await services.commission_agent_position_management(request)
