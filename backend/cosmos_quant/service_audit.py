from __future__ import annotations

import asyncio

from fastapi import Query

from .domain import AuditEvent
from .operator_workspace import AuditEventPage

logger = __import__("logging").getLogger(__name__)


class AuditService:
    async def audit_events(self):
        return self.audit

    async def paged_audit_events(
        self,
        limit: int = Query(default=50, ge=1, le=200),
        cursor: int | None = Query(default=None, ge=1),
        category: str | None = None,
        severity: str | None = None,
        search: str | None = Query(default=None, max_length=120),
    ):
        items, next_cursor = await asyncio.to_thread(
            self.store.page_audit,
            limit=limit,
            before_sequence=cursor,
            category=category,
            severity=severity,
            search=search.strip() if search else None,
        )
        return AuditEventPage(
            items=[AuditEvent.model_validate(item) for item in items],
            next_cursor=next_cursor,
        )
