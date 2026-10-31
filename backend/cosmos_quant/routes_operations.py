from __future__ import annotations

from fastapi import Request
from fastapi.responses import PlainTextResponse

from .acceptance import ProductAcceptanceReport
from .backup import BackupResult
from .dependencies import ServiceDependency


def register(app):

    @app.middleware("http")
    async def operational_headers_and_metrics(request: Request, call_next):
        services = request.app.state.services
        return await services.operational_headers_and_metrics(request, call_next)

    @app.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
    async def prometheus_metrics(*, services: ServiceDependency):
        return await services.prometheus_metrics()

    @app.get("/api/ready")
    async def readiness(*, services: ServiceDependency):
        return await services.readiness()

    @app.post("/api/maintenance/backup", response_model=BackupResult)
    async def create_backup(*, services: ServiceDependency):
        return await services.create_backup()

    @app.get("/api/maintenance/backup/latest", response_model=BackupResult | None)
    async def latest_backup(*, services: ServiceDependency):
        return await services.latest_backup()

    @app.get("/api/health")
    async def health(*, services: ServiceDependency):
        return await services.health()

    @app.get("/api/readiness")
    async def worker_readiness(*, services: ServiceDependency):
        return await services.worker_readiness()

    @app.get("/api/acceptance/product", response_model=ProductAcceptanceReport)
    async def product_acceptance(*, services: ServiceDependency):
        return await services.product_acceptance()
