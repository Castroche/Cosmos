from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .dependencies import ServiceDependency
from .trading_learning import LearningSnapshot, selected_snapshot


class LearningSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    pinned_version: str | None = Field(default=None, min_length=1, max_length=100)
    shadow_enabled: bool = True


class LearningVersion(BaseModel):
    version: str
    sample_count: int
    updated_at: datetime
    algorithm: str


class LearningVersionPage(BaseModel):
    items: list[LearningVersion]
    total: int
    limit: int
    offset: int


class ShadowPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    symbol: str
    mode: str
    primary: dict[str, Any]
    control: dict[str, Any] | None = None
    format_valid: bool | None = None
    learning_version: str | None = None
    case_ids: list[str] = Field(default_factory=list)
    proxy: dict[str, Any] = Field(default_factory=dict)
    skip_reason: str | None = None


class ShadowRecord(BaseModel):
    decision_id: str
    budget_day: str
    status: str
    created_at: datetime
    reserved: int
    payload: ShadowPayload


class ShadowPage(BaseModel):
    items: list[ShadowRecord]
    total: int
    limit: int
    offset: int


class ShadowSummary(BaseModel):
    counts: dict[str, int]
    completed: int
    format_valid_rate: float | None
    budget_day: str
    daily_used: int
    daily_limit: int
    sample_rate: float
    evidence_status: str
    cost: float | None
    cost_status: str
    automatic_promotion: bool


def version_page(store, limit, offset):
    with store.store.connect() as c:
        rows = c.execute(
            "SELECT payload FROM ti_artifacts_v2 WHERE artifact_type=? "
            "ORDER BY created_at DESC, artifact_id DESC LIMIT ? OFFSET ?",
            ("TradingLearningVersion", limit, offset),
        ).fetchall()
        total = c.execute(
            "SELECT COUNT(*) FROM ti_artifacts_v2 WHERE artifact_type=?",
            ("TradingLearningVersion",),
        ).fetchone()[0]
    import json

    return {
        "items": [LearningVersion.model_validate(json.loads(r["payload"])) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def register(app):
    @app.get("/api/trading-intelligence/learning/snapshot", response_model=LearningSnapshot)
    async def snapshot(services: ServiceDependency):
        return await asyncio.to_thread(selected_snapshot, services.trading_intelligence_store)

    @app.get("/api/trading-intelligence/learning/versions", response_model=LearningVersionPage)
    async def versions(
        services: ServiceDependency,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return await asyncio.to_thread(
            version_page, services.trading_intelligence_store, limit, offset
        )

    @app.get("/api/trading-intelligence/learning/settings", response_model=LearningSettings)
    async def settings(services: ServiceDependency):
        return await asyncio.to_thread(services.store.learning.settings)

    @app.put("/api/trading-intelligence/learning/settings", response_model=LearningSettings)
    async def update_settings(request: LearningSettings, services: ServiceDependency):
        store = services.trading_intelligence_store
        if request.pinned_version and not await asyncio.to_thread(
            store.get_artifact, f"learning-{request.pinned_version}"
        ):
            raise HTTPException(404, "Learning version not found")
        result = await asyncio.to_thread(
            services.store.learning.save_settings, request.model_dump()
        )
        await asyncio.to_thread(
            store.append_event,
            dedupe_key=f"learning-settings:{uuid4().hex}",
            event_type="LEARNING_SETTINGS_UPDATED",
            aggregate_type="learning",
            aggregate_id="trading-learning",
            aggregate_version=1,
            payload=result,
        )
        if not request.shadow_enabled or not request.enabled:
            await services.learning_shadow.stop()
        elif (
            services.trading_intelligence_automation
            and services.trading_intelligence_automation.runtime().enabled
        ):
            services.learning_shadow.start()
        return result

    @app.get("/api/trading-intelligence/learning/comparisons", response_model=ShadowPage)
    async def comparisons(
        services: ServiceDependency,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return await asyncio.to_thread(services.store.learning.shadows, limit, offset)

    @app.get("/api/trading-intelligence/learning/summary", response_model=ShadowSummary)
    async def summary(services: ServiceDependency):
        return await asyncio.to_thread(services.store.learning.summary)
