from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cosmos_quant.ai_usage import AICompletionResult, AIUsage
from cosmos_quant.domain import Candle
from cosmos_quant.learning_shadow import ShadowComparisonWorker, sampled
from cosmos_quant.storage import SqliteStore
from cosmos_quant.trading_intelligence_store import TradingIntelligenceStore
from cosmos_quant.trading_learning import learning_context, refresh_learning
from test_immediate_contract import NOW, decision, market
from test_portfolio_learning_upgrade import save_outcome


@pytest.fixture
def store(tmp_path):
    return TradingIntelligenceStore(SqliteStore(tmp_path / "learning.db"))


def test_causal_cases_exclusions_modes_and_version_pin(store):
    started = datetime.now(UTC)
    save_outcome(store, "complete", "100")
    save_outcome(store, "other-mode", "100", mode="SHADOW")
    save_outcome(store, "partial", "100", complete=False)
    snapshot = refresh_learning(store)
    assert snapshot.sample_count == 2
    assert snapshot.excluded[0]["episode_id"] == "partial"
    assert learning_context(store, "BTC/USDT:USDT", "OKX_DEMO", as_of=started)["cases"] == []
    context = learning_context(store, "BTC/USDT:USDT", "OKX_DEMO", regime="TREND_UP")
    assert [c["episode_id"] for c in context["cases"]] == ["complete"]
    assert context["cases"][0]["match"] == "EXACT_REGIME"
    assert len(context["input_hash"]) == 64
    store.store.learning.save_settings(
        {"enabled": True, "shadow_enabled": True, "pinned_version": snapshot.version}
    )
    save_outcome(store, "new", "200")
    refresh_learning(store)
    assert learning_context(store, "BTC/USDT:USDT", "OKX_DEMO")["version"] == snapshot.version
    store.store.learning.save_settings(
        {"enabled": False, "shadow_enabled": False, "pinned_version": None}
    )
    assert learning_context(store, "BTC/USDT:USDT", "OKX_DEMO")["status"] == "DISABLED"


def test_missing_fees_never_default_to_zero(store):
    save_outcome(store, "no-fees", "100")
    episode = store.list_episodes()[0]
    del episode.outcome["fees"]
    store.save_episode(episode)
    snapshot = refresh_learning(store)
    assert snapshot.sample_count == 0
    assert snapshot.skipped_count == 1


def test_quota_atomic_restart_and_shanghai_midnight(store):
    repo = store.store.learning
    now = datetime(2026, 10, 31, 15, 59, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: repo.reserve_shadow(str(i), {}, now=now), range(30)))
    assert sum(results) == 10
    assert not repo.reserve_shadow("0", {}, now=now)
    restarted = SqliteStore(store.store.path).learning
    assert restarted.summary(now)["daily_used"] == 10
    restarted.recover()
    assert restarted.summary(now)["counts"]["INTERRUPTED"] == 10
    assert restarted.reserve_shadow("next-day", {}, now=now + timedelta(minutes=1))
    assert restarted.summary(now + timedelta(minutes=1))["daily_used"] == 1


def comparison_result():
    d = decision()
    d.decision_id = next(f"sample-{i}" for i in range(100) if sampled(f"sample-{i}"))
    state = market()
    payload = {
        "market_state": state.model_dump(mode="json"),
        "analyst": {"same": True},
        "portfolio": {"symbol_positions": []},
        "learning": {"version": "learning-one", "cases": [{"episode_id": "prior"}], "groups": []},
    }
    completion = AICompletionResult(
        provider="custom",
        model="test-model",
        data=d.model_dump(mode="json"),
        latency_ms=4,
        configuration_hash="frozen",
        usage=AIUsage(total_tokens=12),
    )
    return SimpleNamespace(
        decision=d,
        episode=SimpleNamespace(symbol=state.symbol),
        trader_request={
            "prompt": json.dumps(payload),
            "system": "same system",
            "schema": {},
            "schema_name": "test",
        },
        trader_completion=completion,
    )


