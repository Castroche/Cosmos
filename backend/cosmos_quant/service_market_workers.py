from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from cosmos_quant.quant_intelligence import LightGBMQuantIntelligenceV1

from .quant_intelligence import QuantModelArtifactV1, market_state_feature_vector
from .trading_intelligence import V2_SYMBOLS, MarketStateV2

logger = __import__("logging").getLogger(__name__)


class MarketWorkersService:
    def load_quant_v1_artifact(self, symbol: str) -> QuantModelArtifactV1 | None:
        """Read display metadata without importing or constructing native predictors."""

        cached = self.quant_model_cache.get(symbol)
        if cached is not None:
            return cached.artifact
        filename = {
            "BTC/USDT:USDT": "btc-usdt-swap.json",
            "ETH/USDT:USDT": "eth-usdt-swap.json",
            "SOL/USDT:USDT": "sol-usdt-swap.json",
        }.get(symbol)
        if filename is None:
            return None
        path = self.QUANT_V1_MODEL_DIR / filename
        if not path.is_file():
            return None
        artifact = QuantModelArtifactV1.model_validate_json(path.read_text(encoding="utf-8"))
        if artifact.symbol != symbol or artifact.timeframe != "5m":
            raise ValueError("Quant model artifact scope does not match BTC/ETH/SOL 5m v1")
        return artifact

    def load_quant_v1_model(self, symbol: str) -> LightGBMQuantIntelligenceV1 | None:
        # UI requests and minute workers can arrive together during cold startup.
        # Only one native predictor per symbol may be created.
        with self.quant_model_load_lock:
            cached = self.quant_model_cache.get(symbol)
            if cached is not None:
                return cached
            artifact = self.load_quant_v1_artifact(symbol)
            if artifact is None:
                return None
            model = self.LightGBMQuantIntelligenceV1(artifact)
            self.quant_model_cache[symbol] = model
            return model

    async def infer_quant_v1_on_five_minute_close(self, state: MarketStateV2) -> None:
        model = await asyncio.to_thread(self.load_quant_v1_model, state.symbol)
        if model is None:
            return
        bucket = state.data_as_of.replace(
            minute=(state.data_as_of.minute // 5) * 5,
            second=0,
            microsecond=0,
        )
        latest = await asyncio.to_thread(
            self.trading_intelligence_store.latest_quant_prediction,
            state.symbol,
        )
        if latest and latest.data_as_of >= bucket:
            return
        features = market_state_feature_vector(
            state.model_dump(mode="json", exclude_none=True),
            model.artifact.feature_names,
        )
        prediction = await asyncio.to_thread(
            model.infer,
            data_as_of=bucket,
            features=features,
        )
        await asyncio.to_thread(self.trading_intelligence_store.save_quant_prediction, prediction)
        await self.trading_intelligence_engine.ingest_external_event(
            symbol=state.symbol,
            event_type="QUANT_PREDICTION_UPDATED",
            event_key=prediction.prediction_id,
            payload={"quant_prediction_id": prediction.prediction_id},
        )

    async def trading_intelligence_market_state_loop(self) -> None:
        """Update v2 state on minute closes; ordinary updates never invoke an LLM."""

        async def symbol_loop(symbol: str) -> None:
            while True:
                started = datetime.now(UTC)
                try:
                    await self.refresh_trading_intelligence_market_state(symbol)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.metrics.increment(
                        "trading_intelligence_market_state_failures_total",
                        symbol=symbol,
                    )
                elapsed = (datetime.now(UTC) - started).total_seconds()
                await asyncio.sleep(max(60 - elapsed, 1))

        async with asyncio.TaskGroup() as workers:
            for symbol in V2_SYMBOLS:
                workers.create_task(symbol_loop(symbol), name=f"market-state:{symbol}")
                workers.create_task(
                    self.trading_intelligence_decision_queue_loop(symbol),
                    name=f"decision-queue:{symbol}",
                )
            workers.create_task(
                self.trading_intelligence_price_loop(), name="trading-intelligence-prices"
            )

    async def trading_intelligence_decision_queue_loop(self, symbol: str) -> None:
        while True:
            try:
                if not self.risk_state_service.get().halted:
                    await self.require_v2_automation().drain_decisions(symbol)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Deferred decision failed for %s", symbol)
            await asyncio.sleep(1)

    async def trading_intelligence_price_loop(self) -> None:
        """Read ticker cache; full LLM features stay on their own cadence."""
        while True:
            for symbol in V2_SYMBOLS:
                try:
                    tick = self.runtime.market_data.websocket_snapshot_for(
                        symbol.removesuffix(":USDT")
                    )
                    if tick:
                        await self.require_v2_automation().on_price_tick(tick)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.metrics.increment(
                        "trading_intelligence_price_tick_failures_total", symbol=symbol
                    )
            await asyncio.sleep(1)

    async def enqueue_v2_trade_completed(self, symbol: str, event_id: str) -> None:
        self.require_v2_automation().enqueue_decision(symbol, "TRADE_COMPLETED", event_id)
