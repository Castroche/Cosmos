from __future__ import annotations

from fastapi import Query

from .ai_configuration import AIConfigurationUpdate, AIConfigurationView, AIProvider
from .ai_gateway import AIProviderTestResult
from .ai_usage import AIUsageReport
from .configuration import ConfigurationUpdate, ConfigurationView, ConnectionTestResult
from .dependencies import ServiceDependency


def register(app):

    @app.get("/api/settings", response_model=ConfigurationView)
    async def get_settings(*, services: ServiceDependency):
        return await services.get_settings()

    @app.get("/api/ai/settings", response_model=AIConfigurationView)
    async def get_ai_settings(*, services: ServiceDependency):
        return await services.get_ai_settings()

    @app.get("/api/ai/usage", response_model=AIUsageReport)
    async def get_ai_usage(
        days: int = Query(default=30, ge=1, le=365),
        provider: AIProvider | None = None,
        *,
        services: ServiceDependency,
    ):
        return await services.get_ai_usage(days, provider)

    @app.get("/api/ai/health")
    async def get_ai_health(*, services: ServiceDependency):
        return await services.get_ai_health()

    @app.put("/api/ai/settings", response_model=AIConfigurationView)
    async def update_ai_settings(update: AIConfigurationUpdate, *, services: ServiceDependency):
        return await services.update_ai_settings(update)

    @app.post("/api/ai/providers/{provider}/test", response_model=AIProviderTestResult)
    async def test_ai_provider(provider: AIProvider, *, services: ServiceDependency):
        return await services.test_ai_provider(provider)

    @app.post("/api/ai/providers/{provider}/discover-models", response_model=list[str])
    async def discover_ai_models(provider: AIProvider, *, services: ServiceDependency):
        return await services.discover_ai_models(provider)

    @app.put("/api/settings", response_model=ConfigurationView)
    async def update_settings(update: ConfigurationUpdate, *, services: ServiceDependency):
        return await services.update_settings(update)

    @app.post("/api/settings/test-connection", response_model=ConnectionTestResult)
    async def test_connection(update: ConfigurationUpdate, *, services: ServiceDependency):
        return await services.test_connection(update)
