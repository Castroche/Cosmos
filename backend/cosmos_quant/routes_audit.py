from __future__ import annotations

from fastapi import Query

from .dependencies import ServiceDependency
from .domain import AuditEvent
from .operator_workspace import AuditEventPage


def register(app):

    @app.get("/api/audit", response_model=list[AuditEvent])
    async def audit_events(*, services: ServiceDependency):
        return await services.audit_events()

    @app.get("/api/audit/events", response_model=AuditEventPage)
    async def paged_audit_events(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: int | None = Query(default=None, ge=1),
        category: str | None = None,
        severity: str | None = None,
        search: str | None = Query(default=None, max_length=120),
        *,
        services: ServiceDependency,
    ):
        return await services.paged_audit_events(limit, cursor, category, severity, search)
