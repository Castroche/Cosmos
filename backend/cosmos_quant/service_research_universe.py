from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from functools import partial
from typing import Literal

from fastapi import HTTPException

from .api_contracts import (
    CatalogJournalCaptureRequest,
    CatalogJournalCaptureResult,
    HistoricalAnchorScanRequest,
    HistoricalUniverseArchiveImportRequest,
    OfficialArchiveCaptureRequest,
    ResearchUniverseCell,
    ResearchUniverseJob,
    ResearchUniverseRequest,
)
from .catalog_journal import (
    MarketCatalogChangeEvent,
    MarketCatalogJournalSnapshot,
    evaluate_catalog_journal_coverage,
)
from .historical_anchor_scan import HistoricalAnchorScan, build_binance_historical_anchor_scan
from .official_archive_index import OfficialArchiveIndex, download_official_archive_index
from .research import ResearchJobStatus
from .research_holdout import SealedHoldoutReport, SealedHoldoutRequest, seal_research_holdout
from .research_universe import (
    ResearchUniverseSnapshot,
    build_point_in_time_universe_snapshot,
    validate_universe_snapshot,
)

logger = __import__("logging").getLogger(__name__)


class ResearchUniverseService:
    async def prepare_research_datasets(self, request: ResearchUniverseRequest):
        try:
            report = await self.prepare_research_universe(request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        self.add_audit(
            "research",
            "批量研究数据集抓取完成",
            requested_markets=report.requested_markets,
            completed_markets=report.completed_markets,
            healthy_markets=report.healthy_markets,
            total_candles=report.total_candles,
        )
        return report

    async def seal_research_holdout_endpoint(
        self,
        request: SealedHoldoutRequest,
    ) -> SealedHoldoutReport:
        try:
            report = await asyncio.to_thread(seal_research_holdout, self.store, request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        self.add_audit(
            "research",
            "冻结研究范围已切分为开发集与单次盲测集",
            blind_set_id=report.blind_set_id,
            universe_snapshot_id=report.universe_snapshot_id,
            market_count=len(report.datasets),
            development_dataset_count=len(report.development_dataset_ids),
            blind_dataset_count=len(report.blind_dataset_ids),
        )
        return report

    async def run_research_universe_job(self, job: ResearchUniverseJob) -> None:
        job.status = ResearchJobStatus.RUNNING
        job.started_at = job.started_at or datetime.now(UTC)
        job.completed_at = None
        job.error = None
        job.completed_markets = 0
        job.healthy_markets = 0
        job.total_candles = 0
        job.last_cell = None
        job.report = None
        job.updated_at = datetime.now(UTC)
        await asyncio.to_thread(self.store.save_research_universe_job, job)

        async def persist_progress(
            completed: int,
            total: int,
            cell: ResearchUniverseCell,
        ) -> None:
            job.completed_markets = completed
            job.total_markets = total
            job.healthy_markets += int(cell.ok)
            job.total_candles += cell.candle_count
            job.last_cell = cell
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_research_universe_job, job)

        try:
            report = await self.prepare_research_universe(
                ResearchUniverseRequest.model_validate(job.request),
                progress_callback=persist_progress,
            )
            job.report = report
            job.total_markets = report.requested_markets
            job.completed_markets = report.completed_markets
            job.healthy_markets = report.healthy_markets
            job.total_candles = report.total_candles
            job.status = ResearchJobStatus.COMPLETED
            job.completed_at = datetime.now(UTC)
        except asyncio.CancelledError:
            job.status = ResearchJobStatus.QUEUED
            job.updated_at = datetime.now(UTC)
            await asyncio.to_thread(self.store.save_research_universe_job, job)
            raise
        except Exception as exc:
            job.status = ResearchJobStatus.FAILED
            job.error = str(getattr(exc, "detail", exc))[:1000]
            job.completed_at = datetime.now(UTC)
        finally:
            if job.status != ResearchJobStatus.QUEUED:
                job.updated_at = datetime.now(UTC)
                await asyncio.to_thread(self.store.save_research_universe_job, job)

    def schedule_research_universe_job(self, job: ResearchUniverseJob) -> None:
        task = asyncio.create_task(
            self.run_research_universe_job(job),
            name=f"research-universe:{job.id}",
        )
        self.universe_job_tasks.add(task)
        task.add_done_callback(self.universe_job_tasks.discard)

    async def queue_research_universe_job(
        self,
        request: ResearchUniverseRequest,
    ) -> ResearchUniverseJob:
        normalized = request.model_dump(mode="json")
        normalized["symbols"] = sorted(
            {symbol.strip().upper().replace("-", "/") for symbol in request.symbols}
        )
        normalized["timeframes"] = sorted(
            {timeframe.strip().lower() for timeframe in request.timeframes}
        )
        normalized["exchange_ids"] = sorted(
            {exchange.strip().lower() for exchange in request.exchange_ids}
        )
        fingerprint = self.research_universe_request_fingerprint(normalized)
        for payload in await asyncio.to_thread(self.store.list_research_universe_jobs, 100):
            existing = ResearchUniverseJob.model_validate(payload)
            if existing.request_fingerprint == fingerprint and existing.status in {
                ResearchJobStatus.QUEUED,
                ResearchJobStatus.RUNNING,
            }:
                return existing
        total_markets = min(
            len(normalized["symbols"])
            * len(normalized["timeframes"])
            * max(len(normalized["exchange_ids"]), 1),
            request.max_markets,
        )
        job = ResearchUniverseJob(
            request=normalized,
            request_fingerprint=fingerprint,
            total_markets=total_markets,
        )
        await asyncio.to_thread(self.store.save_research_universe_job, job)
        self.schedule_research_universe_job(job)
        return job

    async def list_research_universe_snapshots(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_research_universe_snapshots,
            min(max(limit, 1), 200),
        )
        snapshots: list[ResearchUniverseSnapshot] = []
        for payload in payloads:
            snapshot = ResearchUniverseSnapshot.model_validate(payload)
            try:
                validate_universe_snapshot(snapshot)
            except ValueError:
                # Preserve corrupt/tampered legacy evidence in storage and keep the
                # direct endpoint fail-closed with HTTP 409, but quarantine it from
                # the collection of usable snapshots so one bad artifact cannot
                # make every valid snapshot undiscoverable.
                continue
            snapshots.append(snapshot)
        return snapshots

    async def import_historical_universe_archive(
        self,
        request: HistoricalUniverseArchiveImportRequest,
    ):
        """Import an operator-supplied, hash-bound historical catalog artifact."""

        if request.as_of.tzinfo is None:
            raise HTTPException(422, "historical archive as_of must be timezone-aware")
        if set(request.catalogs) - self.SUPPORTED_RESEARCH_EXCHANGES:
            raise HTTPException(422, "historical archive contains an unsupported exchange")
        record_count = sum(len(items) for items in request.catalogs.values())
        if record_count > 5000:
            raise HTTPException(422, "historical archive supports at most 5000 instruments")
        try:
            snapshot = build_point_in_time_universe_snapshot(
                request.catalogs,
                policy=request.policy,
                as_of=request.as_of,
                archive_evidence=request.archive_evidence,
            )
            validate_universe_snapshot(snapshot)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await asyncio.to_thread(self.store.save_research_universe_snapshot, snapshot)
        self.add_audit(
            "research",
            "Historical point-in-time market catalog archive imported",
            universe_snapshot_id=snapshot.id,
            exchanges=len(snapshot.exchange_ids),
            instruments=len(snapshot.instruments),
            historical_survivorship_complete=snapshot.historical_survivorship_complete,
        )
        return snapshot

    async def capture_market_catalog_journal(self, request: CatalogJournalCaptureRequest):
        exchanges = sorted({item.strip().lower() for item in request.exchange_ids})
        if set(exchanges) - self.SUPPORTED_RESEARCH_EXCHANGES:
            raise HTTPException(422, "catalog journal contains an unsupported exchange")
        unsupported_scopes = [
            f"{exchange}:{market_type}"
            for exchange in exchanges
            for market_type in request.market_types
            if (exchange, market_type) not in self.CATALOG_JOURNAL_SOURCE_URLS
        ]
        if unsupported_scopes:
            raise HTTPException(
                422,
                "catalog journal source is not configured: " + ",".join(unsupported_scopes),
            )
        results: list[CatalogJournalCaptureResult] = []
        for exchange in exchanges:
            for market_type in sorted(set(request.market_types)):
                try:
                    results.append(await self.capture_catalog_journal_scope(exchange, market_type))
                except (RuntimeError, ValueError) as exc:
                    raise HTTPException(503, str(exc)) from exc
        return results

    async def capture_official_archive_indexes(self, request: OfficialArchiveCaptureRequest):
        """Fetch exchange-owned archive directories on the server and seal raw hashes."""

        proxy_url = self.configuration_service.current_configuration().network_proxy_url
        archives: list[OfficialArchiveIndex] = []
        for exchange in sorted(set(request.exchange_ids)):
            try:
                archive = await asyncio.to_thread(
                    download_official_archive_index,
                    exchange,
                    proxy_url=proxy_url,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                raise HTTPException(
                    503,
                    f"unable to capture {exchange} official archive index: {exc}",
                ) from exc
            await asyncio.to_thread(self.store.save_official_archive_index, archive)
            archives.append(archive)
            self.add_audit(
                "research",
                "Official historical archive index captured",
                archive_id=archive.id,
                exchange=archive.exchange,
                instruments=archive.normalized_usdt_perpetual_count,
                source_payload_sha256=archive.source_payload_sha256,
                historical_point_in_time_complete=False,
            )
        return archives

    async def list_official_archive_indexes(
        self,
        exchange: Literal["binance", "bybit"] | None = None,
        limit: int = 100,
    ):
        payloads = await asyncio.to_thread(
            partial(
                self.store.list_official_archive_indexes,
                exchange=exchange,
                limit=min(max(limit, 1), 1000),
            )
        )
        return [OfficialArchiveIndex.model_validate(item) for item in payloads]

    async def create_historical_anchor_scan(self, request: HistoricalAnchorScanRequest):
        payload = await asyncio.to_thread(
            self.store.load_official_archive_index,
            request.official_archive_index_id,
        )
        if payload is None:
            raise HTTPException(404, "official archive index not found")
        archive = OfficialArchiveIndex.model_validate(payload)
        try:
            scan = await asyncio.to_thread(
                build_binance_historical_anchor_scan,
                archive,
                anchor_month=request.anchor_month,
                proxy_url=self.configuration_service.current_configuration().network_proxy_url,
                maximum_workers=request.maximum_workers,
            )
            await asyncio.to_thread(self.store.save_historical_anchor_scan, scan)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(503, str(exc)) from exc
        self.add_audit(
            "research",
            "Official historical anchor candidate scan completed",
            scan_id=scan.id,
            archive_id=archive.id,
            anchor_month=scan.anchor_month.isoformat(),
            candidates=scan.candidate_count,
            observed=scan.observed_count,
            absent=scan.absent_count,
            historical_point_in_time_complete=False,
        )
        return scan

    async def list_historical_anchor_scans(
        self,
        exchange: Literal["binance"] | None = None,
        limit: int = 100,
    ):
        payloads = await asyncio.to_thread(
            partial(
                self.store.list_historical_anchor_scans,
                exchange=exchange,
                limit=min(max(limit, 1), 1000),
            )
        )
        return [HistoricalAnchorScan.model_validate(item) for item in payloads]

    async def list_market_catalog_journal_snapshots(
        self,
        exchange: str | None = None,
        market_type: Literal["spot", "swap"] | None = None,
        limit: int = 100,
    ):
        payloads = await asyncio.to_thread(
            partial(
                self.store.list_market_catalog_journal_snapshots,
                exchange=exchange.lower() if exchange else None,
                market_type=market_type,
                limit=min(max(limit, 1), 1000),
            )
        )
        return [MarketCatalogJournalSnapshot.model_validate(item) for item in payloads]

    async def list_market_catalog_journal_events(
        self,
        exchange: str | None = None,
        market_type: Literal["spot", "swap"] | None = None,
        limit: int = 1000,
    ):
        payloads = await asyncio.to_thread(
            partial(
                self.store.list_market_catalog_change_events,
                exchange=exchange.lower() if exchange else None,
                market_type=market_type,
                limit=min(max(limit, 1), 10000),
            )
        )
        return [MarketCatalogChangeEvent.model_validate(item) for item in payloads]

    async def market_catalog_journal_coverage(self):
        snapshot_payloads, event_payloads = await asyncio.gather(
            asyncio.to_thread(self.store.list_market_catalog_journal_snapshots, limit=100000),
            asyncio.to_thread(self.store.list_market_catalog_change_events, limit=100000),
        )
        snapshots = [
            MarketCatalogJournalSnapshot.model_validate(item) for item in snapshot_payloads
        ]
        events = [MarketCatalogChangeEvent.model_validate(item) for item in event_payloads]
        now = datetime.now(UTC)
        return [
            evaluate_catalog_journal_coverage(
                snapshots,
                events,
                exchange=exchange,
                market_type=market_type,
                now=now,
            )
            for exchange in ("binance", "okx", "bybit")
            for market_type in ("spot", "swap")
        ]

    async def get_research_universe_snapshot(self, snapshot_id: str):
        payload = await asyncio.to_thread(self.store.load_research_universe_snapshot, snapshot_id)
        if payload is None:
            raise HTTPException(404, "research universe snapshot not found")
        snapshot = ResearchUniverseSnapshot.model_validate(payload)
        try:
            validate_universe_snapshot(snapshot)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return snapshot

    async def list_research_universe_jobs(self, limit: int = 50):
        payloads = await asyncio.to_thread(
            self.store.list_research_universe_jobs,
            min(max(limit, 1), 200),
        )
        return [ResearchUniverseJob.model_validate(payload) for payload in payloads]

    async def get_research_universe_job(self, job_id: str):
        payload = await asyncio.to_thread(self.store.load_research_universe_job, job_id)
        if not payload:
            raise HTTPException(404, "research universe job not found")
        return ResearchUniverseJob.model_validate(payload)