class FakeGateway:
    def __init__(self, invalid=False):
        self.calls = []
        self.invalid = invalid

    async def generate_shadow_json(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return AICompletionResult(
            provider="custom",
            model="test-model",
            latency_ms=2,
            data={} if self.invalid else decision().model_dump(mode="json"),
            usage=AIUsage(total_tokens=7),
        )


def test_shadow_only_changes_learning_and_never_writes_orders(store):
    async def run():
        gateway = FakeGateway()
        worker = ShadowComparisonWorker(store.store.learning, gateway)
        result = comparison_result()
        original = copy.deepcopy(result.trader_request)
        worker.start()
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        await asyncio.wait_for(worker.queue.join(), 3)
        await worker.stop()
        assert len(gateway.calls) == 1
        sent = json.loads(gateway.calls[0]["prompt"])
        before = json.loads(original["prompt"])
        assert sent.pop("learning") == {"policy": "ADVISORY_ONLY", "groups": []}
        before.pop("learning")
        assert sent == before
        assert result.trader_request == original
        assert gateway.calls[0]["configuration_hash"] == "frozen"
        assert store.list_open_plans() == []
        assert store.list_positions() == []
        assert store.list_episodes() == []
        record = store.store.learning.shadows()["items"][0]
        assert record["status"] == "COMPLETED"
        assert record["payload"]["format_valid"]

    asyncio.run(run())


def test_shadow_repair_is_bounded_and_busy_work_is_skipped(store):
    async def run():
        gateway = FakeGateway(invalid=True)
        worker = ShadowComparisonWorker(store.store.learning, gateway)
        result = comparison_result()
        output = await worker.compare(result.trader_request, result.trader_completion)
        assert len(gateway.calls) == 2
        assert not output["format_valid"]
        worker.foreground_busy = lambda: 2
        worker.start()
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        await worker.stop()
        assert (
            store.store.learning.shadows()["items"][0]["payload"]["skip_reason"]
            == "FOREGROUND_BUSY"
        )

    asyncio.run(run())


def test_price_proxy_requires_complete_closed_horizon_and_has_no_pnl(store):
    repo = store.store.learning
    observed = NOW + timedelta(seconds=30)
    payload = {
        "symbol": "BTC/USDT:USDT",
        "observed_at": observed.isoformat(),
        "reference_price": "100",
        "primary": {"action": "LONG"},
        "control": {"action": "WAIT"},
        "format_valid": True,
        "proxy": {"status": "PENDING"},
    }
    repo.reserve_shadow("proxy", payload)
    repo.update_shadow("proxy", "COMPLETED")
    candles = [
        Candle(
            timestamp=NOW + timedelta(minutes=5 * (i + 1)),
            open=100,
            high=102,
            low=99,
            close=101,
            volume=1,
        )
        for i in range(24)
    ]
    worker = ShadowComparisonWorker(repo, FakeGateway())
    worker.observe(payload["symbol"], candles, now=NOW + timedelta(minutes=120))
    assert repo.shadows()["items"][0]["payload"]["proxy"]["status"] == "PENDING"
    worker.observe(payload["symbol"], candles, now=NOW + timedelta(minutes=125))
    result = repo.shadows()["items"][0]["payload"]["proxy"]
    assert result["primary"]["kind"] == "PRICE_PROXY_NOT_PNL"
    assert result["control"]["status"] == "NOT_DIRECTIONAL"
    assert "pnl" not in result["primary"]
    repo.reserve_shadow("gap", payload)
    repo.update_shadow("gap", "COMPLETED")
    worker.observe(payload["symbol"], candles[1:], now=NOW + timedelta(minutes=125))
    rows = {r["decision_id"]: r for r in repo.shadows()["items"]}
    assert rows["gap"]["payload"]["proxy"]["status"] == "UNAVAILABLE"


def test_shadow_timeout_and_stop_are_terminal_without_retry(store):
    async def run():
        started = asyncio.Event()
        calls = []

        class SlowGateway:
            async def generate_shadow_json(self, **kwargs):
                calls.append(kwargs)
                started.set()
                await asyncio.Event().wait()

        worker = ShadowComparisonWorker(store.store.learning, SlowGateway(), timeout_seconds=0.05)
        worker.start()
        result = comparison_result()
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        await asyncio.wait_for(worker.queue.join(), 2)
        await worker.stop()
        assert len(calls) == 1
        assert store.store.learning.shadows()["items"][0]["status"] == "FAILED"
        worker.start()
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        assert worker.queue.empty()  # persistent idempotency includes failed requests
        result.decision.decision_id = next(
            f"cancel-{i}" for i in range(100) if sampled(f"cancel-{i}")
        )
        started.clear()
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        await asyncio.wait_for(started.wait(), 2)
        await worker.stop()
        assert not worker.accepting and worker.task is None
        assert store.store.learning.shadows()["items"][0]["status"] == "INTERRUPTED"

    asyncio.run(run())


def test_queue_limit_and_no_context_are_persisted_without_budget(store):
    async def run():
        worker = ShadowComparisonWorker(store.store.learning, FakeGateway())
        worker.accepting = True  # keep the worker stopped to saturate its queue deterministically
        for i in range(10):
            worker.queue.put_nowait((f"queued-{i}", None))
        result = comparison_result()
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        row = store.store.learning.shadows()["items"][0]
        assert row["payload"]["skip_reason"] == "QUEUE_FULL"
        assert row["reserved"] == 0
        await worker.stop()
        worker.accepting = True
        result.decision.decision_id = next(
            f"empty-{i}" for i in range(100) if sampled(f"empty-{i}")
        )
        prompt = json.loads(result.trader_request["prompt"])
        prompt["learning"] = {"groups": [], "cases": []}
        result.trader_request["prompt"] = json.dumps(prompt)
        await worker.offer(result, mode="OKX_DEMO", decision_started=NOW)
        assert (
            store.store.learning.shadows()["items"][0]["payload"]["skip_reason"]
            == "NO_VALID_LEARNING_CONTEXT"
        )
        assert store.store.learning.summary()["daily_used"] == 0

    asyncio.run(run())


def test_case_selection_is_bounded_and_prefers_regime(store):
    for i in range(8):
        save_outcome(store, f"case-{i}", "10")
    snapshot = refresh_learning(store)
    assert snapshot.sample_count == 8
    result = learning_context(store, "BTC/USDT:USDT", "OKX_DEMO", regime="TREND_UP")
    assert len(result["cases"]) == 5
    assert all(c["match"] == "EXACT_REGIME" for c in result["cases"])
    fallback = learning_context(store, "BTC/USDT:USDT", "OKX_DEMO", regime="RANGE")
    assert all(c["match"] == "SYMBOL_FALLBACK" for c in fallback["cases"])


def test_gateway_shadow_uses_one_attempt_and_rejects_configuration_change(monkeypatch):
    from hashlib import sha256

    from cosmos_quant.ai_configuration import AIProviderConfiguration
    from cosmos_quant.ai_gateway import AIGateway

    config = AIProviderConfiguration(
        provider="custom", model="test-model", base_url="http://localhost:9999"
    )
    instances = []

    class Adapter:
        configuration = config

        async def generate_json(self, **kwargs):
            assert self.maximum_attempts == 1
            return AICompletionResult(provider="custom", model="test-model", data={}, latency_ms=1)

        async def close(self):
            self.closed = True

    gateway = AIGateway(SimpleNamespace())

    def adapter(_):
        item = Adapter()
        instances.append(item)
        return item

    async def record(_):
        pass

    monkeypatch.setattr(gateway, "_adapter", adapter)
    monkeypatch.setattr(gateway, "_record_usage", record)

    async def run():
        await gateway.generate_shadow_json(
            provider="custom",
            configuration_hash=sha256(config.model_dump_json().encode()).hexdigest(),
            expected_model="test-model",
            system="test",
            prompt="test",
            schema={},
            schema_name="test",
        )
        with pytest.raises(ValueError, match="MODEL_CONFIGURATION_CHANGED"):
            await gateway.generate_shadow_json(
                provider="custom",
                configuration_hash="stale",
                expected_model="test-model",
                system="test",
                prompt="test",
                schema={},
                schema_name="test",
            )
        assert all(a.closed for a in instances)

    asyncio.run(run())
