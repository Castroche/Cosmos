from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from .domain import LiquidationEvent
from .exchanges.paper import PaperExchange
from .liquidation_stream import BINANCE_LIQUIDATION_STREAM
from .market_data import TIMEFRAME_SECONDS
from .market_data_capture import MarketDataCaptureSession, calculate_capture_coverage
from .risk import RiskCheck, RiskDecision

logger = __import__("logging").getLogger(__name__)


class MarketService:
    async def dashboard(self):
        configuration = self.configuration_service.current_configuration()
        try:
            market, history = await self.runtime.market_context(
                limit=configuration.strategy.research_bars
            )
            portfolio = await self.runtime.portfolio()
        except Exception as exc:
            raise HTTPException(503, f"真实行情不可用：{exc}") from exc
        decision = RiskDecision(
            approved=False,
            checks=[
                RiskCheck(
                    code="trading_intelligence_v2_boundary",
                    passed=False,
                    message="仅在 v2 WakeCondition 触发后为对应 TradePlan 执行预交易风控",
                )
            ],
        )
        execution = self.runtime.execution
        assert execution is not None
        if self.runtime.order_ledger and self.runtime.order_ledger.records:
            recent_orders = self.runtime.order_ledger.recent_orders(10)
        elif isinstance(execution, PaperExchange):
            recent_orders = list(execution.orders.values())[-10:][::-1]
        else:
            recent_orders = (await execution.open_orders())[-10:][::-1]
        latency_ms = max(round((datetime.now(UTC) - market.timestamp).total_seconds() * 1000), 0)
        quality = self.runtime.market_quality
        return {
            "market": market,
            "candles": history[-self.CHART_CANDLE_LIMIT :],
            "portfolio": portfolio,
            "risk": decision,
            "orders": recent_orders,
            "audit": self.audit[:12],
            "system": {
                "mode": configuration.trading_mode,
                "market_type": configuration.market_type,
                "margin_mode": configuration.margin_mode,
                "contract_leverage": configuration.contract_leverage,
                "data_latency_ms": latency_ms,
                "market_data_real": True,
                "exchange": configuration.exchange_id,
                "market_quality": quality,
                "risk_state": self.risk_state_service.get(),
                "reconciliation": self.runtime.reconciliation_report,
                "reconciliation_health": self.runtime.reconciliation_health,
                "private_order_stream": self.runtime.private_stream_status,
            },
        }

    async def market_window(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
        limit: int = 300,
    ):
        configuration = self.configuration_service.current_configuration()
        selected_symbol = (symbol or configuration.default_symbol).upper()
        selected_timeframe = timeframe or configuration.default_timeframe
        if selected_symbol not in configuration.strategy.watchlist:
            raise HTTPException(400, "交易品种不在已配置的观察列表中")
        if selected_timeframe not in TIMEFRAME_SECONDS:
            raise HTTPException(400, "不支持的 K 线周期")
        if not 60 <= limit <= 500:
            raise HTTPException(400, "K 线数量必须在 60–500 之间")
        try:
            market, history = await self.runtime.market_context(
                limit=limit,
                symbol=selected_symbol,
                timeframe=selected_timeframe,
            )
        except Exception as exc:
            raise HTTPException(503, f"真实行情不可用：{exc}") from exc
        return {
            "market": market,
            "candles": history[-limit:],
            "timeframe": selected_timeframe,
            "quality": self.runtime.market_quality,
        }

    async def liquidation_collection_status(self):
        if self.liquidation_collector is None:
            return {
                "running": False,
                "historical_backfill": False,
                "sampling": "latest_per_symbol_within_1000ms",
                "message": "collector has not started",
            }
        sessions_payload, event_summaries = await asyncio.gather(
            asyncio.to_thread(
                partial(
                    self.store.list_market_data_capture_sessions,
                    source="binance",
                    stream=BINANCE_LIQUIDATION_STREAM,
                )
            ),
            asyncio.to_thread(self.store.summarize_liquidation_events),
        )
        sessions = [
            MarketDataCaptureSession.model_validate(payload) for payload in sessions_payload
        ]
        now = datetime.now(UTC)
        coverage = [
            calculate_capture_coverage(
                sessions,
                source="binance",
                stream=BINANCE_LIQUIDATION_STREAM,
                symbol=symbol,
                now=now,
            ).model_dump(mode="json")
            for symbol in sorted(self.liquidation_collector.symbols)
        ]
        return {
            **self.liquidation_collector.status(),
            "durable_capture_coverage": coverage,
            "event_summaries": event_summaries,
        }

    async def liquidation_events(self, symbol: str | None = None, limit: int = 1000):
        normalized = symbol.upper().replace("-", "/") if symbol else None
        payloads = await asyncio.to_thread(
            partial(
                self.store.list_liquidation_events,
                symbol=normalized,
                limit=limit,
            )
        )
        return [LiquidationEvent.model_validate(item) for item in payloads]

    async def stream_market(self, websocket: WebSocket):
        """Push the current exchange tick and forming candle to the browser."""

        if not self.local_access.trusted_origin(
            websocket.headers.get("origin"),
            str(websocket.url),
        ):
            await websocket.close(code=1008, reason="untrusted origin")
            return
        await websocket.accept()
        queue = self.runtime.market_data.subscribe()
        try:
            configuration = self.configuration_service.current_configuration()
            symbol = websocket.query_params.get("symbol", configuration.default_symbol).upper()
            timeframe = websocket.query_params.get("timeframe", configuration.default_timeframe)
            if symbol not in configuration.strategy.watchlist or timeframe not in TIMEFRAME_SECONDS:
                await websocket.close(code=1008, reason="unsupported market subscription")
                return
            await self.runtime.market_context(
                limit=300,
                symbol=symbol,
                timeframe=timeframe,
            )
            initial = self.runtime.market_data.stream_event(symbol, timeframe)
            if initial:
                await websocket.send_json(initial)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                    candle = event["candles"].get(timeframe)
                    if candle and event["market"]["symbol"] == symbol:
                        await websocket.send_json(event)
                except TimeoutError:
                    await websocket.send_json(
                        {
                            "type": "heartbeat",
                            "server_time": datetime.now(UTC).isoformat(),
                        }
                    )
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            with suppress(Exception):
                await websocket.send_json({"type": "error", "message": str(exc)[:180]})
                await websocket.close(code=1011)
        finally:
            self.runtime.market_data.unsubscribe(queue)
