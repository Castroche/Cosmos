from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from fastapi import HTTPException, Query
from pydantic import ValidationError

from .api_contracts import HumanLiveGrantPreviewRequest
from .decision_attribution import DecisionOutcomeV2, build_decision_attribution
from .domain import Candle, PortfolioState
from .market_data import validate_candles
from .market_state_v2 import build_cross_market_context, build_market_state_v2
from .official_events import ETF_FLOW_STATUS, OfficialEvent
from .timeframes import closed_candles
from .trade_history import build_trade_history_v2
from .trading_intelligence import (
    V2_SYMBOLS,
    MarketStateV2,
    PositionRuntimeV2,
    PositionStateV2,
    RiskAllocation,
    TradingModeV2,
)
from .trading_intelligence_engine import configuration_fingerprint as v2_configuration_fingerprint

logger = __import__("logging").getLogger(__name__)


class IntelligenceQueriesService:
    def v2_llm_portfolio_context(
        self,
        portfolio: PortfolioState,
        *,
        symbol: str,
        max_daily_drawdown: Decimal,
        risk_halted: bool,
        unresolved_reconciliation: bool,
        managed_positions: list[PositionRuntimeV2] | None = None,
    ) -> dict:
        """Expose position context to the LLM without leaking sizing/affordability inputs."""

        normalized_symbol = symbol.removesuffix(":USDT")
        positions = [
            {
                "symbol": item.symbol,
                "side": str(item.side),
                "average_entry": str(item.average_entry),
                "mark_price": str(item.mark_price),
                "stop_loss": str(item.stop_loss) if item.stop_loss is not None else None,
                "take_profit": str(item.take_profit) if item.take_profit is not None else None,
            }
            for item in portfolio.open_positions
        ]
        for position in managed_positions or []:
            if position.state not in {PositionStateV2.OPEN, PositionStateV2.PARTIALLY_FILLED}:
                continue
            row = next(
                (
                    item
                    for item in positions
                    if item["symbol"].removesuffix(":USDT") == position.symbol.removesuffix(":USDT")
                    and item["side"].upper() == position.side
                ),
                None,
            )
            if row is not None:
                row.update(
                    {
                        "symbol": position.symbol,
                        "side": position.side,
                        "position_id": position.position_id,
                        "position_version": position.version,
                        "state": str(position.state),
                        "plan_id": position.plan_id,
                        "stop_loss": str(position.current_stop),
                    }
                )
        symbol_positions = [
            item for item in positions if item["symbol"].removesuffix(":USDT") == normalized_symbol
        ]
        return {
            "decision_symbol": symbol,
            "position_state": "OPEN" if symbol_positions else "FLAT",
            "account_position_state": "OPEN" if positions else "FLAT",
            "symbol_positions": symbol_positions,
            "has_position_for_symbol": any(
                item.symbol.removesuffix(":USDT") == normalized_symbol
                for item in portfolio.open_positions
            ),
            "open_positions": positions,
            "risk_engine_status": "HALTED" if risk_halted else "AVAILABLE",
            "daily_drawdown_limit_reached": (
                portfolio.daily_drawdown_fraction >= max_daily_drawdown
            ),
            "unresolved_order_or_position": unresolved_reconciliation,
            "sizing_note": (
                "Equity, cash, margin, quantity and leverage are intentionally withheld; "
                "the deterministic Risk Engine evaluates them only after an entry trigger."
            ),
        }

    def v2_live_grant_preview_payload(self, request: HumanLiveGrantPreviewRequest) -> dict:
        view = self.configuration_service.get()
        configuration = view.configuration
        fingerprint = v2_configuration_fingerprint(configuration)
        blockers: list[str] = ["live_reconciliation_not_implemented"]
        if configuration.exchange_id != "okx":
            blockers.append("exchange_must_be_okx")
        if str(configuration.market_type) != "swap":
            blockers.append("market_type_must_be_swap")
        credentials = view.credential_scopes.get("okx:live")
        if not credentials or not (
            credentials.api_key_configured
            and credentials.secret_configured
            and credentials.passphrase_configured
        ):
            blockers.append("okx_live_credentials_incomplete")
        commissioning = view.commissioning
        if not commissioning or not commissioning.agent_position_management_passed:
            blockers.append("native_protection_and_position_management_commissioning_required")
        report = self.runtime.reconciliation_report
        if report is None:
            blockers.append("reconciliation_not_available")
        else:
            if report.uncertain_order_ids or report.unknown_open_order_ids:
                blockers.append("uncertain_or_unknown_orders")
            if report.unknown_position_symbols:
                blockers.append("unknown_positions")
            if report.unprotected_order_ids:
                blockers.append("unprotected_orders")
        return {
            "eligible": not blockers,
            "blockers": blockers,
            "configuration_fingerprint": fingerprint,
            "exchange": "okx",
            "symbols": request.symbols,
            "limits": request.model_dump(mode="json"),
            "expires_at": datetime.now(UTC) + timedelta(hours=request.valid_hours),
            "requires_two_confirmations": True,
        }

    def durable_official_events(self, limit: int = 1000) -> list[OfficialEvent]:
        events: dict[str, OfficialEvent] = {}
        for item in self.trading_intelligence_store.list_events(limit=limit):
            if item["event_type"] != "OFFICIAL_EVENT":
                continue
            try:
                event = OfficialEvent.model_validate(item["payload"]["event"])
            except (KeyError, ValidationError):
                continue
            events[event.event_id] = event
        return sorted(events.values(), key=lambda value: value.scheduled_at)

    async def collect_official_events_once(self) -> tuple[list[OfficialEvent], dict[str, str], int]:
        events, health = await self.official_event_client.fetch_all()
        persisted = 0
        for event in events:
            event_id, created = await asyncio.to_thread(
                self.trading_intelligence_store.append_event,
                dedupe_key=f"official-event:{event.source}:{event.source_hash}",
                event_type="OFFICIAL_EVENT",
                aggregate_type="official_event",
                aggregate_id=event.event_id,
                aggregate_version=1,
                payload={"event": event.model_dump(mode="json")},
            )
            persisted += int(created)
            windows = self.active_risk_windows([event])
            if windows:
                window_event_id, window_created = await asyncio.to_thread(
                    self.trading_intelligence_store.append_event,
                    dedupe_key=(
                        f"official-event-window-active:{event.event_id}:"
                        f"{event.scheduled_at.isoformat()}"
                    ),
                    event_type="OFFICIAL_EVENT_WINDOW_ACTIVE",
                    aggregate_type="official_event",
                    aggregate_id=event.event_id,
                    aggregate_version=1,
                    payload={
                        "official_event_id": event_id,
                        "event": event.model_dump(mode="json"),
                        "active_windows": windows,
                    },
                )
                if window_created and self.trading_intelligence_automation is not None:
                    await self.trading_intelligence_automation.on_major_event(window_event_id)
        return events, health, persisted

    async def trading_intelligence_official_events(self):
        events = await asyncio.to_thread(self.durable_official_events)
        return {
            "events": events,
            "active_risk_windows": self.active_risk_windows(events),
            "etf_flow": ETF_FLOW_STATUS,
        }

    async def trading_intelligence_migration_summary(self):
        def legacy_counts() -> dict[str, int]:
            result: dict[str, int] = {}
            with self.store._lock, self.store.connect() as connection:
                names = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                for table in (
                    "agent_goals",
                    "agent_runs",
                    "agent_events",
                    "strategies",
                    "orders",
                ):
                    if table in names:
                        result[table] = int(
                            connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                                "count"
                            ]
                        )
            return result

        return {
            "runtime": "trading_intelligence_v2",
            "legacy_strategy_scheduler_running": False,
            "legacy_agent_scheduler_running": False,
            "legacy_mutations": "not_registered_http_404",
            "legacy_runtime_modules_present": False,
            "legacy_evidence": "read_only",
            "historical_record_counts": await asyncio.to_thread(legacy_counts),
        }

    async def trading_intelligence_quant_models(self):
        result = []
        for symbol in V2_SYMBOLS:
            try:
                artifact = await asyncio.to_thread(self.load_quant_v1_artifact, symbol)
                eligible = bool(artifact and self.quant_model_is_eligible(artifact))
                result.append(
                    {
                        "symbol": symbol,
                        "timeframe": "5m",
                        "health": (
                            "healthy" if eligible else ("degraded" if artifact else "unavailable")
                        ),
                        "entry_eligible": eligible,
                        "reason": (
                            None
                            if eligible or not artifact
                            else "purged OOF metrics do not beat the uniform 3-class baseline"
                        ),
                        "model_fingerprint": artifact.model_fingerprint if artifact else None,
                        "data_as_of": artifact.data_as_of if artifact else None,
                        "validation": artifact.validation if artifact else None,
                    }
                )
            except (OSError, ValueError, RuntimeError) as exc:
                result.append(
                    {
                        "symbol": symbol,
                        "timeframe": "5m",
                        "health": "degraded",
                        "reason": str(exc)[:200],
                    }
                )
        return result

    async def refresh_trading_intelligence_official_events(self):
        events, health, persisted = await self.collect_official_events_once()
        return {
            "events": events,
            "active_risk_windows": self.active_risk_windows(events),
            "source_health": health,
            "persisted": persisted,
            "etf_flow": ETF_FLOW_STATUS,
        }

    async def trading_intelligence_overview(self):
        market_states = await asyncio.gather(
            *(
                asyncio.to_thread(self.trading_intelligence_store.latest_market_state, symbol)
                for symbol in V2_SYMBOLS
            )
        )
        quant_predictions = await asyncio.gather(
            *(
                asyncio.to_thread(self.trading_intelligence_store.latest_quant_prediction, symbol)
                for symbol in V2_SYMBOLS
            )
        )
        return {
            "version": 2,
            "runtime_modes": [str(item) for item in TradingModeV2],
            "symbols": list(V2_SYMBOLS),
            "market_states": [item for item in market_states if item],
            "active_theses": await asyncio.to_thread(
                self.trading_intelligence_store.list_active_theses
            ),
            "active_plans": await asyncio.to_thread(
                self.trading_intelligence_store.list_open_plans
            ),
            "active_waits": await asyncio.to_thread(
                self.trading_intelligence_store.list_wait_runtimes,
                active_only=True,
            ),
            "positions": await asyncio.to_thread(self.trading_intelligence_store.list_positions),
            "entry_orders": await asyncio.to_thread(self.v2_overview_entry_orders),
            "quant_predictions": [item for item in quant_predictions if item],
            "quant_models": await self.trading_intelligence_quant_models(),
            "episodes": await asyncio.to_thread(
                self.trading_intelligence_store.list_episodes, limit=50
            ),
            "automation": await asyncio.to_thread(
                self.trading_intelligence_store.automation_runtime
            ),
            "live_grant": await asyncio.to_thread(self.trading_intelligence_store.active_grant),
            "llm_health": await asyncio.to_thread(self.ai_gateway.health_snapshot),
            "learning": await asyncio.to_thread(self.v2_learning_snapshot),
            "portfolio_policy": self.configuration_service.current_configuration().risk.model_dump(
                mode="json"
            ),
        }

    def v2_overview_entry_orders(self):
        active = self.trading_intelligence_store.list_entry_orders(active_only=True)
        recent = self.trading_intelligence_store.list_entry_orders(limit=100)
        return list({item.entry_id: item for item in [*recent, *active]}.values())

    def v2_learning_snapshot(self):
        from .trading_learning import selected_snapshot

        return selected_snapshot(self.trading_intelligence_store).model_dump(mode="json")

    async def trading_intelligence_learning(self):
        return await asyncio.to_thread(self.v2_learning_snapshot)

    async def trading_intelligence_market_state(self, symbol: str):
        if symbol not in V2_SYMBOLS:
            raise HTTPException(400, "v2 supports only OKX BTC/ETH/SOL USDT perpetuals")
        state = await asyncio.to_thread(self.trading_intelligence_store.latest_market_state, symbol)
        if not state:
            raise HTTPException(404, "market state not available")
        return state

    async def ingest_trading_intelligence_market_state(self, state: MarketStateV2):
        raise HTTPException(403, "生产行情只能由服务端采集器写入，请使用 refresh 接口")

    async def refresh_trading_intelligence_market_state(self, symbol: str):
        if symbol not in V2_SYMBOLS:
            raise HTTPException(400, "v2 supports only OKX BTC/ETH/SOL USDT perpetuals")
        configuration = self.configuration_service.current_configuration()
        if configuration.exchange_id != "okx" or str(configuration.market_type) != "swap":
            raise HTTPException(409, "MarketState v2 requires OKX swap market data")
        exchange_symbol = symbol.removesuffix(":USDT")
        try:
            contexts = await asyncio.gather(
                *(
                    self.runtime.market_context(
                        limit=260,
                        symbol=exchange_symbol,
                        timeframe=timeframe,
                    )
                    for timeframe in ("1m", "5m", "15m", "1h", "4h", "1d")
                )
            )
        except Exception as exc:
            raise HTTPException(503, f"MarketState v2 source data unavailable: {exc}") from exc
        market = contexts[0][0].model_copy(update={"symbol": symbol})
        candles_by_timeframe: dict[str, list[Candle]] = {}
        quality_by_timeframe: dict[str, dict] = {}
        for timeframe, (_, history) in zip(
            ("1m", "5m", "15m", "1h", "4h", "1d"), contexts, strict=True
        ):
            causal = closed_candles(history, timeframe, market.timestamp)
            candles_by_timeframe[timeframe] = causal
            try:
                _, quality = validate_candles(causal, timeframe)
                quality_by_timeframe[timeframe] = quality.model_dump(mode="json")
            except Exception as exc:
                quality_by_timeframe[timeframe] = {
                    "status": "unavailable",
                    "message": str(exc)[:200],
                }
        microstructure: dict = {}
        if self.runtime.market is not None:
            with suppress(Exception):
                microstructure = (
                    await self.runtime.market.market_microstructure(exchange_symbol) or {}
                )
        cross_histories: dict[str, list[Candle]] = {symbol: candles_by_timeframe.get("1h", [])}
        other_symbols = [item for item in V2_SYMBOLS if item != symbol]
        other_contexts = await asyncio.gather(
            *(
                self.runtime.market_context(
                    limit=120,
                    symbol=item.removesuffix(":USDT"),
                    timeframe="1h",
                )
                for item in other_symbols
            ),
            return_exceptions=True,
        )
        for other_symbol, context in zip(other_symbols, other_contexts, strict=True):
            if isinstance(context, Exception):
                continue
            _, history = context
            cross_histories[other_symbol] = closed_candles(history, "1h", market.timestamp)
        state = await asyncio.to_thread(
            build_market_state_v2,
            market=market,
            candles_by_timeframe=candles_by_timeframe,
            quality_by_timeframe=quality_by_timeframe,
            microstructure=microstructure,
            cross_market=build_cross_market_context(cross_histories),
            normalized_events=self.active_risk_windows(
                await asyncio.to_thread(self.durable_official_events),
                at=market.timestamp,
            ),
        )
        try:
            await asyncio.to_thread(
                self.learning_shadow.observe,
                symbol,
                candles_by_timeframe.get("5m", []),
                now=market.timestamp,
            )
        except Exception:
            logger.exception("Shadow price observation unavailable")
        try:
            await self.infer_quant_v1_on_five_minute_close(state)
        except (OSError, ValueError, RuntimeError) as exc:
            await asyncio.to_thread(
                self.trading_intelligence_store.append_event,
                dedupe_key=f"quant-degraded:{state.symbol}:{state.data_as_of:%Y%m%d%H%M}",
                event_type="QUANT_INFERENCE_DEGRADED",
                aggregate_type="quant_model",
                aggregate_id=state.symbol,
                aggregate_version=1,
                payload={"reason_hash": sha256(str(exc).encode()).hexdigest()},
            )
        if self.risk_state_service.get().halted:
            await asyncio.to_thread(self.trading_intelligence_store.save_market_state, state)
        else:
            await self.require_v2_automation().on_market_state(state)
        return state

    async def trading_intelligence_episodes(
        self,
        symbol: str | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ):
        if symbol is not None and symbol not in V2_SYMBOLS:
            raise HTTPException(400, "unsupported v2 symbol")
        return await asyncio.to_thread(
            self.trading_intelligence_store.list_episodes,
            symbol=symbol,
            limit=limit,
        )

    async def trading_intelligence_events(
        self,
        aggregate_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ):
        return await asyncio.to_thread(
            self.trading_intelligence_store.list_events,
            aggregate_id=aggregate_id,
            limit=limit,
        )

    async def trading_intelligence_attribution(self):
        outcomes: list[DecisionOutcomeV2] = []
        for episode in await asyncio.to_thread(
            self.trading_intelligence_store.list_episodes,
            limit=5000,
        ):
            if not episode.outcome:
                continue
            try:
                outcomes.append(DecisionOutcomeV2.model_validate(episode.outcome))
            except ValidationError:
                continue
        return build_decision_attribution(outcomes)

    async def trading_intelligence_trade_history(
        self,
        symbol: str | None = None,
        days: int = Query(default=30, ge=0, le=3650),
        limit: int = Query(default=500, ge=1, le=2000),
    ):
        if symbol is not None and symbol not in V2_SYMBOLS:
            raise HTTPException(400, "unsupported v2 symbol")
        positions, episode_list = await asyncio.gather(
            asyncio.to_thread(
                self.trading_intelligence_store.list_positions,
                symbol=symbol,
                include_closed=True,
            ),
            asyncio.to_thread(
                self.trading_intelligence_store.list_episodes, symbol=symbol, limit=5000
            ),
        )
        episodes = {item.episode_id: item for item in episode_list}
        closed_episode_ids = {
            item.episode_id for item in positions if str(item.state) == "CLOSED" and item.episode_id
        }
        allocations: dict[str, RiskAllocation] = {}
        for episode in episode_list:
            if episode.episode_id not in closed_episode_ids:
                continue
            allocation_id = episode.risk_allocation_id
            if not allocation_id or allocation_id in allocations:
                continue
            artifact = await asyncio.to_thread(
                self.trading_intelligence_store.get_artifact,
                allocation_id,
            )
            if not artifact or artifact["artifact_type"] != "RiskAllocation":
                continue
            try:
                allocations[allocation_id] = RiskAllocation.model_validate(artifact["payload"])
            except ValidationError:
                continue
        return build_trade_history_v2(
            positions,
            episodes,
            allocations,
            symbol=symbol,
            days=days,
            limit=limit,
        )

    async def trading_intelligence_artifact(self, artifact_id: str):
        artifact = await asyncio.to_thread(
            self.trading_intelligence_store.get_artifact, artifact_id
        )
        if not artifact:
            raise HTTPException(404, "trading intelligence artifact not found")
        return artifact
