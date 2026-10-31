from __future__ import annotations

from fastapi import Request

from .api_contracts import GenerateRequest, HaltRequest, ResumeRequest
from .backtest import BacktestResult
from .dependencies import ServiceDependency
from .domain import Order, StrategyPlan
from .risk import RiskDecision
from .risk_state import RiskControlState


def register(app):

    @app.middleware("http")
    async def require_local_origin(request: Request, call_next):
        services = request.app.state.services
        return await services.require_local_origin(request, call_next)

    @app.post("/api/strategies/generate", response_model=StrategyPlan)
    async def generate_strategy(request: GenerateRequest, *, services: ServiceDependency):
        return await services.generate_strategy(request)

    @app.post("/api/orders/{order_id}/cancel", response_model=Order)
    async def cancel_order(order_id: str, *, services: ServiceDependency):
        """Cancel a tracked working order without changing any open position."""
        return await services.cancel_order(order_id)

    @app.post("/api/strategies/{plan_id}/backtest", response_model=BacktestResult)
    async def backtest_strategy(
        plan_id: str, research_trial_count: int = 1, *, services: ServiceDependency
    ):
        return await services.backtest_strategy(plan_id, research_trial_count)

    @app.get("/api/strategies/{plan_id}/backtest", response_model=BacktestResult | None)
    async def latest_strategy_backtest(plan_id: str, *, services: ServiceDependency):
        return await services.latest_strategy_backtest(plan_id)

    @app.get("/api/strategies/{plan_id}/market-fit")
    async def strategy_market_fit(plan_id: str, *, services: ServiceDependency):
        """Explain whether a fixed strategy still fits the latest closed market data."""
        return await services.strategy_market_fit(plan_id)

    @app.get("/api/risk", response_model=RiskDecision)
    async def current_risk(*, services: ServiceDependency):
        return await services.current_risk()

    @app.get("/api/risk/state", response_model=RiskControlState)
    async def get_risk_state(*, services: ServiceDependency):
        return await services.get_risk_state()

    @app.post("/api/risk/halt", response_model=RiskControlState)
    async def halt_trading(request: HaltRequest, *, services: ServiceDependency):
        return await services.halt_trading(request)

    @app.post("/api/risk/resume", response_model=RiskControlState)
    async def resume_trading(request: ResumeRequest, *, services: ServiceDependency):
        return await services.resume_trading(request)
