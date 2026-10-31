from __future__ import annotations

from typing import Literal

from .api_contracts import (
    CatalogJournalCaptureRequest,
    CatalogJournalCaptureResult,
    HistoricalAnchorScanRequest,
    HistoricalUniverseArchiveImportRequest,
    OfficialArchiveCaptureRequest,
    ResearchUniverseJob,
    ResearchUniverseReport,
    ResearchUniverseRequest,
)
from .catalog_journal import (
    MarketCatalogChangeEvent,
    MarketCatalogJournalCoverage,
    MarketCatalogJournalSnapshot,
)
from .dependencies import ServiceDependency
from .historical_anchor_scan import HistoricalAnchorScan
from .official_archive_index import OfficialArchiveIndex
from .research_holdout import SealedHoldoutReport, SealedHoldoutRequest
from .research_universe import ResearchUniverseSnapshot


def register(app):

    @app.post("/api/research/datasets/prepare", response_model=ResearchUniverseReport)
    async def prepare_research_datasets(
        request: ResearchUniverseRequest, *, services: ServiceDependency
    ):
        return await services.prepare_research_datasets(request)

    @app.post("/api/research/holdouts/seal", response_model=SealedHoldoutReport)
    async def seal_research_holdout_endpoint(
        request: SealedHoldoutRequest, *, services: ServiceDependency
    ) -> SealedHoldoutReport:
        return await services.seal_research_holdout_endpoint(request)

    @app.post("/api/research/universe-jobs", response_model=ResearchUniverseJob, status_code=202)
    async def queue_research_universe_job(
        request: ResearchUniverseRequest, *, services: ServiceDependency
    ) -> ResearchUniverseJob:
        return await services.queue_research_universe_job(request)

    @app.get("/api/research/universe-snapshots", response_model=list[ResearchUniverseSnapshot])
    async def list_research_universe_snapshots(limit: int = 50, *, services: ServiceDependency):
        return await services.list_research_universe_snapshots(limit)

    @app.post(
        "/api/research/universe-snapshots/import-archive", response_model=ResearchUniverseSnapshot
    )
    async def import_historical_universe_archive(
        request: HistoricalUniverseArchiveImportRequest, *, services: ServiceDependency
    ):
        """Import an operator-supplied, hash-bound historical catalog artifact."""
        return await services.import_historical_universe_archive(request)

    @app.post(
        "/api/research/catalog-journal/capture", response_model=list[CatalogJournalCaptureResult]
    )
    async def capture_market_catalog_journal(
        request: CatalogJournalCaptureRequest, *, services: ServiceDependency
    ):
        return await services.capture_market_catalog_journal(request)

    @app.post(
        "/api/research/official-archive-index/capture", response_model=list[OfficialArchiveIndex]
    )
    async def capture_official_archive_indexes(
        request: OfficialArchiveCaptureRequest, *, services: ServiceDependency
    ):
        """Fetch exchange-owned archive directories on the server and seal raw hashes."""
        return await services.capture_official_archive_indexes(request)

    @app.get("/api/research/official-archive-indexes", response_model=list[OfficialArchiveIndex])
    async def list_official_archive_indexes(
        exchange: Literal["binance", "bybit"] | None = None,
        limit: int = 100,
        *,
        services: ServiceDependency,
    ):
        return await services.list_official_archive_indexes(exchange, limit)

    @app.post("/api/research/historical-anchor-scan", response_model=HistoricalAnchorScan)
    async def create_historical_anchor_scan(
        request: HistoricalAnchorScanRequest, *, services: ServiceDependency
    ):
        return await services.create_historical_anchor_scan(request)

    @app.get("/api/research/historical-anchor-scans", response_model=list[HistoricalAnchorScan])
    async def list_historical_anchor_scans(
        exchange: Literal["binance"] | None = None, limit: int = 100, *, services: ServiceDependency
    ):
        return await services.list_historical_anchor_scans(exchange, limit)

    @app.get(
        "/api/research/catalog-journal/snapshots", response_model=list[MarketCatalogJournalSnapshot]
    )
    async def list_market_catalog_journal_snapshots(
        exchange: str | None = None,
        market_type: Literal["spot", "swap"] | None = None,
        limit: int = 100,
        *,
        services: ServiceDependency,
    ):
        return await services.list_market_catalog_journal_snapshots(exchange, market_type, limit)

    @app.get("/api/research/catalog-journal/events", response_model=list[MarketCatalogChangeEvent])
    async def list_market_catalog_journal_events(
        exchange: str | None = None,
        market_type: Literal["spot", "swap"] | None = None,
        limit: int = 1000,
        *,
        services: ServiceDependency,
    ):
        return await services.list_market_catalog_journal_events(exchange, market_type, limit)

    @app.get(
        "/api/research/catalog-journal/coverage", response_model=list[MarketCatalogJournalCoverage]
    )
    async def market_catalog_journal_coverage(*, services: ServiceDependency):
        return await services.market_catalog_journal_coverage()

    @app.get(
        "/api/research/universe-snapshots/{snapshot_id}", response_model=ResearchUniverseSnapshot
    )
    async def get_research_universe_snapshot(snapshot_id: str, *, services: ServiceDependency):
        return await services.get_research_universe_snapshot(snapshot_id)

    @app.get("/api/research/universe-jobs", response_model=list[ResearchUniverseJob])
    async def list_research_universe_jobs(limit: int = 50, *, services: ServiceDependency):
        return await services.list_research_universe_jobs(limit)

    @app.get("/api/research/universe-jobs/{job_id}", response_model=ResearchUniverseJob)
    async def get_research_universe_job(job_id: str, *, services: ServiceDependency):
        return await services.get_research_universe_job(job_id)
