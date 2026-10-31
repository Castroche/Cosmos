"""Observational comparisons. This module has no order or execution dependency."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from .trading_intelligence import MarketStateV2, PortfolioTraderDecision
from .trading_intelligence_engine import new_decision_contract_errors

logger = logging.getLogger(__name__)


def sampled(decision_id: str) -> bool:
    return int(sha256(decision_id.encode()).hexdigest()[:16], 16) % 10 == 0


def price_proxy(decision, candles, reference):
    """Fixed-horizon price observations, deliberately not a fill/PnL simulation."""
    action = decision.get("action")
    if action not in {"LONG", "SHORT"}:
        return {"status": "NOT_DIRECTIONAL", "kind": "PRICE_PROXY_NOT_PNL"}
    direction = 1 if action == "LONG" else -1
    entry = float(reference)
    if entry <= 0:
        return {"status": "UNAVAILABLE", "kind": "PRICE_PROXY_NOT_PNL"}
    returns = [direction * (float(c.close) / entry - 1) for c in candles]
    favorable = [
        direction * (float(c.high if direction == 1 else c.low) / entry - 1) for c in candles
    ]
    adverse = [
        direction * (float(c.low if direction == 1 else c.high) / entry - 1) for c in candles
    ]
    return {
        "status": "COMPLETE",
        "kind": "PRICE_PROXY_NOT_PNL",
        "horizon_bars": 24,
        "direction_return": returns[-1],
        "mfe": max(0, *favorable),
        "mae": min(0, *adverse),
    }


class ShadowComparisonWorker:
    def __init__(self, repository, gateway, *, foreground_busy=lambda: False, timeout_seconds=60):
        self.timeout_seconds = timeout_seconds
        self.repository = repository
        self.gateway = gateway
        self.foreground_busy = foreground_busy
        self.queue = asyncio.Queue(maxsize=10)
        self.task = None
        self.accepting = False

    def start(self):
        if self.task is None or self.task.done():
            self.accepting = True
            self.task = asyncio.create_task(self.run(), name="learning-shadow-comparisons")

    async def stop(self):
        self.accepting = False
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        while not self.queue.empty():
            decision_id, _ = self.queue.get_nowait()
            await asyncio.to_thread(self.repository.update_shadow, decision_id, "CANCELLED")
            self.queue.task_done()

    async def offer(self, result, *, mode, decision_started):
        if not self.accepting or not self.repository.settings()["shadow_enabled"]:
            return
        request = getattr(result, "trader_request", None)
        completion = getattr(result, "trader_completion", None)
        if request is None or completion is None:
            return
        decision_id = result.decision.decision_id
        if not sampled(decision_id):
            return
        prompt = json.loads(request["prompt"])
        learning = prompt.get("learning") or {}
        skip = None
        if not learning.get("cases"):
            skip = "NO_VALID_LEARNING_CONTEXT"
        elif not completion.configuration_hash:
            skip = "MISSING_MODEL_CONFIGURATION"
        elif self.queue.full():
            skip = "QUEUE_FULL"
        elif self.foreground_busy() > 1:
            skip = "FOREGROUND_BUSY"
        payload = {
            "symbol": result.episode.symbol,
            "mode": mode,
            "decision_started": decision_started.isoformat(),
            "observed_at": datetime.now(UTC).isoformat(),
            "reference_price": prompt["market_state"]["market"]["values"].get("last"),
            "learning_version": learning.get("version"),
            "case_ids": [c["episode_id"] for c in learning.get("cases", [])],
            "input_hash": sha256(request["prompt"].encode()).hexdigest(),
            "primary": result.decision.model_dump(mode="json"),
            "primary_raw": completion.data,
            "primary_usage": completion.usage.model_dump(mode="json"),
            "primary_latency_ms": completion.latency_ms,
            "model": completion.model,
            "provider": str(completion.provider),
            "configuration_hash": completion.configuration_hash,
            "proxy": {"status": "PENDING", "kind": "PRICE_PROXY_NOT_PNL"},
        }
        reserved = await asyncio.to_thread(
            self.repository.reserve_shadow, decision_id, payload, skip=skip
        )
        if reserved and not self.accepting:
            await asyncio.to_thread(self.repository.update_shadow, decision_id, "CANCELLED")
            return
        if reserved:
            # Queue insertion is non-awaiting and cannot race another offer on this loop.
            try:
                self.queue.put_nowait((decision_id, (copy.deepcopy(request), completion)))
            except asyncio.QueueFull:
                await asyncio.to_thread(
                    self.repository.update_shadow, decision_id, "SKIPPED", skip_reason="QUEUE_FULL"
                )

    async def run(self):
        while True:
            decision_id, request = await self.queue.get()
            try:
                if self.foreground_busy():
                    await asyncio.to_thread(
                        self.repository.update_shadow,
                        decision_id,
                        "SKIPPED",
                        skip_reason="FOREGROUND_BUSY",
                    )
                    continue
                if not self.repository.settings()["shadow_enabled"]:
                    await asyncio.to_thread(self.repository.update_shadow, decision_id, "CANCELLED")
                    continue
                await asyncio.to_thread(self.repository.update_shadow, decision_id, "RUNNING")
                async with asyncio.timeout(self.timeout_seconds):
                    changes = await self.compare(*request)
                await asyncio.to_thread(
                    self.repository.update_shadow, decision_id, "COMPLETED", **changes
                )
            except asyncio.CancelledError:
                await asyncio.to_thread(self.repository.update_shadow, decision_id, "INTERRUPTED")
                raise
            except Exception as exc:
                await asyncio.to_thread(
                    self.repository.update_shadow,
                    decision_id,
                    "FAILED",
                    error=type(exc).__name__,
                    detail=str(exc)[:300],
                )
                logger.warning("Shadow comparison failed: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def compare(self, request, primary):
        request = copy.deepcopy(request)
        prompt = json.loads(request["prompt"])
        prompt["learning"] = {"policy": "ADVISORY_ONLY", "groups": []}
        request["prompt"] = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
        request["operation"] = "learning_shadow_trader"
        usages = []
        latencies = []
        payload = None
        for attempt in range(2):
            result = await self.gateway.generate_shadow_json(
                provider=primary.provider,
                configuration_hash=primary.configuration_hash,
                expected_model=primary.model,
                **request,
            )
            usages.append(result.usage.model_dump(mode="json"))
            latencies.append(result.latency_ms)
            payload = result.data
            try:
                decision = PortfolioTraderDecision.model_validate(payload)
                errors = new_decision_contract_errors(
                    decision,
                    market_state=MarketStateV2.model_validate(
                        {
                            k: v
                            for k, v in prompt["market_state"].items()
                            if k in MarketStateV2.model_fields
                        }
                    ),
                    portfolio=prompt["portfolio"],
                )
                if errors:
                    raise ValueError("; ".join(errors))
                return {
                    "control": decision.model_dump(mode="json"),
                    "format_valid": True,
                    "control_usage": usages,
                    "control_latency_ms": sum(latencies),
                    "control_input_hash": sha256(
                        json.dumps(prompt, sort_keys=True).encode()
                    ).hexdigest(),
                    "repaired": bool(attempt),
                }
            except ValueError as exc:
                if attempt == 1:
                    return {
                        "control": payload,
                        "format_valid": False,
                        "repaired": True,
                        "validation_error": str(exc)[:500],
                        "control_usage": usages,
                        "control_latency_ms": sum(latencies),
                    }
                request["operation"] = "learning_shadow_repair"
                request["prompt"] = json.dumps(
                    {
                        "original_context": prompt,
                        "invalid": payload,
                        "format_errors": str(exc)[:2000],
                    },
                    default=str,
                )
        raise AssertionError("bounded repair exhausted")

    def observe(self, symbol, candles, *, now=None):
        now = now or datetime.now(UTC)
        # Query pending comparisons explicitly: pagination must not evict unevaluated evidence.
        with self.repository.store.connect() as c:
            rows = c.execute(
                "SELECT decision_id,payload FROM ti_learning_shadows "
                "WHERE status='COMPLETED' AND "
                "json_extract(payload,'$.proxy.status')='PENDING' "
                "AND json_extract(payload,'$.symbol')=?",
                (symbol,),
            ).fetchall()
        for row in rows:
            data = json.loads(row["payload"])
            observed = datetime.fromisoformat(data["observed_at"])
            boundary = observed.replace(second=0, microsecond=0)
            boundary = boundary.replace(minute=boundary.minute // 5 * 5) + timedelta(minutes=5)
            expected = [boundary + timedelta(minutes=5 * i) for i in range(24)]
            if now < expected[-1] + timedelta(minutes=5):
                continue
            by_time = {c.timestamp: c for c in candles}
            if any(t not in by_time for t in expected) or not data.get("reference_price"):
                proxy = {
                    "status": "UNAVAILABLE",
                    "reason": "MISSING_CANDLES_OR_REFERENCE",
                    "kind": "PRICE_PROXY_NOT_PNL",
                }
            elif not data.get("format_valid"):
                proxy = {
                    "status": "UNAVAILABLE",
                    "reason": "INVALID_CONTROL",
                    "kind": "PRICE_PROXY_NOT_PNL",
                }
            else:
                series = [by_time[t] for t in expected]
                proxy = {
                    "status": "COMPLETE",
                    "kind": "PRICE_PROXY_NOT_PNL",
                    "primary": price_proxy(data["primary"], series, data["reference_price"]),
                    "control": price_proxy(data["control"], series, data["reference_price"]),
                }
            self.repository.update_shadow(row["decision_id"], "COMPLETED", proxy=proxy)
