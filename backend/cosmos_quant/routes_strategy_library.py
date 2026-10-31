from __future__ import annotations

from typing import Literal

from .autonomous_research import AutonomousResearchRunRequest, ResearchRunResult
from .dependencies import ServiceDependency
from .domain import StrategyPlan
from .operator_workspace import (
    PersonalStrategyCreate,
    PersonalStrategyUpdate,
    StrategyLibraryItem,
    StrategyLibraryPage,
    StrategyLibraryPreference,
)
from .research import ResearchDataset


def register(app):

    @app.get("/api/strategies", response_model=list[StrategyPlan])
    async def list_strategies(*, services: ServiceDependency):
        return await services.list_strategies()

    @app.get("/api/research/strategy-spec")
    async def strategy_spec_contract(*, services: ServiceDependency):
        return await services.strategy_spec_contract()

    @app.post("/api/research/autonomous-runs", response_model=ResearchRunResult)
    async def create_autonomous_research_run(
        request: AutonomousResearchRunRequest, *, services: ServiceDependency
    ):
        return await services.create_autonomous_research_run(request)

    @app.get("/api/research/autonomous-runs/{run_id}", response_model=ResearchRunResult)
    async def autonomous_research_run_result(run_id: str, *, services: ServiceDependency):
        return await services.autonomous_research_run_result(run_id)

    @app.get("/api/research/trials")
    async def permanent_trial_ledger(
        family_id: str | None = None, limit: int = 100, *, services: ServiceDependency
    ):
        return await services.permanent_trial_ledger(family_id, limit)

    @app.get("/api/research/certificates/{certificate_id}")
    async def research_certificate_status(certificate_id: str, *, services: ServiceDependency):
        return await services.research_certificate_status(certificate_id)

    @app.get("/api/research/datasets", response_model=list[ResearchDataset])
    async def research_datasets(limit: int = 50, *, services: ServiceDependency):
        return await services.research_datasets(limit)

    @app.get("/api/research/strategy-library")
    async def builtin_strategy_library(family: str | None = None, *, services: ServiceDependency):
        return await services.builtin_strategy_library(family)

    @app.get("/api/strategy-library", response_model=StrategyLibraryPage)
    async def operator_strategy_library(
        origin: Literal["builtin", "personal", "agent"] | None = None,
        status: str | None = None,
        family: str | None = None,
        include_hidden: bool = False,
        search: str | None = None,
        *,
        services: ServiceDependency,
    ):
        return await services.operator_strategy_library(
            origin, status, family, include_hidden, search
        )

    @app.post("/api/strategy-library", response_model=StrategyLibraryItem)
    async def create_personal_strategy(
        request: PersonalStrategyCreate, *, services: ServiceDependency
    ):
        return await services.create_personal_strategy(request)

    @app.patch("/api/strategy-library/{item_id}", response_model=StrategyLibraryItem)
    async def update_personal_strategy(
        item_id: str, request: PersonalStrategyUpdate, *, services: ServiceDependency
    ):
        return await services.update_personal_strategy(item_id, request)

    @app.post("/api/strategy-library/{item_id}/archive", response_model=StrategyLibraryItem)
    async def archive_strategy_library_item(item_id: str, *, services: ServiceDependency):
        return await services.archive_strategy_library_item(item_id)

    @app.post("/api/strategy-library/{item_id}/restore", response_model=StrategyLibraryItem)
    async def restore_strategy_library_item(item_id: str, *, services: ServiceDependency):
        return await services.restore_strategy_library_item(item_id)

    @app.put("/api/strategy-library/{item_id}/preference", response_model=StrategyLibraryItem)
    async def set_strategy_library_preference(
        item_id: str, request: StrategyLibraryPreference, *, services: ServiceDependency
    ):
        return await services.set_strategy_library_preference(item_id, request)

    @app.delete("/api/strategy-library/{item_id}")
    async def delete_personal_strategy(item_id: str, *, services: ServiceDependency):
        return await services.delete_personal_strategy(item_id)
