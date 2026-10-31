from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from . import (
    routes_account,
    routes_audit,
    routes_carry,
    routes_commissioning,
    routes_intelligence_automation,
    routes_intelligence_decisions,
    routes_intelligence_execution,
    routes_intelligence_queries,
    routes_market,
    routes_operations,
    routes_relative_value,
    routes_research_admissions,
    routes_research_candidates,
    routes_research_helpers,
    routes_research_jobs,
    routes_research_proposals,
    routes_research_revisions,
    routes_research_universe,
    routes_review,
    routes_settings,
    routes_strategy_library,
)
from .application_services import ApplicationServices
from .http_middleware import InstanceCORSMiddleware
from .settings import Settings


def create_app(settings: Settings | None = None, *, services=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        context = app.state.services
        if context is None:
            context = ApplicationServices(settings)
            app.state.services = context
        async with context.lifespan(app):
            yield

    app = FastAPI(
        title="COSMOS Quant API",
        version="2.0.0",
        description=(
            "Event-driven Trading Intelligence, deterministic risk, and offline research API"
        ),
        lifespan=lifespan,
    )
    app.state.services = services
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "test"]
    )
    app.add_middleware(InstanceCORSMiddleware)
    routes_research_helpers.register(app)
    routes_account.register(app)
    routes_operations.register(app)
    routes_settings.register(app)
    routes_review.register(app)
    routes_research_admissions.register(app)
    routes_commissioning.register(app)
    routes_market.register(app)
    routes_strategy_library.register(app)
    routes_research_jobs.register(app)
    routes_relative_value.register(app)
    routes_carry.register(app)
    routes_research_revisions.register(app)
    routes_research_universe.register(app)
    routes_research_proposals.register(app)
    routes_research_candidates.register(app)
    routes_audit.register(app)
    routes_intelligence_queries.register(app)
    routes_intelligence_decisions.register(app)
    routes_intelligence_execution.register(app)
    routes_intelligence_automation.register(app)
    distribution = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
    if distribution.exists():
        app.mount("/", StaticFiles(directory=distribution, html=True), name="cosmos-web")
    return app


app = create_app()
