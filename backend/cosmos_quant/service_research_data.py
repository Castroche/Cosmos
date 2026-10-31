from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cosmos_quant.exchanges.ccxt_adapter import CcxtExchangeAdapter

from .api_contracts import ResearchUniverseCell, ResearchUniverseReport, ResearchUniverseRequest
from .domain import (
    BasisPoint,
    Candle,
    FundingRatePoint,
    LiquidationFlowPoint,
    OpenInterestPoint,
    OrderFlowBar,
    PositioningPoint,
)
from .market_data import TIMEFRAME_SECONDS, validate_candles
from .research import ResearchDataset, build_research_dataset, verify_research_dataset
from .research_universe import (
    PointInTimeUniversePolicy,
    ResearchUniverseSnapshot,
    build_point_in_time_universe_snapshot,
    validate_universe_snapshot,
)
from .timeframes import closed_candles

logger = __import__("logging").getLogger(__name__)


class ResearchDataService:
    async def materialize_research_dataset(
        self,
        candles: list[Candle],
        *,
        exchange: str,
        market_type: str,
        symbol: str,
        timeframe: str,
        adapter: CcxtExchangeAdapter | None = None,
        quality=None,
        fetch_derivative_features: bool = True,
    ) -> tuple[
        ResearchDataset,
        list[FundingRatePoint],
        list[OpenInterestPoint],
        list[PositioningPoint],
        list[BasisPoint],
    ]:
        funding_rates: list[FundingRatePoint] = []
        open_interest: list[OpenInterestPoint] = []
        positioning: list[PositioningPoint] = []
        basis: list[BasisPoint] = []
        if market_type == "swap" and fetch_derivative_features:
            if adapter is not None:
                feature_requests = (
                    adapter.fetch_funding_history(
                        symbol,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                    adapter.fetch_open_interest_history(
                        symbol,
                        timeframe,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                    adapter.fetch_positioning_history(
                        symbol,
                        timeframe,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                    adapter.fetch_basis_history(
                        symbol,
                        timeframe,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                )
            else:
                feature_requests = (
                    self.runtime.funding_history(
                        symbol,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                    self.runtime.open_interest_history(
                        symbol,
                        timeframe,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                    self.runtime.positioning_history(
                        symbol,
                        timeframe,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                    self.runtime.basis_history(
                        symbol,
                        timeframe,
                        candles[0].timestamp,
                        candles[-1].timestamp,
                    ),
                )
            feature_results = await asyncio.gather(
                *feature_requests,
                return_exceptions=True,
            )
            funding_rates, open_interest, positioning, basis = [
                [] if isinstance(result, BaseException) else result for result in feature_results
            ]
        dataset = build_research_dataset(
            candles,
            exchange=exchange,
            market_type=market_type,
            symbol=symbol,
            timeframe=timeframe,
            quality=quality or self.runtime.market_quality,
            funding_rates=funding_rates,
            open_interest=open_interest,
            positioning=positioning,
            basis=basis,
        )
        # Immutable datasets are content addressed. Reuse an identical stored copy
        # instead of creating one row per scan, which previously inflated the
        # research inventory with overlapping 5m datasets.
        existing_payload = next(
            (
                item
                for item in await asyncio.to_thread(self.store.list_research_datasets, 1000)
                if item.get("data_fingerprint") == dataset.data_fingerprint
                and item.get("exchange") == dataset.exchange
                and item.get("market_type") == dataset.market_type
                and item.get("symbol") == dataset.symbol
                and item.get("timeframe") == dataset.timeframe
            ),
            None,
        )
        if existing_payload:
            return (
                ResearchDataset.model_validate(existing_payload),
                funding_rates,
                open_interest,
                positioning,
                basis,
            )
        await asyncio.to_thread(
            self.store.save_research_dataset,
            dataset,
            candles,
            funding_rates,
            open_interest,
            positioning,
            basis,
        )
        return dataset, funding_rates, open_interest, positioning, basis

    async def verify_stored_research_dataset(self, dataset: ResearchDataset) -> bool:
        (
            candle_payloads,
            funding_payloads,
            open_interest_payloads,
            positioning_payloads,
            basis_payloads,
            order_flow_payloads,
            liquidation_flow_payloads,
        ) = await asyncio.gather(
            asyncio.to_thread(self.store.load_research_dataset_candles, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_funding, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_open_interest, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_positioning, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_basis, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_order_flow, dataset.id),
            asyncio.to_thread(self.store.load_research_dataset_liquidation_flow, dataset.id),
        )
        verification = verify_research_dataset(
            dataset,
            [Candle.model_validate(item) for item in candle_payloads],
            [FundingRatePoint.model_validate(item) for item in funding_payloads],
            [OpenInterestPoint.model_validate(item) for item in open_interest_payloads],
            [PositioningPoint.model_validate(item) for item in positioning_payloads],
            [BasisPoint.model_validate(item) for item in basis_payloads],
            [OrderFlowBar.model_validate(item) for item in order_flow_payloads],
            [LiquidationFlowPoint.model_validate(item) for item in liquidation_flow_payloads],
        )
        return bool(verification["verified"])

    async def prepare_research_universe(
        self,
        request: ResearchUniverseRequest,
        *,
        progress_callback: Callable[[int, int, ResearchUniverseCell], Awaitable[None]]
        | None = None,
    ) -> ResearchUniverseReport:
        configuration = self.configuration_service.current_configuration()
        research_market_type = request.market_type or str(configuration.market_type)
        symbols = list(
            dict.fromkeys(symbol.strip().upper().replace("-", "/") for symbol in request.symbols)
        )
        if request.selection_mode == "explicit" and not symbols:
            raise ValueError("explicit research universe requires at least one symbol")
        timeframes = list(
            dict.fromkeys(timeframe.strip().lower() for timeframe in request.timeframes)
        )
        unknown_timeframes = [
            timeframe for timeframe in timeframes if timeframe not in TIMEFRAME_SECONDS
        ]
        if unknown_timeframes:
            raise ValueError("unsupported research timeframes: " + ",".join(unknown_timeframes))
        exchange_ids = list(
            dict.fromkeys(
                exchange_id.strip().lower()
                for exchange_id in (request.exchange_ids or [configuration.exchange_id])
            )
        )
        unsupported_exchanges = [
            exchange_id
            for exchange_id in exchange_ids
            if exchange_id not in self.SUPPORTED_RESEARCH_EXCHANGES
        ]
        if unsupported_exchanges:
            raise ValueError("unsupported research exchanges: " + ",".join(unsupported_exchanges))
        cells: list[ResearchUniverseCell] = []
        existing_datasets = [
            ResearchDataset.model_validate(payload)
            for payload in await asyncio.to_thread(self.store.list_research_datasets, 1000)
            if payload.get("research_role", "general") == "general"
        ]
        adapters = {
            exchange_id: self.CcxtExchangeAdapter(
                exchange_id,
                {},
                sandbox=False,
                live_enabled=False,
                public_only=True,
                market_type=research_market_type,
                margin_mode=str(configuration.margin_mode),
                contract_leverage=configuration.contract_leverage,
                proxy_url=configuration.network_proxy_url,
            )
            for exchange_id in exchange_ids
        }
        universe_snapshot: ResearchUniverseSnapshot | None = None
        universe_evidence = {}
        if request.selection_mode == "frozen_snapshot":
            try:
                if not request.universe_snapshot_id:
                    raise ValueError("frozen_snapshot selection requires universe_snapshot_id")
                payload = await asyncio.to_thread(
                    self.store.load_research_universe_snapshot,
                    request.universe_snapshot_id,
                )
                if payload is None:
                    raise ValueError("frozen research universe snapshot was not found")
                universe_snapshot = ResearchUniverseSnapshot.model_validate(payload)
                validate_universe_snapshot(universe_snapshot)
                selected = [item for item in universe_snapshot.instruments if item.selected]
                expected_exchanges = {item.entry.exchange for item in selected}
                expected_symbols = {item.entry.symbol for item in selected}
                expected_timeframes = set(universe_snapshot.policy.timeframes)
                if set(exchange_ids) != expected_exchanges:
                    raise ValueError("exchange_ids must exactly match the frozen universe")
                if set(symbols) != expected_symbols:
                    raise ValueError("symbols must exactly match the frozen universe")
                if set(timeframes) != expected_timeframes:
                    raise ValueError("timeframes must exactly match the frozen universe")
                if research_market_type != universe_snapshot.policy.market_type:
                    raise ValueError("market_type must exactly match the frozen universe")
                universe_evidence = {
                    (item.entry.exchange, item.entry.symbol): item for item in selected
                }
                markets = [
                    (item.entry.exchange, item.entry.symbol, timeframe)
                    for item in sorted(
                        selected,
                        key=lambda value: (
                            value.entry.exchange,
                            value.rank or 0,
                            value.entry.symbol,
                        ),
                    )
                    for timeframe in timeframes
                ]
                if len(markets) > request.max_markets:
                    raise ValueError(
                        "frozen universe exceeds max_markets; increase the bound instead "
                        "of silently truncating it"
                    )
            except BaseException:
                await asyncio.gather(
                    *(adapter.close() for adapter in adapters.values()),
                    return_exceptions=True,
                )
                raise
        elif request.selection_mode == "point_in_time":

            async def discover_point_in_time_universe():
                if research_market_type not in {"spot", "swap", "future"}:
                    raise ValueError(
                        "point-in-time universe requires spot, swap, or future market type"
                    )
                catalog_results = await asyncio.gather(
                    *(
                        adapters[exchange_id].research_market_catalog()
                        for exchange_id in exchange_ids
                    ),
                    return_exceptions=True,
                )
                catalogs = {}
                catalog_errors = {}
                for exchange_id, result in zip(exchange_ids, catalog_results, strict=True):
                    if isinstance(result, BaseException):
                        catalogs[exchange_id] = []
                        catalog_errors[exchange_id] = str(result)[:500]
                    else:
                        catalogs[exchange_id] = result
                policy = PointInTimeUniversePolicy(
                    quote_currency=request.quote_currency.upper(),
                    market_type=research_market_type,
                    timeframes=timeframes,
                    allowed_symbols=symbols,
                    minimum_listing_age_days=request.minimum_listing_age_days,
                    minimum_quote_volume_24h=request.minimum_quote_volume_24h,
                    required_bars_by_timeframe={
                        timeframe: self.effective_research_bars(request.bars, timeframe)
                        for timeframe in timeframes
                    },
                    minimum_history_coverage_ratio=(request.minimum_history_coverage_ratio),
                    minimum_symbols_per_exchange=request.minimum_symbols_per_exchange,
                    maximum_symbols_per_exchange=request.maximum_symbols_per_exchange,
                )
                snapshot = build_point_in_time_universe_snapshot(
                    catalogs,
                    policy=policy,
                    catalog_errors=catalog_errors,
                )
                validate_universe_snapshot(snapshot)
                await asyncio.to_thread(self.store.save_research_universe_snapshot, snapshot)
                selected = [item for item in snapshot.instruments if item.selected]
                if not selected:
                    raise ValueError("point-in-time universe selected no eligible markets")
                evidence = {(item.entry.exchange, item.entry.symbol): item for item in selected}
                selected_markets = [
                    (item.entry.exchange, item.entry.symbol, timeframe)
                    for item in sorted(
                        selected,
                        key=lambda value: (
                            value.entry.exchange,
                            value.rank or 0,
                            value.entry.symbol,
                        ),
                    )
                    for timeframe in timeframes
                ]
                if len(selected_markets) > request.max_markets:
                    raise ValueError(
                        "point-in-time universe exceeds max_markets; increase the bound instead "
                        "of silently truncating the frozen universe"
                    )
                return snapshot, evidence, selected_markets

            try:
                (
                    universe_snapshot,
                    universe_evidence,
                    markets,
                ) = await discover_point_in_time_universe()
            except BaseException:
                await asyncio.gather(
                    *(adapter.close() for adapter in adapters.values()),
                    return_exceptions=True,
                )
                raise
        else:
            markets = [
                (exchange_id, symbol, timeframe)
                for exchange_id in exchange_ids
                for symbol in symbols
                for timeframe in timeframes
            ][: request.max_markets]
        # Research adapters are public-only and independent from the configured
        # trading venue. Sequential pagination respects each exchange rate limit,
        # while every dataset retains its own venue identity and quality evidence.
        try:
            for exchange_id, symbol, timeframe in markets:
                requested_bars = self.effective_research_bars(request.bars, timeframe)
                try:
                    freshness_cutoff = datetime.now(UTC) - timedelta(
                        seconds=TIMEFRAME_SECONDS[timeframe] * 2
                    )
                    reusable_candidates = sorted(
                        (
                            dataset
                            for dataset in existing_datasets
                            if dataset.exchange == exchange_id
                            and dataset.symbol == symbol
                            and dataset.timeframe == timeframe
                            and dataset.market_type == research_market_type
                            and dataset.candle_count >= requested_bars
                            and dataset.data_end >= freshness_cutoff
                            and dataset.gap_count == 0
                            and dataset.invalid_count == 0
                        ),
                        key=lambda dataset: (dataset.candle_count, dataset.created_at),
                        reverse=True,
                    )
                    reusable = None
                    for candidate in reusable_candidates:
                        if await self.verify_stored_research_dataset(candidate):
                            reusable = candidate
                            break
                    if reusable is not None:
                        snapshot_evidence = universe_evidence.get((exchange_id, symbol))
                        cell = ResearchUniverseCell(
                            exchange=exchange_id,
                            symbol=symbol,
                            timeframe=timeframe,
                            requested_bars=requested_bars,
                            candle_count=reusable.candle_count,
                            coverage_ratio=min(
                                Decimal(reusable.candle_count) / Decimal(requested_bars),
                                Decimal("1"),
                            ).quantize(Decimal("0.001")),
                            dataset_id=reusable.id,
                            data_start=reusable.data_start,
                            data_end=reusable.data_end,
                            quality_status=reusable.quality_status,
                            gap_count=reusable.gap_count,
                            funding_count=reusable.funding_count,
                            open_interest_count=reusable.open_interest_count,
                            open_interest_coverage_ratio=(reusable.open_interest_coverage_ratio),
                            positioning_count=reusable.positioning_count,
                            positioning_coverage_ratio=(reusable.positioning_coverage_ratio),
                            basis_count=reusable.basis_count,
                            basis_coverage_ratio=reusable.basis_coverage_ratio,
                            universe_snapshot_id=(
                                universe_snapshot.id if universe_snapshot is not None else None
                            ),
                            universe_rank=(
                                snapshot_evidence.rank if snapshot_evidence is not None else None
                            ),
                            listed_at=(
                                snapshot_evidence.entry.listed_at
                                if snapshot_evidence is not None
                                else None
                            ),
                            quote_volume_24h=(
                                snapshot_evidence.entry.quote_volume_24h
                                if snapshot_evidence is not None
                                else None
                            ),
                            minimum_required_bars=requested_bars,
                            history_coverage_passed=True,
                            ok=True,
                        )
                        cells.append(cell)
                        if progress_callback is not None:
                            await progress_callback(len(cells), len(markets), cell)
                        continue
                    adapter = adapters[exchange_id]
                    raw_history = await adapter.fetch_candles(
                        symbol,
                        timeframe,
                        requested_bars + 2,
                    )
                    history = closed_candles(
                        raw_history,
                        timeframe,
                        datetime.now(UTC),
                    )[-requested_bars:]
                    history, quality = validate_candles(history, timeframe)
                    materialize_kwargs = {
                        "exchange": exchange_id,
                        "market_type": research_market_type,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "adapter": adapter,
                        "quality": quality,
                    }
                    if request.feature_mode == "ohlcv_only":
                        materialize_kwargs["fetch_derivative_features"] = False
                    (
                        dataset,
                        funding,
                        open_interest,
                        positioning,
                        basis,
                    ) = await self.materialize_research_dataset(history, **materialize_kwargs)
                    coverage = min(
                        Decimal(dataset.candle_count) / Decimal(requested_bars),
                        Decimal("1"),
                    )
                    snapshot_evidence = universe_evidence.get((exchange_id, symbol))
                    cell = ResearchUniverseCell(
                        exchange=exchange_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        requested_bars=requested_bars,
                        candle_count=dataset.candle_count,
                        coverage_ratio=coverage.quantize(Decimal("0.001")),
                        dataset_id=dataset.id,
                        data_start=dataset.data_start,
                        data_end=dataset.data_end,
                        quality_status=dataset.quality_status,
                        gap_count=dataset.gap_count,
                        funding_count=len(funding),
                        open_interest_count=len(open_interest),
                        open_interest_coverage_ratio=(dataset.open_interest_coverage_ratio),
                        positioning_count=len(positioning),
                        positioning_coverage_ratio=(dataset.positioning_coverage_ratio),
                        basis_count=len(basis),
                        basis_coverage_ratio=dataset.basis_coverage_ratio,
                        universe_snapshot_id=(
                            universe_snapshot.id if universe_snapshot is not None else None
                        ),
                        universe_rank=(
                            snapshot_evidence.rank if snapshot_evidence is not None else None
                        ),
                        listed_at=(
                            snapshot_evidence.entry.listed_at
                            if snapshot_evidence is not None
                            else None
                        ),
                        quote_volume_24h=(
                            snapshot_evidence.entry.quote_volume_24h
                            if snapshot_evidence is not None
                            else None
                        ),
                        minimum_required_bars=requested_bars,
                        history_coverage_passed=bool(
                            coverage >= request.minimum_history_coverage_ratio
                        ),
                        ok=bool(
                            dataset.gap_count == 0
                            and dataset.invalid_count == 0
                            and coverage >= request.minimum_history_coverage_ratio
                        ),
                    )
                    cells.append(cell)
                except Exception as exc:
                    snapshot_evidence = universe_evidence.get((exchange_id, symbol))
                    cell = ResearchUniverseCell(
                        exchange=exchange_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        requested_bars=requested_bars,
                        universe_snapshot_id=(
                            universe_snapshot.id if universe_snapshot is not None else None
                        ),
                        universe_rank=(
                            snapshot_evidence.rank if snapshot_evidence is not None else None
                        ),
                        listed_at=(
                            snapshot_evidence.entry.listed_at
                            if snapshot_evidence is not None
                            else None
                        ),
                        quote_volume_24h=(
                            snapshot_evidence.entry.quote_volume_24h
                            if snapshot_evidence is not None
                            else None
                        ),
                        minimum_required_bars=requested_bars,
                        error=str(exc)[:500],
                    )
                    cells.append(cell)
                if progress_callback is not None:
                    await progress_callback(len(cells), len(markets), cell)
        finally:
            await asyncio.gather(
                *(adapter.close() for adapter in adapters.values()),
                return_exceptions=True,
            )
        materialized_scope_complete = bool(
            len(cells) == len(markets)
            and all(cell.dataset_id is not None and cell.ok for cell in cells)
        )
        selection_limitations = (
            list(universe_snapshot.limitations) if universe_snapshot is not None else []
        )
        if universe_snapshot is not None and not materialized_scope_complete:
            selection_limitations.append(
                "The frozen market scope did not fully materialize at the required "
                "history coverage; Campaign execution is prohibited."
            )
        return ResearchUniverseReport(
            requested_markets=len(markets),
            completed_markets=sum(cell.dataset_id is not None for cell in cells),
            healthy_markets=sum(cell.ok for cell in cells),
            total_candles=sum(cell.candle_count for cell in cells),
            cells=cells,
            universe_snapshot_id=(universe_snapshot.id if universe_snapshot is not None else None),
            universe_snapshot_fingerprint=(
                universe_snapshot.fingerprint if universe_snapshot is not None else None
            ),
            point_in_time_selection_passed=bool(
                universe_snapshot and universe_snapshot.point_in_time_selection_passed
            ),
            historical_survivorship_complete=bool(
                universe_snapshot and universe_snapshot.historical_survivorship_complete
            ),
            materialized_scope_complete=materialized_scope_complete,
            selection_limitations=selection_limitations,
        )
