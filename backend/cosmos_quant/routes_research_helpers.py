from __future__ import annotations

from .api_contracts import CausalPolicyBacktestRequest
from .backtest import BacktestResult
from .dependencies import ServiceDependency


def register(app):

    @app.post("/api/research/causal-policy/backtest", response_model=BacktestResult)
    async def backtest_causal_strategy_policy(
        request: CausalPolicyBacktestRequest, *, services: ServiceDependency
    ) -> BacktestResult:
        return await services.backtest_causal_strategy_policy(request)
