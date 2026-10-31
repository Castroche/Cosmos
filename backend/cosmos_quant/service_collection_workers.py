from __future__ import annotations

import asyncio
from functools import partial
from typing import Literal

from .api_contracts import CatalogJournalCaptureResult
from .catalog_journal import MarketCatalogJournalSnapshot, build_catalog_journal_observation

logger = __import__("logging").getLogger(__name__)


class CollectionWorkersService:
    async def capture_catalog_journal_scope(
        self,
        exchange: str,
        market_type: Literal["spot", "swap"],
    ) -> CatalogJournalCaptureResult:
        source_url = self.CATALOG_JOURNAL_SOURCE_URLS.get((exchange, market_type))
        if source_url is None:
            raise ValueError("catalog journal source is not configured for this scope")
        configuration = self.configuration_service.current_configuration()
        adapter = self.CcxtExchangeAdapter(
            exchange,
            {},
            sandbox=False,
            live_enabled=False,
            public_only=True,
            market_type=market_type,
            margin_mode=str(configuration.margin_mode),
            contract_leverage=configuration.contract_leverage,
            proxy_url=configuration.network_proxy_url,
        )
        try:
            entries = await adapter.research_market_catalog(include_tickers=False)
        finally:
            await adapter.close()
        scoped_entries = [item for item in entries if item.market_type == market_type]
        if not scoped_entries:
            raise RuntimeError("exchange returned an empty structural market catalog")
        async with self.catalog_journal_lock:
            previous_payload = await asyncio.to_thread(
                partial(
                    self.store.load_latest_market_catalog_journal_snapshot,
                    exchange=exchange,
                    market_type=market_type,
                )
            )
            previous = (
                await asyncio.to_thread(
                    MarketCatalogJournalSnapshot.model_validate, previous_payload
                )
                if previous_payload
                else None
            )
            snapshot, events = await asyncio.to_thread(
                build_catalog_journal_observation,
                scoped_entries,
                exchange=exchange,
                market_type=market_type,
                source_url=source_url,
                previous=previous,
            )
            await asyncio.to_thread(
                self.store.append_market_catalog_journal_observation,
                snapshot,
                events,
            )
        return CatalogJournalCaptureResult(snapshot=snapshot, events=events)

    async def catalog_journal_collection_loop(self) -> None:
        """Continuously append structural catalogs; failures create gaps, never fake data."""

        while True:
            for exchange in ("binance", "okx", "bybit"):
                for market_type in ("spot", "swap"):
                    try:
                        result = await self.capture_catalog_journal_scope(exchange, market_type)
                        self.add_audit(
                            "market_data",
                            "Prospective exchange catalog observation persisted",
                            exchange=exchange,
                            market_type=market_type,
                            snapshot_id=result.snapshot.id,
                            records=result.snapshot.record_count,
                            changes=len(result.events),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.metrics.increment("catalog_journal_collection_failures_total")
                        self.add_audit(
                            "market_data",
                            "Prospective exchange catalog observation failed; "
                            "continuity remains incomplete",
                            severity="warning",
                            exchange=exchange,
                            market_type=market_type,
                            error=str(exc)[:300],
                        )
            await asyncio.sleep(3600)

    async def maintenance_loop(self) -> None:
        consecutive_failures = 0
        while True:
            try:
                await asyncio.to_thread(self.backup_service.create_if_due)
                consecutive_failures = 0
                self.risk_state_service.recover_maintenance()
            except Exception as exc:
                consecutive_failures += 1
                self.metrics.increment("backup_failures_total")
                if consecutive_failures >= 3:
                    self.risk_state_service.halt(
                        f"自动备份连续失败 {consecutive_failures} 次：{str(exc)[:180]}",
                        source="maintenance",
                    )
            await asyncio.sleep(3600)

    async def official_event_collection_loop(self) -> None:
        """Collect normalized events; only newly active major windows may recall the LLM."""

        while True:
            try:
                await self.collect_official_events_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.metrics.increment("trading_intelligence_official_event_failures_total")
            await asyncio.sleep(300)

    async def learning_refresh_loop(self):
        from .trading_learning import refresh_learning

        while True:
            try:
                await asyncio.to_thread(refresh_learning, self.trading_intelligence_store)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Learning update unavailable; retaining prior snapshot")
            await asyncio.sleep(30)
